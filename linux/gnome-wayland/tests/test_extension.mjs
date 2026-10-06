// Local panel-state harness. GNOME Shell lifecycle still needs a live session.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../extension/sleep-disabler@local/extension.js', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '')
    .replace('export default class SleepDisablerExtension', 'class SleepDisablerExtension') +
    '\nglobalThis.SleepDisablerExtension = SleepDisablerExtension;';
const context = {
    Extension: class {},
    Gio: {DBusCallFlags: {NONE: 0}},
    GLib: {Variant: class {constructor(_type, value) { this.value = value; }}, VariantType: class {}},
};
vm.createContext(context);
vm.runInContext(source, context);
const panel = new context.SleepDisablerExtension();
const item = () => ({setToggleState(value) { this.toggle = value; },
    setSensitive(value) { this.sensitive = value; }});
panel._button = {};
panel._status = {label: {text: ''}};
panel._icon = {};
panel._prevention = item();
panel._lid = item();
panel._dimming = item();
panel._failsafe = item();
panel._cancel = item();
panel._timerItems = [item()];
panel._generation = 1;
panel._cancellable = {};
const state = {enabled: true, lidDimming: true, brightnessAvailable: false,
    brightnessRecoveryPending: true, lidRequested: true, lidLockAcquired: true,
    lidOutcome: 'unverified'};
panel._render(state);
assert.equal(panel._dimming.sensitive, true, 'enabled dimming can be switched off');
assert.match(panel._status.label.text, /brightness recovery pending/);
assert.match(panel._status.label.text, /unverified/);

panel._bus = {call(_app, _path, _iface, _method, _params, _out, _flags, _timeout, _cancel, callback) {
    callback(this, {});
}, call_finish() { throw new Error('preference rejected'); }};
panel._refresh = () => panel._render(state);
panel._call('SetLidDimming', {});
assert.match(panel._status.label.text, /preference rejected/,
    'action error survives authoritative state refresh');
assert.equal(panel._dimming.toggle, true, 'rejected switch reverts to agent state');
panel._disconnected();
assert.equal(panel._actionError, '', 'owner change clears stale action error');

panel._button = {};
panel._state = null;
panel._actionError = '';
panel._render({enabled: false, desired: false, gnomeReleasePending: true});
assert.match(panel._status.label.text, /release pending/,
    'uncertain GNOME release is distinct from fully off');
assert.equal(panel._prevention.sensitive, false,
    'prevention cannot be reacquired while the old cookie is uncertain');
assert.equal(panel._icon.icon_name, 'dialog-warning-symbolic');
console.log('Panel state harness passed');
