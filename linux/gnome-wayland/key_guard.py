"""Journaled GNOME hardware sleep-key redirection to the screen lock action."""

import json
import os


SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys'
SLEEP_KEYS = ('suspend', 'suspend-static', 'hibernate', 'hibernate-static',
              'power', 'power-static')
LOCK_KEY = 'screensaver'
KEYS = (*SLEEP_KEYS, LOCK_KEY)


def desktop_settings():
    from gi.repository import Gio

    schema = Gio.SettingsSchemaSource.get_default().lookup(SCHEMA, True)
    if schema is None or not all(schema.has_key(key) for key in KEYS):
        raise ValueError('GNOME media-key settings are unavailable')
    return Gio.Settings.new_full(schema, None, None)


class GnomeKeyGuard:
    def __init__(self, path, persist, settings=None, sync=None):
        self.path = path
        self.persist = persist
        self.owned_entries = {}
        self.settings = settings if settings is not None else desktop_settings()
        if sync is None:
            from gi.repository import Gio
            sync = Gio.Settings.sync
        self.sync = sync

    def user_value(self, key):
        value = self.settings.get_user_value(key)
        return None if value is None else list(value.unpack())

    def read(self):
        try:
            record = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if (not isinstance(record, dict) or record.get('schema') != 1 or
                record.get('uid') != os.getuid() or
                not isinstance(record.get('keys'), dict)):
            raise ValueError('Invalid sleep-key recovery journal; retained for inspection')
        keys = record['keys']
        for key, entry in keys.items():
            if key not in KEYS or not isinstance(entry, dict) or set(entry) != {'prior', 'written'}:
                raise ValueError('Invalid sleep-key recovery entry')
            for field in ('prior', 'written'):
                items = entry[field]
                if field == 'prior' and items is None:
                    continue
                if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                    raise ValueError('Invalid sleep-key recovery value')
            if key in SLEEP_KEYS and entry['written'] != []:
                raise ValueError('Invalid sleep-key guard value')
        return keys

    def save(self, entries):
        self.persist(self.path, {'schema': 1, 'uid': os.getuid(), 'keys': entries})
        self.owned_entries = entries

    def journal_missing(self):
        return bool(self.owned_entries) and not self.path.exists()

    def active(self):
        entries = self.read()
        if self.journal_missing():
            entries = self.owned_entries
        return (set(entries) == set(KEYS) and
                all(self.user_value(key) == entry['written']
                    for key, entry in entries.items()))

    def acquire(self):
        if self.active():
            return
        self.restore()
        if not all(self.settings.is_writable(key) for key in KEYS):
            raise ValueError('GNOME sleep-key settings are locked by administrator policy')
        lock_bindings = list(self.settings.get_strv(LOCK_KEY))
        for key in SLEEP_KEYS:
            for binding in self.settings.get_strv(key):
                if binding and binding not in lock_bindings:
                    lock_bindings.append(binding)
        entries = {key: {'prior': self.user_value(key),
                         'written': lock_bindings if key == LOCK_KEY else []}
                   for key in KEYS}
        # Persist all original user values before the first GSettings write.
        self.save(entries)
        try:
            for key, entry in entries.items():
                if not self.settings.set_strv(key, entry['written']):
                    raise ValueError('Could not redirect GNOME sleep key: ' + key)
            self.sync()
            if not self.active():
                raise ValueError('GNOME sleep-key redirection readback failed')
        except Exception:
            self.restore()
            raise

    def restore(self):
        entries = self.read()
        if self.journal_missing():
            # Only release may recreate a deleted journal. reset() removes user
            # overrides so installed schema/vendor defaults take effect.
            entries = {key: {'prior': None, 'written': entry['written']}
                       for key, entry in self.owned_entries.items()}
            self.save(entries)
        if not entries:
            return
        remaining = dict(entries)
        failures = []
        # Release our lock binding before returning keys to their old actions.
        for key in (LOCK_KEY, *SLEEP_KEYS):
            if key not in entries:
                continue
            entry = entries[key]
            try:
                if self.user_value(key) == entry['written']:
                    if entry['prior'] is None:
                        self.settings.reset(key)
                    elif not self.settings.set_strv(key, entry['prior']):
                        raise ValueError('Could not restore GNOME sleep key: ' + key)
                    self.sync()
                    if self.user_value(key) != entry['prior']:
                        raise ValueError('GNOME sleep-key restoration readback failed: ' + key)
                # An intervening user edit belongs to the user and is preserved.
                remaining.pop(key)
            except Exception as error:
                failures.append(str(error))
        self.save(remaining)
        if failures:
            raise ValueError('; '.join(failures))
