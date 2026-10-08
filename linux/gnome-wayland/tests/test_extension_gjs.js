// Run callback/error cases without scheduling:
// SLEEP_DISABLER_NO_SCHEDULING=1 gjs -m tests/test_extension_gjs.js
// Uses only fake UI and D-Bus objects; no session service or sleep call.
import GLib from 'gi://GLib';
const NO_SCHEDULING = GLib.getenv('SLEEP_DISABLER_NO_SCHEDULING') === '1';

function check(condition, message) {
    if (!condition)
        throw new Error(message);
}

const extensionPath = GLib.build_filenamev([
    GLib.get_current_dir(), 'extension', 'sleep-disabler@local', 'extension.js']);
const [readable, bytes] = GLib.file_get_contents(extensionPath);
check(readable, 'extension source is readable');
const source = new TextDecoder().decode(bytes)
    .replace(/^import .*;\n/gm, '')
    .replace('export default class SleepDisablerExtension', 'class SleepDisablerExtension');

class Item {
    constructor(label = '') {
        this.label = {text: label};
        this.handlers = {};
    }
    connect(signal, callback) { this.handlers[signal] = callback; }
    setSensitive(value) { this.sensitive = value; }
    setToggleState(value) { this.toggle = value; }
}

const watches = new Map();
let nextWatch = 1;
const GioFake = {
    Cancellable: class {
        cancel() { this.cancelled = true; }
        is_cancelled() { return this.cancelled === true; }
    },
    BusType: {SESSION: 0},
    BusNameWatcherFlags: {NONE: 0},
    DBusCallFlags: {NONE: 0},
    DBusSignalFlags: {NONE: 0},
    bus_watch_name(_type, _name, _flags, appeared, vanished) {
        const id = nextWatch++;
        watches.set(id, {appeared, vanished});
        return id;
    },
    bus_unwatch_name(id) { watches.delete(id); },
};
const timers = new Map();
let nextTimer = 1;
function fireTimer(id) {
    const timer = timers.get(id);
    check(timer !== undefined, `timer ${id} exists`);
    timers.delete(id);
    timer.callback();
}
const GLibFake = {
    Variant: GLib.Variant,
    VariantType: GLib.VariantType,
    PRIORITY_DEFAULT: 0,
    SOURCE_REMOVE: false,
    timeout_add_seconds(_priority, delay, callback) {
        check(!NO_SCHEDULING, 'no-scheduling mode cannot attach retry timers');
        const id = nextTimer++;
        timers.set(id, {delay, callback});
        return id;
    },
    source_remove(id) { timers.delete(id); },
};
const PanelMenu = {Button: class {
    constructor() { this.menu = {addMenuItem() {}}; }
    add_child() {}
    destroy() {}
}};
const PopupMenu = {PopupMenuItem: Item, PopupSwitchMenuItem: Item,
    PopupSeparatorMenuItem: class {}};
const St = {Icon: class {}};
const Main = {panel: {addToStatusArea() {}}};
const ExtensionClass = new Function('Gio', 'GLib', 'St', 'Extension', 'Main',
    'PanelMenu', 'PopupMenu', `${source}\nreturn SleepDisablerExtension;`)(
        GioFake, GLibFake, St, class {}, Main, PanelMenu, PopupMenu);

const calls = [];
const signals = new Map();
const bus = {
    signal_subscribe(_owner, _iface, name, _path, _arg, _flags, callback) {
        signals.set(name, callback);
        return signals.size;
    },
    signal_unsubscribe() {},
    call(...args) {
        calls.push(args);
        if (this.failNext === args[3]) {
            this.failNext = '';
            throw new Error(`send failed: ${args[3]}`);
        }
    },
    call_finish(result) {
        if (result?.error)
            throw new Error(result.error);
        return result;
    },
};
const panel = new ExtensionClass();
panel.enable();
panel._connected(bus, ':1.agent');
check(calls.length === 2, 'connection requests state and registration');
check(calls[0][7] === 3000, 'status call uses the three-second bound');
check(calls[1][3] === 'RegisterPanelRuntime', 'runtime registration method');
check(calls[1][4].deep_unpack()[0] === '6', 'current runtime version');

const state = {enabled: true, desired: true, timerRemaining: 0};
calls[0][9](bus, {deep_unpack: () => [state]});
check(panel._connectionState === 'connected', 'authoritative state connects panel');
panel._showError(new Error('action failed'));
panel._showError(new Error('registration failed'), 'registration');
panel._showError(new Error('connection failed'), 'connection');
check(panel._status.label.text.includes('action failed') &&
    panel._status.label.text.includes('registration failed') &&
    panel._status.label.text.includes('connection failed'), 'independent errors appear');
if (!NO_SCHEDULING) {
    signals.get('TimerChanged')(null, null, null, null, null,
        {deep_unpack: () => [60]});
    check(panel._status.label.text.includes('connection failed'),
        'timer-only signal cannot clear connection failure');
}

signals.get('StateChanged')(null, null, null, null, null,
    {deep_unpack: () => [state]});
check(!panel._status.label.text.includes('connection failed'),
    'authoritative state clears connection error');
check(panel._status.label.text.includes('action failed') &&
    panel._status.label.text.includes('registration failed'),
    'state preserves other errors');
const diagnosticState = {...state, error: 'agent diagnostic',
    lidRequested: true, lidError: 'lid diagnostic',
    brightnessRecoveryPending: true, timerOutcome: 'timer diagnostic'};
signals.get('StateChanged')(null, null, null, null, null,
    {deep_unpack: () => [diagnosticState]});
check(['agent diagnostic', 'lid diagnostic', 'brightness recovery pending',
    'timer diagnostic', 'action failed', 'registration failed'].every(
        message => panel._status.label.text.includes(message)),
    'agent diagnostics and independent panel errors remain visible together');

calls[1][9](bus, {});
check(panel._registrationState === 'succeeded', 'registration succeeds');
check(!panel._status.label.text.includes('registration failed') &&
    panel._status.label.text.includes('action failed'),
    'registration success clears only registration error');

panel._call('SetLidDimming', new GLib.Variant('(b)', [true]));
const oldAction = calls.at(-1);
panel._call('SetFailsafe', new GLib.Variant('(bu)', [true, 20]));
const newAction = calls.at(-1);
newAction[9](bus, {error: 'new action failed'});
check(panel._status.label.text.includes('new action failed'), 'new failure appears');
oldAction[9](bus, {});
check(panel._status.label.text.includes('new action failed'),
    'old success cannot erase newer failure');
panel._call('SetLidDimming', new GLib.Variant('(b)', [false]));
calls.at(-1)[9](bus, {});
check(!panel._status.label.text.includes('new action failed'),
    'newest action success clears the superseded action failure');
panel._call('SetLidDimming', new GLib.Variant('(b)', [true]));
const olderFailure = calls.at(-1);
panel._call('SetLidDimming', new GLib.Variant('(b)', [false]));
calls.at(-1)[9](bus, {});
olderFailure[9](bus, {error: 'stale action failure'});
check(!panel._status.label.text.includes('stale action failure'),
    'old failure cannot replace the newest successful action');

if (!NO_SCHEDULING) {
    panel._cancel.handlers.activate();
    check(calls.at(-1)[3] === 'CancelTimer' &&
        calls.at(-1)[4].deep_unpack().length === 0,
        'CancelTimer uses a valid empty D-Bus tuple');
}

panel.disable();
check(panel._button === null, 'disable tears down panel');
check(calls.at(-1)[3] === 'UnregisterPanelRuntime', 'disable unregisters runtime');

const second = new ExtensionClass();
second.enable();
second._connected(bus, ':1.old');
const pendingRegistration = calls.at(-1);
second._connected(bus, ':1.new');
const newRegistration = calls.at(-1);
pendingRegistration[9](bus, {});
check(second._registrationState === 'pending',
    'old owner registration reply cannot complete new registration');
newRegistration[9](bus, {});
check(second._registrationState === 'succeeded', 'new owner registration succeeds');
second.disable();
newRegistration[9](bus, {error: 'late error'});
check(second._registrationState === 'idle',
    'late registration reply cannot alter disabled lifecycle');

const cancelled = new ExtensionClass();
cancelled.enable();
cancelled._connected(bus, ':1.cancelled');
const cancelledRegistration = calls.at(-1);
cancelled._cancellable.cancel();
cancelledRegistration[9](bus, {});
check(cancelled._registrationState === 'pending',
    'cancelled registration reply cannot claim success');
cancelled.disable();

const watched = new ExtensionClass();
watched.enable();
const oldWatch = watches.get(watched._watch);
watched.disable();
watched.enable();
const currentWatch = watches.get(watched._watch);
const callsBeforeOldWatch = calls.length;
oldWatch.appeared(bus, 'org.sleepdisabler.App', ':1.stale');
oldWatch.vanished(bus, 'org.sleepdisabler.App');
check(calls.length === callsBeforeOldWatch && watched._bus === null,
    'queued watcher callbacks from a disabled panel cannot attach an old owner');
currentWatch.appeared(bus, 'org.sleepdisabler.App', ':1.current');
check(watched._owner === ':1.current', 'new watcher attaches the current owner');
oldWatch.vanished(bus, 'org.sleepdisabler.App');
oldWatch.appeared(bus, 'org.sleepdisabler.App', ':1.stale');
check(watched._owner === ':1.current' && watched._bus === bus,
    'old watcher callbacks cannot disconnect or replace the new owner');
watched.disable();

if (!NO_SCHEDULING) {
const retried = new ExtensionClass();
retried.enable();
bus.failNext = 'RegisterPanelRuntime';
retried._connected(bus, ':1.retry');
check(retried._registrationState === 'idle' && retried._registrationAttempts === 1,
    'synchronous registration send error schedules first retry');
check(timers.get(retried._registrationRetry)?.delay === 1, 'first retry waits one second');
fireTimer(retried._registrationRetry);
const secondAttempt = calls.at(-1);
check(secondAttempt[3] === 'RegisterPanelRuntime' && retried._registrationAttempts === 2,
    'second registration attempt sent');
secondAttempt[9](bus, {error: 'asynchronous registration failure'});
check(timers.get(retried._registrationRetry)?.delay === 2, 'second retry waits two seconds');
fireTimer(retried._registrationRetry);
const thirdAttempt = calls.at(-1);
check(thirdAttempt[3] === 'RegisterPanelRuntime' && retried._registrationAttempts === 3,
    'third and final registration attempt sent');
thirdAttempt[9](bus, {});
check(retried._registrationState === 'succeeded' && retried._registrationRetry === 0,
    'registration retry success is terminal');
retried.disable();

const exhausted = new ExtensionClass();
exhausted.enable();
exhausted._connected(bus, ':1.exhausted');
for (let attempt = 1; attempt <= 3; attempt++) {
    const pending = calls.at(-1);
    check(pending[3] === 'RegisterPanelRuntime', 'registration attempt is pending');
    pending[9](bus, {error: 'registration timed out'});
    pending[9](bus, {});
    check(exhausted._registrationState === (attempt < 3 ? 'idle' : 'exhausted'),
        'a late success cannot revive a settled registration attempt');
    if (attempt < 3)
        fireTimer(exhausted._registrationRetry);
}
check(exhausted._registrationState === 'exhausted' &&
    exhausted._status.label.text.includes('registration timed out'),
    'three failures exhaust registration and display its error');
const exhaustedState = calls.findLast(call => call[3] === 'GetState' && call[0] === ':1.exhausted');
exhaustedState[9](bus, {deep_unpack: () => [state]});
check(exhausted._connectionState === 'connected' &&
    exhausted._status.label.text.includes('registration timed out'),
    'authoritative state recovers connection without erasing registration error');
exhausted.disable();

const changedDuringRetry = new ExtensionClass();
changedDuringRetry.enable();
changedDuringRetry._connected(bus, ':1.before');
calls.at(-1)[9](bus, {error: 'first owner failed'});
const staleRetry = timers.get(changedDuringRetry._registrationRetry).callback;
changedDuringRetry._connected(bus, ':1.after');
const newOwnerRegistration = calls.at(-1);
const callsBeforeStaleTimer = calls.length;
staleRetry();
check(calls.length === callsBeforeStaleTimer &&
    changedDuringRetry._registrationAttempts === 1,
    'old owner retry cannot send against replacement owner');
newOwnerRegistration[9](bus, {error: 'remote success followed by timeout'});
fireTimer(changedDuringRetry._registrationRetry);
const repeatedRegistration = calls.at(-1);
check(repeatedRegistration[0] === ':1.after' &&
    repeatedRegistration[4].deep_unpack()[0] === '6',
    'timeout retry repeats registration for the same owner and version');
repeatedRegistration[9](bus, {});
check(changedDuringRetry._registrationState === 'succeeded',
    'idempotent server registration can resolve timeout retry');
changedDuringRetry.disable();

const reenabled = new ExtensionClass();
reenabled.enable();
reenabled._connected(bus, ':1.lifecycle');
const beforeDisable = calls.at(-1);
beforeDisable[9](bus, {error: 'registration failed before disable'});
const disabledRetry = timers.get(reenabled._registrationRetry).callback;
reenabled.disable();
reenabled.enable();
reenabled._connected(bus, ':1.lifecycle-new');
const afterEnable = calls.at(-1);
disabledRetry();
beforeDisable[9](bus, {});
check(reenabled._registrationState === 'pending' &&
    reenabled._registrationAttempts === 1 &&
    calls.at(-1) === afterEnable,
    'prior lifecycle retry and reply cannot mutate re-enabled panel');
afterEnable[9](bus, {});
check(reenabled._registrationState === 'succeeded',
    'new lifecycle registration succeeds');
reenabled.disable();
}

print('GJS panel callback, lifecycle, and error harness passed');
