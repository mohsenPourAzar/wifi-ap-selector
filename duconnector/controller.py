"""Cancellable network jobs independent of the GUI, injectable for testing."""

from dataclasses import dataclass
import math
import threading
import time

from .core import AccessPoint, Cancelled, pick_fastest
from .wlan import check_cancel


@dataclass(frozen=True)
class Job:
    mode: str
    guid: str
    ssid: bytes
    profile: str = ""
    selected: AccessPoint | None = None
    monitor: bool = False
    interval: int = 15
    gain: float = 0.2
    consent: bool = False

    def validate(self):
        if self.mode not in {"networks", "scan", "best", "connect", "test"}:
            raise ValueError("Unknown job mode.")
        if not self.guid or (self.mode != "networks" and not 1 <= len(self.ssid) <= 32):
            raise ValueError("Select an adapter and provide a valid SSID.")
        if self.mode not in {"networks", "scan"} and not self.profile:
            raise ValueError("Select a saved Wi-Fi profile first.")
        if self.mode in {"best", "test"} and not self.consent:
            raise ValueError("Accept the Ookla terms and test data usage before testing.")
        if self.mode in {"connect", "test"} and self.selected is None:
            raise ValueError("Scan and select an access point first.")
        if not 3 <= self.interval <= 1440 or not math.isfinite(self.gain) or not 0 <= self.gain <= 1:
            raise ValueError("Retest interval: 3 to 1440 minutes. Required improvement: 0 to 100 percent.")


class Controller:
    def __init__(self, wlan, engine, emit, stop=None):
        self.wlan, self.engine, self.emit = wlan, engine, emit
        self.stop = stop if stop is not None else threading.Event()

    def log(self, text):
        self.emit("log", text)

    def row(self, ap, state, result=None):
        self.emit("row", (ap.bssid, state, result))

    def status(self, text):
        self.emit("status", text)

    def restore(self, job, original):
        if original is None or not original.profile:
            self.log("There was no original connected profile to restore. Use Windows Wi-Fi settings.")
            return
        self.status("Restoring the previous connection...")
        # Restoration must finish even after Stop or window-close is requested.
        restore_stop = threading.Event()
        try:
            self.wlan.connect(job.guid, original.profile, original.ssid, original.bssid, restore_stop)
            self.log("Original access point restored.")
            return
        except Exception as exc:
            self.log(f"Original AP restoration failed: {exc}")
        try:
            self.log("Asking Windows to restore the original saved network profile.")
            self.wlan.connect(job.guid, original.profile, original.ssid, None, restore_stop, timeout=20)
            self.log("Original network restored; Windows selected its access point.")
        except Exception as exc:
            self.log(f"Could not restore the original network: {exc}. Check Windows Wi-Fi settings.")

    def connect(self, job, ap):
        self.row(ap, "Connecting")
        self.log(f"Connecting to {ap.bssid} using saved profile '{job.profile}'.")
        self.wlan.connect(job.guid, job.profile, job.ssid, ap.bssid, self.stop)
        self.log(f"Verified target BSSID: {ap.bssid}")

    def measure(self, job, ap, server=0):
        def verify():
            active = self.wlan.snapshot(job.guid)
            return bool(active and active.bssid == ap.bssid and active.ssid == job.ssid and active.profile == job.profile)

        self.row(ap, "Waiting for IPv4")
        deadline = time.monotonic() + 30
        while True:
            check_cancel(self.stop)
            if not verify():
                raise OSError("BSSID changed before measurement; result discarded.")
            address = self.wlan.ipv4(job.guid)
            if address:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("No usable Wi-Fi IPv4 address. Check DHCP and network login.")
            self.stop.wait(0.5)
        self.log(f"Wi-Fi IPv4 address: {address}; Speedtest binds to this address.")
        if self.stop.wait(4):
            raise Cancelled("Stopped before Speedtest.")
        if not verify():
            raise OSError("BSSID changed while waiting for IPv4; result discarded.")
        self.status("Measuring real Speedtest throughput...")
        self.row(ap, "Testing speed")
        result = self.engine.measure(address, server, self.stop, verify)
        if not verify():
            raise OSError("BSSID changed at measurement completion; result discarded.")
        if server and result.server != server:
            raise OSError("Speedtest returned a different server; result discarded.")
        return result

    def compare(self, job, aps):
        if not aps:
            self.log("No matching access points are visible. Check the exact SSID and Location permissions.")
            return False
        original = self.wlan.snapshot(job.guid)
        original_index = next((i for i, ap in enumerate(aps) if original and ap.bssid == original.bssid), -1)
        order = list(range(len(aps)))
        if original_index >= 0:
            order.remove(original_index)
            order.insert(0, original_index)
        results, server, selected = [None] * len(aps), 0, False
        try:
            for position, index in enumerate(order, 1):
                check_cancel(self.stop)
                ap = aps[index]
                self.status(f"Test {position} of {len(aps)}: {ap.bssid}")
                try:
                    self.connect(job, ap)
                    result = self.measure(job, ap, server)
                    check_cancel(self.stop)
                    results[index], server = result, result.server
                    self.row(ap, "Test successful", result)
                except Cancelled:
                    self.row(ap, "Cancelled")
                    raise
                except Exception as exc:
                    self.row(ap, "Test failed")
                    self.log(f"{ap.bssid}: {exc}")
            check_cancel(self.stop)
            best = pick_fastest(results, original_index, gain=job.gain)
            while best >= 0:
                check_cancel(self.stop)
                try:
                    self.connect(job, aps[best])
                    check_cancel(self.stop)
                    self.row(aps[best], "Selected and connected", results[best])
                    self.log(f"Selected {aps[best].bssid}: {results[best].download:.2f} Mbps download.")
                    if results[best].download < 20:
                        self.log("No reachable measured AP achieved 20 Mbps. Selected the fastest reachable result.")
                    selected = True
                    return True
                except Cancelled:
                    raise
                except Exception as exc:
                    self.log(f"Final connection failed: {exc}; trying the next measured candidate.")
                    self.row(aps[best], "Final connection failed", results[best])
                    results[best] = None
                    best = pick_fastest(results, gain=0)
            self.log("All tests or final connections failed.")
            return False
        finally:
            if not selected:
                self.restore(job, original)

    def _single(self, job):
        original = self.wlan.snapshot(job.guid)
        successful = False
        try:
            if job.mode == "test":
                self.status("Preparing the official Speedtest engine...")
                self.engine.prepare(self.stop)
            self.connect(job, job.selected)
            result = self.measure(job, job.selected) if job.mode == "test" else None
            check_cancel(self.stop)
            self.row(job.selected, "Test successful" if result else "Connected", result)
            successful = True
        finally:
            if not successful:
                self.row(job.selected, "Cancelled" if self.stop.is_set() else "Failed")
                self.restore(job, original)

    def _run(self, job):
        job.validate()
        check_cancel(self.stop)
        if job.mode == "networks":
            self.status("Finding nearby Wi-Fi networks...")
            networks = self.wlan.discover(job.guid, self.stop)
            self.emit("networks", networks)
            self.log(f"Found {len(networks)} visible networks. Select a network or enter its exact SSID.")
            return
        if job.mode in {"connect", "test"}:
            self._single(job)
            return
        if job.mode == "best":
            self.status("Preparing the official Speedtest engine...")
            self.engine.prepare(self.stop)
        last_round, last_signature, final_bssid = None, frozenset(), None
        while True:
            check_cancel(self.stop)
            self.status("Scanning access points...")
            try:
                aps = self.wlan.scan(job.guid, job.ssid, self.stop)
            except Cancelled:
                raise
            except Exception as exc:
                if not job.monitor:
                    raise
                self.log(f"Monitor scan failed: {exc}; retrying in one minute.")
                if self.stop.wait(60):
                    raise Cancelled("Monitoring stopped.")
                continue
            self.log(f"Scan complete: {len(aps)} matching APs. All signal levels are eligible.")
            if job.mode == "scan":
                self.emit("list", aps)
                return
            signature = frozenset(ap.bssid for ap in aps)
            active = self.wlan.snapshot(job.guid)
            now = time.monotonic()
            due = last_round is None or now - last_round >= job.interval * 60
            changed = signature != last_signature or not active or active.bssid != final_bssid
            if due or (changed and now - last_round >= 180):
                self.emit("list", aps)
                self.compare(job, aps)
                last_round, last_signature = time.monotonic(), signature
                active = self.wlan.snapshot(job.guid)
                final_bssid = active.bssid if active else None
            if not job.monitor:
                return
            self.status("Monitoring active: waiting for movement or the next retest.")
            if self.stop.wait(60):
                raise Cancelled("Monitoring stopped.")

    def run(self, job):
        try:
            self._run(job)
            self.status("Operation completed. See the diagnostic log for results.")
        except Cancelled:
            self.log("Operation stopped.")
            self.status("Stopped. Check the current connection below.")
        except Exception as exc:
            self.log(f"Operation failed: {exc}")
            self.status("Operation failed. See the diagnostic log.")
        finally:
            self.emit("done", None)
