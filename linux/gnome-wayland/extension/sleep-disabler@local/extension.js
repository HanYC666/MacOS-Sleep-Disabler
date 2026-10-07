import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const APP = 'org.sleepdisabler.App';
const PATH = '/org/sleepdisabler/App';
const IFACE = 'org.sleepdisabler.App1';
const PANEL_RUNTIME_VERSION = '5';
const STATE_ATTEMPTS = 3;
const STATE_RETRY_DELAYS = [1, 2];

function value(state, key, fallback = null) {
    const item = state?.[key];
    if (item === undefined)
        return fallback;
    return item?.deep_unpack ? item.deep_unpack() : item;
}

export default class SleepDisablerExtension extends Extension {
    enable() {
        this._bus = null;
        this._watch = 0;
        this._signal = 0;
        this._timerSignal = 0;
        this._state = null;
        this._actionError = '';
        this._updating = false;
        // A disabled instance may be enabled again while old replies are pending.
        this._generation = (this._generation ?? 0) + 1;
        this._owner = '';
        this._stateSerial = 0;
        this._stateAttempts = 0;
        this._stateRetry = 0;
        this._runtimeRegistrationStarted = false;
        this._connectionState = 'disconnected';
        this._cancellable = new Gio.Cancellable();

        this._button = new PanelMenu.Button(0.0, 'Sleep Disabler', false);
        this._icon = new St.Icon({icon_name: 'weather-clear-night-symbolic', style_class: 'system-status-icon'});
        this._button.add_child(this._icon);
        this._status = new PopupMenu.PopupMenuItem('Agent not connected', {reactive: false});
        this._button.menu.addMenuItem(this._status);
        this._button.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._prevention = new PopupMenu.PopupSwitchMenuItem('Prevent sleep and idle blanking', false);
        this._prevention.connect('toggled', (_item, enabled) => {
            if (!this._updating)
                this._call('SetPrevention', new GLib.Variant('(b)', [enabled]));
        });
        this._button.menu.addMenuItem(this._prevention);

        this._lid = new PopupMenu.PopupSwitchMenuItem('Lid stay awake (experimental)', false);
        this._lid.connect('toggled', (_item, enabled) => {
            if (!this._updating)
                this._call('SetLidMode', new GLib.Variant('(b)', [enabled]));
        });
        this._button.menu.addMenuItem(this._lid);

        this._dimming = new PopupMenu.PopupSwitchMenuItem('Dim panel on lid close', false);
        this._dimming.connect('toggled', (_item, enabled) => {
            if (!this._updating)
                this._call('SetLidDimming', new GLib.Variant('(b)', [enabled]));
        });
        this._button.menu.addMenuItem(this._dimming);

        this._failsafe = new PopupMenu.PopupSwitchMenuItem('Battery failsafe', false);
        this._failsafe.connect('toggled', (_item, enabled) => {
            if (!this._updating)
                this._call('SetFailsafe', new GLib.Variant('(bu)', [enabled, value(this._state, 'threshold', 20)]));
        });
        this._button.menu.addMenuItem(this._failsafe);

        this._button.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._timerItems = [];
        for (const [label, seconds] of [['Sleep in 30 minutes', 1800], ['Sleep in 1 hour', 3600], ['Sleep in 2 hours', 7200]]) {
            const item = new PopupMenu.PopupMenuItem(label);
            item.connect('activate', () => this._call('StartTimer', new GLib.Variant('(tbb)', [seconds, false, false])));
            this._button.menu.addMenuItem(item);
            this._timerItems.push(item);
        }
        this._cancel = new PopupMenu.PopupMenuItem('Cancel countdown');
        this._cancel.connect('activate', () => this._call('CancelTimer', new GLib.Variant('()')));
        this._button.menu.addMenuItem(this._cancel);
        this._help = new PopupMenu.PopupMenuItem('More options: sleep-disablerctl --help', {reactive: false});
        this._button.menu.addMenuItem(this._help);

        Main.panel.addToStatusArea(this.uuid, this._button);
        this._setConnectionState('disconnected');
        this._watch = Gio.bus_watch_name(
            Gio.BusType.SESSION, APP, Gio.BusNameWatcherFlags.NONE,
            (connection, _name, owner) => this._connected(connection, owner),
            () => this._disconnected()
        );
    }

    _connected(connection, owner = APP) {
        if (!this._button)
            return;
        this._generation++;
        this._cancelStateRetry();
        this._owner = owner;
        this._stateSerial = 0;
        this._stateAttempts = 0;
        this._runtimeRegistrationStarted = false;
        if (this._bus && this._signal)
            this._bus.signal_unsubscribe(this._signal);
        if (this._bus && this._timerSignal)
            this._bus.signal_unsubscribe(this._timerSignal);
        this._bus = connection;
        this._state = null;
        this._actionError = '';
        this._setConnectionState('connecting');
        const generation = this._generation;
        this._signal = connection.signal_subscribe(
            owner, IFACE, 'StateChanged', PATH, null,
            Gio.DBusSignalFlags.NONE,
            (_bus, _sender, _path, _iface, _signal, parameters) => {
                if (!this._button || generation !== this._generation)
                    return;
                const [state] = parameters.deep_unpack();
                this._stateSerial++;
                this._cancelStateRetry();
                this._setConnectionState('connected');
                this._render(state);
            }
        );
        this._timerSignal = connection.signal_subscribe(
            owner, IFACE, 'TimerChanged', PATH, null,
            Gio.DBusSignalFlags.NONE,
            (_bus, _sender, _path, _iface, _signal, parameters) => {
                if (!this._button || generation !== this._generation || !this._state)
                    return;
                const [remaining] = parameters.deep_unpack();
                this._stateSerial++;
                this._state.timerRemaining = new GLib.Variant('t', remaining);
                this._render(this._state);
            }
        );
        this._refresh();
        this._registerRuntime();
    }

    _cancelStateRetry() {
        if (this._stateRetry)
            GLib.source_remove(this._stateRetry);
        this._stateRetry = 0;
    }

    _registerRuntime() {
        if (!this._bus || !this._button || this._runtimeRegistrationStarted)
            return;
        this._runtimeRegistrationStarted = true;
        const generation = this._generation;
        try {
            this._bus.call(this._owner, PATH, IFACE, 'RegisterPanelRuntime',
                new GLib.Variant('(s)', [PANEL_RUNTIME_VERSION]), null,
                Gio.DBusCallFlags.NONE, 5000, this._cancellable, (bus, result) => {
                    try {
                        bus.call_finish(result);
                    } catch (error) {
                        if (this._button && generation === this._generation)
                            this._showError(error);
                    }
                });
        } catch (error) {
            this._showError(error);
        }
    }

    _refresh() {
        if (!this._bus || !this._button)
            return;
        const generation = this._generation;
        // Requests as well as signals supersede older in-flight snapshots.
        const serial = ++this._stateSerial;
        const initial = !this._state;
        if (initial)
            this._stateAttempts++;
        try {
            this._bus.call(this._owner, PATH, IFACE, 'GetState', new GLib.Variant('()'),
                new GLib.VariantType('(a{sv})'), Gio.DBusCallFlags.NONE, 5000,
                this._cancellable, (bus, result) => {
                    if (!this._button || generation !== this._generation)
                        return;
                    try {
                        const [state] = bus.call_finish(result).deep_unpack();
                        if (serial !== this._stateSerial)
                            return;
                        this._cancelStateRetry();
                        this._setConnectionState('connected');
                        this._render(state);
                    } catch (error) {
                        this._stateFailed(error, generation, serial);
                    }
                });
        } catch (error) {
            this._stateFailed(error, generation, serial);
        }
    }

    _stateFailed(error, generation, serial) {
        // Newer authoritative signals and later lifecycles supersede failures.
        if (!this._button || generation !== this._generation || serial !== this._stateSerial)
            return;
        if (!this._state && this._stateAttempts < STATE_ATTEMPTS) {
            this._setConnectionState('retrying');
            this._cancelStateRetry();
            this._stateRetry = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT,
                STATE_RETRY_DELAYS[this._stateAttempts - 1], () => {
                    if (!this._button || generation !== this._generation)
                        return GLib.SOURCE_REMOVE;
                    this._stateRetry = 0;
                    this._refresh();
                    return GLib.SOURCE_REMOVE;
                });
            return;
        }
        if (!this._state)
            this._setConnectionState('disconnected');
        this._showError(error);
    }

    _disconnected() {
        this._generation++;
        this._cancelStateRetry();
        if (this._bus && this._signal)
            this._bus.signal_unsubscribe(this._signal);
        if (this._bus && this._timerSignal)
            this._bus.signal_unsubscribe(this._timerSignal);
        this._bus = null;
        this._owner = '';
        this._runtimeRegistrationStarted = false;
        this._signal = 0;
        this._timerSignal = 0;
        this._state = null;
        this._actionError = '';
        this._setConnectionState('disconnected');
    }

    _setConnectionState(connectionState) {
        if (!this._button)
            return;
        this._connectionState = connectionState;
        this._status.label.text = connectionState === 'connecting' ? 'Connecting…' :
            connectionState === 'retrying' ? 'Connecting… retrying' :
            connectionState === 'connected' ? 'Connected' : 'Agent not connected';
        for (const item of [this._prevention, this._lid, this._dimming, this._failsafe, this._cancel, ...this._timerItems])
            item.setSensitive(false);
        this._icon.icon_name = connectionState === 'connected' ?
            'weather-clear-night-symbolic' : 'dialog-warning-symbolic';
    }

    _render(state) {
        if (!this._button)
            return;
        if (this._connectionState !== 'connected')
            return;
        this._state = state;
        const on = Boolean(value(state, 'enabled', false));
        const desired = Boolean(value(state, 'desired', false));
        const releasePending = Boolean(value(state, 'gnomeReleasePending', false));
        const error = String(value(state, 'error', ''));
        const remaining = Number(value(state, 'timerRemaining', 0));
        const battery = Number(value(state, 'batteryPercent', -1));
        const parts = [releasePending ? 'Prevention release pending' :
            on ? 'Prevention on' : desired ? 'Reconnecting…' : 'Prevention off'];
        if (remaining > 0)
            parts.push(`sleep in ${Math.ceil(remaining / 60)} min`);
        if (battery >= 0)
            parts.push(`battery ${Math.round(battery)}%`);
        if (error)
            parts.push(error);
        if (this._actionError)
            parts.push(this._actionError);
        const lidRequested = Boolean(value(state, 'lidRequested', false));
        const lidAcquired = Boolean(value(state, 'lidLockAcquired', false));
        const lidOutcome = String(value(state, 'lidOutcome', 'unverified'));
        if (lidRequested)
            parts.push(lidAcquired ? `lid lock acquired (${lidOutcome})` : `lid lock missing (${lidOutcome})`);
        const lidError = String(value(state, 'lidError', ''));
        if (lidRequested && lidError && lidError !== error)
            parts.push(lidError);
        const brightnessError = String(value(state, 'brightnessError', ''));
        if (Boolean(value(state, 'brightnessRecoveryPending', false)))
            parts.push('brightness recovery pending');
        else if (String(value(state, 'brightnessState', '')) === 'dimmed-owned')
            parts.push('panel dimmed');
        else if (brightnessError && Boolean(value(state, 'lidDimming', false)))
            parts.push(brightnessError);
        const timerOutcome = String(value(state, 'timerOutcome', ''));
        if (timerOutcome)
            parts.push(timerOutcome);
        this._status.label.text = parts.join(' · ');
        this._icon.icon_name = releasePending || desired && !on ? 'dialog-warning-symbolic' :
            on ? 'media-playback-pause-symbolic' : 'weather-clear-night-symbolic';
        this._updating = true;
        this._prevention.setToggleState(desired);
        this._lid.setToggleState(lidRequested);
        this._dimming.setToggleState(Boolean(value(state, 'lidDimming', false)));
        this._failsafe.setToggleState(Boolean(value(state, 'failsafe', false)));
        this._updating = false;
        for (const item of [this._prevention, this._lid, this._failsafe, ...this._timerItems])
            item.setSensitive(true);
        this._prevention.setSensitive(!releasePending);
        this._dimming.setSensitive(Boolean(value(state, 'brightnessAvailable', false)) ||
            Boolean(value(state, 'lidDimming', false)));
        this._cancel.setSensitive(String(value(state, 'timerPhase', 'idle')) === 'running');
    }

    _call(method, parameters) {
        if (!this._bus || !this._button || this._connectionState !== 'connected')
            return;
        const generation = this._generation;
        try {
            this._bus.call(this._owner, PATH, IFACE, method, parameters, null,
                Gio.DBusCallFlags.NONE, 10000, this._cancellable, (bus, result) => {
                    if (!this._button || generation !== this._generation)
                        return;
                    try {
                        bus.call_finish(result);
                        this._actionError = '';
                        this._refresh();
                    } catch (error) {
                        this._showError(error);
                        // A rejected toggle should return to the authoritative state.
                        this._refresh();
                    }
                });
        } catch (error) {
            this._showError(error);
            this._refresh();
        }
    }

    _showError(error) {
        this._actionError = `Sleep Disabler error: ${error.message}`;
        if (this._state)
            this._render(this._state);
        else if (this._status)
            this._status.label.text = this._actionError;
    }

    disable() {
        if (this._bus && this._owner && this._runtimeRegistrationStarted) {
            try {
                this._bus.call(this._owner, PATH, IFACE, 'UnregisterPanelRuntime',
                    new GLib.Variant('(s)', [PANEL_RUNTIME_VERSION]), null,
                    Gio.DBusCallFlags.NONE, 5000, null, (bus, result) => {
                        try {
                            bus.call_finish(result);
                        } catch (_error) {
                            // The agent may already be gone.
                        }
                    });
            } catch (_error) {
                // Even a synchronous send failure must not prevent local cleanup.
            }
        }
        this._generation++;
        this._cancelStateRetry();
        this._cancellable?.cancel();
        if (this._watch)
            Gio.bus_unwatch_name(this._watch);
        if (this._bus && this._signal)
            this._bus.signal_unsubscribe(this._signal);
        if (this._bus && this._timerSignal)
            this._bus.signal_unsubscribe(this._timerSignal);
        this._button?.destroy();
        this._button = null;
        this._bus = null;
        this._owner = '';
        this._runtimeRegistrationStarted = false;
        this._state = null;
        this._actionError = '';
        this._cancellable = null;
        this._watch = 0;
        this._signal = 0;
        this._timerSignal = 0;
        this._connectionState = 'disconnected';
    }
}
