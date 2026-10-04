#!/usr/bin/python3
"""Small recovery/configuration CLI for the GNOME Wayland agent."""

import argparse
import json
import sys

import dbus


APP = "org.sleepdisabler.App"
PATH = "/org/sleepdisabler/App"
IFACE = "org.sleepdisabler.App1"


def native(value):
    if isinstance(value, dict):
        return {str(key): native(item) for key, item in value.items()}
    if isinstance(value, (dbus.Boolean, bool)):
        return bool(value)
    if isinstance(value, (dbus.Int32, dbus.UInt32, dbus.Int64, dbus.UInt64, dbus.Double)):
        return float(value) if isinstance(value, dbus.Double) else int(value)
    return str(value)


def main():
    parser = argparse.ArgumentParser(description="Control Sleep Disabler in the current GNOME session")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("on")
    sub.add_parser("off")
    lid = sub.add_parser("lid", help="Attempt to block lid-triggered sleep (experimental)")
    lid.add_argument("value", choices=("on", "off"))
    dim = sub.add_parser("dim", help="Dim built-in panel when lid is closed if GNOME exposes the API")
    dim.add_argument("value", choices=("on", "off"))
    failsafe = sub.add_parser("failsafe")
    failsafe.add_argument("value", choices=("on", "off"))
    failsafe.add_argument("threshold", nargs="?", type=int, default=None, help="percent, 1–99")
    timer = sub.add_parser("timer", help="schedule suspend after a whole-minute duration")
    timer.add_argument("minutes", type=int)
    timer.add_argument("--lid-closed", action="store_true")
    timer.add_argument("--prevention-on", action="store_true")
    sub.add_parser("cancel-timer")
    args = parser.parse_args()
    try:
        bus = dbus.SessionBus()
        app = dbus.Interface(bus.get_object(APP, PATH), IFACE)
        state = app.GetState()
        if args.command == "status":
            print(json.dumps(native(state), indent=2, sort_keys=True))
        elif args.command in ("on", "off"):
            app.SetPrevention(args.command == "on")
        elif args.command == "lid":
            app.SetLidMode(args.value == "on")
        elif args.command == "dim":
            app.SetLidDimming(args.value == "on")
        elif args.command == "failsafe":
            threshold = args.threshold if args.threshold is not None else int(state["threshold"])
            app.SetFailsafe(args.value == "on", dbus.UInt32(threshold))
        elif args.command == "timer":
            if not 1 <= args.minutes <= 365 * 1440 + 23 * 60 + 59:
                parser.error("minutes must be 1–527039")
            app.StartTimer(dbus.UInt64(args.minutes * 60), args.lid_closed, args.prevention_on)
        elif args.command == "cancel-timer":
            app.CancelTimer()
        if args.command != "status":
            print(json.dumps(native(app.GetState()), indent=2, sort_keys=True))
    except dbus.DBusException as error:
        print(f"Sleep Disabler: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
