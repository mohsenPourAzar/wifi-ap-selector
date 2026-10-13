"""GUI launcher plus a read-only diagnostic command."""

import argparse
import json
import sys

from . import APP_NAME, __version__


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - Compare access points for any Wi-Fi network")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--demo", action="store_true", help="Preview Qt with explicitly simulated network data")
    parser.add_argument("--diagnose", action="store_true", help="Print adapters, saved profiles, connection and IPv4 without changing Wi-Fi")
    args = parser.parse_args()
    if args.diagnose:
        from .demo import DemoWlan
        from .wlan import WindowsWlan
        try:
            wlan = DemoWlan() if args.demo else WindowsWlan()
            try:
                adapters = []
                for adapter in wlan.adapters():
                    details = {"guid": adapter.guid, "description": adapter.description, "errors": {}}
                    for field, operation in [("profiles", wlan.profiles), ("connection", wlan.snapshot), ("ipv4", wlan.ipv4)]:
                        try:
                            value = operation(adapter.guid)
                            if field == "connection" and value:
                                value = {"bssid": value.bssid, "ssid": value.ssid.decode("utf-8", errors="replace"),
                                         "profile": value.profile}
                            details[field] = value
                        except OSError as exc:
                            details[field] = None
                            details["errors"][field] = str(exc)
                    adapters.append(details)
                print(json.dumps({"application": APP_NAME, "version": __version__, "demo": args.demo, "adapters": adapters}, indent=2, ensure_ascii=True))
            finally:
                wlan.close()
            return 0
        except OSError as exc:
            print(f"Diagnostic failed: {exc}", file=sys.stderr)
            return 1
    try:
        from .ui import run_gui
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6":
            print("Qt is required. Install dependencies: python -m pip install -r requirements.txt", file=sys.stderr)
            return 1
        raise
    return run_gui(demo=args.demo)


if __name__ == "__main__":
    raise SystemExit(main())
