// Local panel-state harness. GNOME Shell lifecycle still needs a live session.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../extension/sleep-disabler@local/extension.js', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '')
    .replace('export default class SleepDisablerExtension', 'class SleepDisablerExtension') +
    '\nglobalThis.SleepDisablerExtension = SleepDisablerExtension;';
class Variant {
    constructor(_type, unpacked) { this.unpacked = unpacked; }
    deep_unpack() { return this.unpacked; }
}
const context = {
    Extension: class {},
    Gio: {
        DBusCallFlags: {NONE: 0},
        DBusSignalFlags: {NONE: 0},
    },
    GLib: {Variant, VariantType: class {}, PRIORITY_DEFAULT: 0, SOURCE_REMOVE: false,
        timeout_add_seconds(_priority, seconds, callback) {
            const source = ++sourceId;
            sources.set(source, {seconds, callback});
            return source;
        },
        source_remove(source) { sources.delete(source); },
    },
};
let sourceId = 0;
const sources = new Map();
vm.createContext(context);
vm.runInContext(source, context);

const item = () => ({
    toggle: false,
    sensitive: null,
    setToggleState(value) { this.toggle = value; },
    setSensitive(value) { this.sensitive = value; },
});
// Minimal Shell UI lifecycle to exercise the real enable() event handlers.
class MenuItem {
    constructor() { Object.assign(this, item()); this.handlers = {}; this.label = {text: ''}; }
    connect(event, callback) { this.handlers[event] = callback; }
}
context.Gio.Cancellable = class {
    cancel() { this.cancelled = true; }
    is_cancelled() { return this.cancelled === true; }
};
context.Gio.BusType = {SESSION: 0};
context.Gio.BusNameWatcherFlags = {NONE: 0};
context.Gio.bus_watch_name = () => 1;
context.Gio.bus_unwatch_name = () => {};
context.PanelMenu = {Button: class {
    constructor() { this.menu = {addMenuItem() {}}; }
    add_child() {}
    destroy() {}
}};
context.PopupMenu = {PopupMenuItem: MenuItem, PopupSwitchMenuItem: MenuItem,
    PopupSeparatorMenuItem: class {}};
context.St = {Icon: class {}};
context.Main = {panel: {addToStatusArea() {}}};
function makePanel() {
    const panel = new context.SleepDisablerExtension();
    panel._button = {destroy() {}};
    panel._status = {label: {text: ''}};
    panel._icon = {};
    panel._prevention = item();
    panel._lid = item();
    panel._dimming = item();
    panel._failsafe = item();
    panel._cancel = item();
    panel._timerItems = [item(), item()];
    panel._generation = 0;
    panel._owner = '';
    panel._stateSerial = 0;
    panel._stateAttempts = 0;
    panel._stateRetry = 0;
    panel._runtimeRegistrationStarted = false;
    panel._cancellable = {cancel() {}};
    panel._state = null;
    panel._actionError = '';
    panel._updating = false;
    panel._bus = null;
    panel._signal = 0;
    panel._timerSignal = 0;
    panel._connectionState = 'disconnected';
    return panel;
}
const state = {
    enabled: true,
    desired: true,
    lidDimming: true,
    brightnessAvailable: false,
    brightnessRecoveryPending: true,
    lidRequested: true,
    lidLockAcquired: true,
    lidOutcome: 'unverified',
    timerRemaining: 120,
    timerPhase: 'running',
};

const panel = makePanel();
panel._setConnectionState('disconnected');
assert.equal(panel._connectionState, 'disconnected');
assert.equal(panel._prevention.sensitive, false);
assert.equal(panel._icon.icon_name, 'dialog-warning-symbolic');

const calls = [];
const subscriptions = [];
const connection = {
    signal_subscribe(_app, _iface, signal, _path, _arg, _flags, callback) {
        subscriptions.push({signal, callback});
        return subscriptions.length;
    },
    signal_unsubscribe() {},
    call(...args) { calls.push(args); },
    call_finish(result) { return result; },
};
panel._connected(connection);
assert.equal(panel._connectionState, 'connecting');
assert.equal(panel._prevention.sensitive, false);
assert.equal(panel._status.label.text, 'Connecting…');
assert.equal(calls.length, 2, 'connecting requests state and registers the runtime');
assert.equal(calls[1][3], 'RegisterPanelRuntime');
assert.equal(calls[1][4].deep_unpack()[0], '6');

// A current-generation StateChanged may establish connection before GetState.
const stateSubscription = subscriptions.find(entry => entry.signal === 'StateChanged');
stateSubscription.callback(null, null, null, null, null,
    new Variant('', [state]));
assert.equal(panel._connectionState, 'connected');
assert.equal(panel._dimming.sensitive, true, 'enabled dimming can be switched off');
assert.equal(panel._cancel.sensitive, true);
assert.match(panel._status.label.text, /brightness recovery pending/);

// The older initial GetState failure cannot undo an authoritative early signal.
connection.call_finish = () => { throw new Error('older initial call failed'); };
calls[0][9](connection, {});
assert.equal(panel._connectionState, 'connected');
assert.equal(panel._prevention.sensitive, true);
assert.doesNotMatch(panel._status.label.text, /older initial call failed/);

// A stale GetState callback from the prior generation cannot reconnect controls.
const staleReply = calls[0][9];
panel._disconnected();
staleReply(connection, new Variant('', [state]));
assert.equal(panel._connectionState, 'disconnected');
assert.equal(panel._prevention.sensitive, false);
stateSubscription.callback(null, null, null, null, null,
    new Variant('', [state]));
assert.equal(panel._connectionState, 'disconnected',
    'stale StateChanged cannot reconnect controls');

// Authoritative GetState establishes connected state.
const panel2 = makePanel();
const calls2 = [];
const connection2 = {
    signal_subscribe() { return 1; },
    signal_unsubscribe() {},
    call(...args) { calls2.push(args); },
    call_finish() { return new Variant('', [state]); },
};
panel2._connected(connection2);
calls2[0][9](connection2, {});
assert.equal(panel2._connectionState, 'connected');
assert.equal(panel2._prevention.sensitive, true);

// Initial GetState failure schedules exactly two bounded retries.
const panel3 = makePanel();
const calls3 = [];
const signals3 = [];
const connection3 = {
    signal_subscribe(_name, _iface, signal, _path, _arg, _flags, callback) {
        signals3.push({signal, callback});
        return signals3.length;
    },
    signal_unsubscribe() {},
    call(...args) { calls3.push(args); },
    call_finish() { throw new Error('initial state failed'); },
};
panel3._connected(connection3);
calls3[0][9](connection3, {});
assert.equal(panel3._connectionState, 'retrying');
assert.equal(sources.get(panel3._stateRetry).seconds, 1);
let retry = sources.get(panel3._stateRetry).callback;
sources.delete(panel3._stateRetry);
retry();
calls3[2][9](connection3, {});
assert.equal(panel3._connectionState, 'retrying');
assert.equal(sources.get(panel3._stateRetry).seconds, 2);
retry = sources.get(panel3._stateRetry).callback;
sources.delete(panel3._stateRetry);
retry();
calls3[3][9](connection3, {});
assert.equal(panel3._connectionState, 'disconnected');
assert.equal(panel3._prevention.sensitive, false);
assert.match(panel3._status.label.text, /initial state failed/);

// An action error remains visible after the authoritative refresh.
const panel4 = makePanel();
panel4._bus = {};
panel4._setConnectionState('connected');
panel4._render(state);
let phase = 'action';
panel4._bus.call = (...args) => args[9](panel4._bus, {});
panel4._bus.call_finish = () => {
    if (phase === 'action') {
        phase = 'refresh';
        throw new Error('preference rejected');
    }
    return new Variant('', [state]);
};
panel4._call('SetLidDimming', {});
assert.equal(panel4._connectionState, 'connected');
assert.match(panel4._status.label.text, /preference rejected/);
assert.equal(panel4._dimming.toggle, true);

panel4._render({enabled: false, desired: false, gnomeReleasePending: true});
assert.match(panel4._status.label.text, /release pending/);
assert.equal(panel4._prevention.sensitive, false);
assert.equal(panel4._icon.icon_name, 'dialog-warning-symbolic');

panel4._render({enabled: false, desired: true, timerRemaining: 0, timerPhase: 'running'});
assert.equal(panel4._prevention.toggle, true);
assert.equal(panel4._prevention.sensitive, true);
assert.equal(panel4._cancel.sensitive, true);
assert.match(panel4._status.label.text, /Reconnecting/);
panel4._render({desired: false, timerRemaining: 0, timerPhase: 'consumed'});
assert.equal(panel4._cancel.sensitive, false);
panel4._render({desired: true, enabled: true, brightnessState: 'dimmed-owned',
    brightnessRecoveryPending: false});
assert.match(panel4._status.label.text, /panel dimmed/);
assert.doesNotMatch(panel4._status.label.text, /recovery pending/);

// Success, signals, owner changes, and disable all cancel outstanding retries.
for (const recovery of ['reply', 'signal', 'disconnect', 'disable']) {
    const p = makePanel();
    const pending = [];
    const signals = [];
    let fail = true;
    const bus = {
        signal_subscribe(_name, _iface, signal, _path, _arg, _flags, callback) {
            signals.push({signal, callback});
            return signals.length;
        },
        signal_unsubscribe() {},
        call(...args) { pending.push(args); },
        call_finish() {
            if (fail)
                throw new Error('temporarily unavailable');
            return new Variant('', [state]);
        },
    };
    p._connected(bus, ':1.owner');
    pending[0][9](bus, {});
    const oldRetry = sources.get(p._stateRetry).callback;
    assert.equal(sources.size, 1);
    if (recovery === 'reply') {
        sources.delete(p._stateRetry);
        oldRetry();
        fail = false;
        pending[2][9](bus, {});
        assert.equal(p._connectionState, 'connected');
    } else if (recovery === 'signal') {
        signals[0].callback(null, null, null, null, null, new Variant('', [state]));
        assert.equal(p._connectionState, 'connected');
    } else if (recovery === 'disconnect') {
        p._disconnected();
        const count = pending.length;
        oldRetry();
        assert.equal(pending.length, count, 'old retry cannot call a newer owner');
    } else {
        p.disable();
        assert.equal(pending.at(-1)[3], 'UnregisterPanelRuntime');
        assert.equal(pending.at(-1)[0], ':1.owner');
        assert.equal(p._button, null);
    }
    assert.equal(sources.size, 0);
}

// An old successful snapshot cannot overwrite a newer authoritative signal.
connection2.call_finish = () => new Variant('', [{desired: false, enabled: false}]);
panel2._stateSerial++;
calls2[0][9](connection2, {});
assert.equal(panel2._prevention.toggle, true);

// Exhausted bootstrap retries still allow a late same-owner state signal.
signals3.find(entry => entry.signal === 'StateChanged').callback(
    null, null, null, null, null, new Variant('', [state]));
assert.equal(panel3._prevention.sensitive, true);

// Invoke the actual toggled handler: requested-but-ineffective prevention cancels.
const lifecycle = new context.SleepDisablerExtension();
lifecycle.enable();
const lifecycleCalls = [];
const lifecycleSignals = [];
const lifecycleBus = {
    signal_subscribe(_name, _iface, signal, _path, _arg, _flags, callback) {
        lifecycleSignals.push({signal, callback});
        return lifecycleSignals.length;
    },
    signal_unsubscribe() {},
    call(...args) { lifecycleCalls.push(args); },
    call_finish(result) { return result; },
};
lifecycle._connected(lifecycleBus, ':1.first');
const oldReply = lifecycleCalls[0][9];
const oldSignal = lifecycleSignals[0].callback;
oldReply(lifecycleBus, new Variant('', [{desired: true, enabled: false}]));
lifecycle._prevention.handlers.toggled(lifecycle._prevention, false);
assert.equal(lifecycleCalls.at(-1)[3], 'SetPrevention');
assert.equal(lifecycleCalls.at(-1)[4].deep_unpack()[0], false);
assert.equal(lifecycleCalls.at(-1)[0], ':1.first');
const firstGeneration = lifecycle._generation;
lifecycle.disable();
lifecycle.enable();
lifecycle._connected(lifecycleBus, ':1.second');
assert.ok(lifecycle._generation > firstGeneration);
assert.equal(lifecycleCalls.at(-1)[3], 'RegisterPanelRuntime');
assert.equal(lifecycleCalls.at(-1)[0], ':1.second');
oldReply(lifecycleBus, new Variant('', [state]));
oldSignal(null, null, null, null, null, new Variant('', [state]));
assert.equal(lifecycle._connectionState, 'connecting');
assert.equal(lifecycle._prevention.sensitive, false);
lifecycleCalls.at(-2)[9](lifecycleBus, new Variant('', [{desired: false, enabled: false,
    lidRequested: false, lidError: 'obsolete lid error', error: 'persistence failure'}]));
assert.match(lifecycle._status.label.text, /persistence failure/);
assert.doesNotMatch(lifecycle._status.label.text, /obsolete lid error/);
lifecycle._render({desired: false, lidRequested: true, lidError: 'lid denied'});
assert.match(lifecycle._status.label.text, /lid denied/);
lifecycle._render({desired: false, lidRequested: false, lidError: ''});
assert.doesNotMatch(lifecycle._status.label.text, /lid denied/);
for (const timerPhase of ['idle', 'consumed', 'canceled']) {
    lifecycle._render({desired: false, timerPhase, timerRemaining: 0});
    assert.equal(lifecycle._cancel.sensitive, false);
}
// A newer countdown signal supersedes an in-flight older snapshot.
lifecycle._render({desired: false, timerPhase: 'running', timerRemaining: 120});
lifecycle._refresh();
const olderCountdownReply = lifecycleCalls.at(-1)[9];
lifecycleSignals.at(-1).callback(null, null, null, null, null, new Variant('', [0]));
olderCountdownReply(lifecycleBus, new Variant('', [{desired: false,
    timerPhase: 'running', timerRemaining: 120}]));
assert.equal(lifecycle._state.timerRemaining.deep_unpack(), 0);
assert.equal(lifecycle._cancel.sensitive, true);

// Local teardown must finish if unregister throws before scheduling a callback.
lifecycleBus.call = () => { throw new Error('bus already closed'); };
lifecycle.disable();
assert.equal(lifecycle._button, null);
assert.equal(lifecycle._bus, null);
assert.equal(sources.size, 0);

// A synchronous send failure follows the same bounded bootstrap policy.
const synchronous = makePanel();
let synchronousStateCalls = 0;
const closedBus = {
    signal_subscribe() { return 1; },
    signal_unsubscribe() {},
    call(...args) {
        if (args[3] === 'GetState')
            synchronousStateCalls++;
        throw new Error('synchronous connection failure');
    },
};
assert.doesNotThrow(() => synchronous._connected(closedBus, ':1.closed'));
for (let attempt = 0; attempt < 2; attempt++) {
    const retry = synchronous._stateRetry;
    const callback = sources.get(retry).callback;
    sources.delete(retry);
    callback();
}
assert.equal(synchronousStateCalls, 3);
assert.equal(synchronous._connectionState, 'disconnected');
assert.equal(sources.size, 0);
synchronous._setConnectionState('connected');
synchronous._render({desired: true, enabled: false});
assert.doesNotThrow(() => synchronous._call('SetPrevention', new Variant('(b)', [false])));
assert.equal(synchronous._prevention.toggle, true);
assert.match(synchronous._status.label.text, /synchronous connection failure/);
synchronous.disable();

// A newer request supersedes an older same-owner reply without any signal.
const overlapping = makePanel();
const overlapCalls = [];
const overlapBus = {...connection, call(...args) { overlapCalls.push(args); },
    call_finish(result) { return result; }};
overlapping._connected(overlapBus, ':1.overlap');
const initialOverlapReply = overlapCalls.find(call => call[3] === 'GetState')[9];
initialOverlapReply(overlapBus, new Variant('', [{desired: true, enabled: true}]));
overlapping._refresh();
const olderOverlapReply = overlapCalls.at(-1)[9];
overlapping._refresh();
const newerOverlapReply = overlapCalls.at(-1)[9];
newerOverlapReply(overlapBus, new Variant('', [{desired: false, enabled: false}]));
olderOverlapReply(overlapBus, new Variant('', [{desired: true, enabled: true}]));
assert.equal(overlapping._prevention.toggle, false);
assert.equal(overlapping._state.desired, false);
overlapping._refresh();
const obsoleteError = overlapCalls.at(-1)[9];
overlapping._refresh();
overlapCalls.at(-1)[9](overlapBus, new Variant('', [{desired: true, enabled: false}]));
obsoleteError({...overlapBus, call_finish() { throw new Error('obsolete failure'); }}, null);
assert.equal(overlapping._prevention.toggle, true);
assert.doesNotMatch(overlapping._status.label.text, /obsolete failure/);
overlapping.disable();
assert.equal(sources.size, 0);

console.log('Panel connection-state harness passed');
