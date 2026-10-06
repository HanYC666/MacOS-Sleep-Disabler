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
        this._generation = 0;
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
        this._setConnected(false);
        this._watch = Gio.bus_watch_name(
            Gio.BusType.SESSION, APP, Gio.BusNameWatcherFlags.NONE,
            (connection) => this._connected(connection),
            () => this._disconnected()
        );
    }

    _connected(connection) {
        if (!this._button)
            return;
        this._generation++;
        if (this._bus && this._signal)
            this._bus.signal_unsubscribe(this._signal);
        if (this._bus && this._timerSignal)
            this._bus.signal_unsubscribe(this._timerSignal);
        this._bus = connection;
        this._state = null;
        this._actionError = '';
        const generation = this._generation;
        this._signal = connection.signal_subscribe(
            APP, IFACE, 'StateChanged', PATH, null,
            Gio.DBusSignalFlags.NONE,
            (_bus, _sender, _path, _iface, _signal, parameters) => {
                if (!this._button || generation !== this._generation)
                    return;
                const [state] = parameters.deep_unpack();
                this._render(state);
            }
        );
        this._timerSignal = connection.signal_subscribe(
            APP, IFACE, 'TimerChanged', PATH, null,
            Gio.DBusSignalFlags.NONE,
            (_bus, _sender, _path, _iface, _signal, parameters) => {
                if (!this._button || generation !== this._generation || !this._state)
                    return;
                const [remaining] = parameters.deep_unpack();
                this._state.timerRemaining = new GLib.Variant('t', remaining);
                this._render(this._state);
            }
        );
        this._setConnected(false);
        this._status.label.text = 'Connecting…';
        this._refresh();
    }

    _refresh() {
        if (!this._bus || !this._button)
            return;
        const generation = this._generation;
        this._bus.call(APP, PATH, IFACE, 'GetState', new GLib.Variant('()'),
            new GLib.VariantType('(a{sv})'), Gio.DBusCallFlags.NONE, 5000,
            this._cancellable, (bus, result) => {
                if (!this._button || generation !== this._generation)
                    return;
                try {
                    const [state] = bus.call_finish(result).deep_unpack();
                    this._render(state);
                } catch (error) {
                    this._setConnected(false);
                    this._showError(error);
                }
            });
    }

    _disconnected() {
        this._generation++;
        if (this._bus && this._signal)
            this._bus.signal_unsubscribe(this._signal);
        if (this._bus && this._timerSignal)
            this._bus.signal_unsubscribe(this._timerSignal);
        this._bus = null;
        this._signal = 0;
        this._timerSignal = 0;
        this._state = null;
        this._actionError = '';
        this._setConnected(false);
    }

    _setConnected(connected) {
        if (!this._button)
            return;
        this._status.label.text = connected ? 'Connecting…' : 'Agent not connected';
        for (const item of [this._prevention, this._lid, this._dimming, this._failsafe, this._cancel, ...this._timerItems])
            item.setSensitive(connected);
        this._icon.icon_name = connected ? 'weather-clear-night-symbolic' : 'dialog-warning-symbolic';
    }

    _render(state) {
        if (!this._button)
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
        const brightnessError = String(value(state, 'brightnessError', ''));
        if (Boolean(value(state, 'brightnessRecoveryPending', false)))
            parts.push('brightness recovery pending');
        else if (brightnessError && Boolean(value(state, 'lidDimming', false)))
            parts.push(brightnessError);
        const timerOutcome = String(value(state, 'timerOutcome', ''));
        if (timerOutcome)
            parts.push(timerOutcome);
        this._status.label.text = parts.join(' · ');
        this._icon.icon_name = releasePending || desired && !on ? 'dialog-warning-symbolic' :
            on ? 'media-playback-pause-symbolic' : 'weather-clear-night-symbolic';
        this._updating = true;
        this._prevention.setToggleState(on);
        this._lid.setToggleState(lidRequested);
        this._dimming.setToggleState(Boolean(value(state, 'lidDimming', false)));
        this._failsafe.setToggleState(Boolean(value(state, 'failsafe', false)));
        this._updating = false;
        for (const item of [this._prevention, this._lid, this._failsafe, ...this._timerItems])
            item.setSensitive(true);
        this._prevention.setSensitive(!releasePending);
        this._dimming.setSensitive(Boolean(value(state, 'brightnessAvailable', false)) ||
            Boolean(value(state, 'lidDimming', false)));
        this._cancel.setSensitive(remaining > 0);
    }

    _call(method, parameters) {
        if (!this._bus || !this._button)
            return;
        const generation = this._generation;
        this._bus.call(APP, PATH, IFACE, method, parameters, null,
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
    }

    _showError(error) {
        this._actionError = `Sleep Disabler error: ${error.message}`;
        if (this._state)
            this._render(this._state);
        else if (this._status)
            this._status.label.text = this._actionError;
    }

    disable() {
        this._generation++;
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
        this._state = null;
        this._actionError = '';
        this._cancellable = null;
        this._watch = 0;
        this._signal = 0;
        this._timerSignal = 0;
    }
}
