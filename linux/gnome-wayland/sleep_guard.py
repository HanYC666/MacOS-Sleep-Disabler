#!/usr/bin/python3
"""Read-only ExecCondition for systemd sleep services.

No suspend, settings, or inhibitor mutations are performed here. Exit 0 allows
the service to continue, 1 skips it because this app holds a sleep block, and
255 fails the service when inhibitor state cannot be established.
"""

import sys


def prevention_held(inhibitors):
    for inhibitor in inhibitors:
        if len(inhibitor) != 6:
            raise ValueError('Malformed login1 inhibitor record')
        what, who, why, mode, uid, pid = inhibitor
        if (not all(isinstance(item, str) for item in (what, who, why, mode)) or
                not isinstance(uid, int) or isinstance(uid, bool) or uid < 0 or
                not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
            raise ValueError('Malformed login1 inhibitor fields')
        if ('sleep' in what.split(':') and mode == 'block' and
                who == 'Sleep Disabler' and why == 'Keep this session awake'):
            return True
    return False


def read_inhibitors():
    import dbus

    bus = dbus.SystemBus(private=True)
    try:
        manager = dbus.Interface(bus.get_object('org.freedesktop.login1',
                                '/org/freedesktop/login1', introspect=False),
                                 'org.freedesktop.login1.Manager')
        return manager.ListInhibitors(timeout=2)
    finally:
        bus.close()


def main():
    try:
        if prevention_held(read_inhibitors()):
            print('Sleep Disabler: sleep prevention is on; sleep service skipped', file=sys.stderr)
            return 1
        return 0
    except Exception as error:
        print(f'Sleep Disabler: inhibitor state unavailable; sleep service blocked: {error}',
              file=sys.stderr)
        return 255


if __name__ == '__main__':
    sys.exit(main())
