"""Explicitly simulated data for safe previews, never used by real network jobs."""

from .core import AccessPoint, Adapter, Measurement, Network, Snapshot
from .wlan import check_cancel

DEMO_GUID = "00000000-0000-0000-0000-000000000001"
DEMO_SSID = b"Office Wi-Fi"
DEMO_PROFILE = "Employee access"
GUEST_SSID = b"Guest Wi-Fi"
GUEST_PROFILE = "Visitor access"
DEMO_APS = [AccessPoint("02:00:00:00:00:01", 92, -47, 2_437_000),
            AccessPoint("02:00:00:00:00:02", 64, -68, 5_180_000),
            AccessPoint("02:00:00:00:00:03", 28, -84, 5_745_000)]
GUEST_APS = [AccessPoint("02:00:00:00:01:01", 76, -60, 2_437_000),
             AccessPoint("02:00:00:00:01:02", 50, -75, 5_180_000)]


class DemoWlan:
    def __init__(self):
        self.active = Snapshot(DEMO_APS[0].bssid, DEMO_SSID, DEMO_PROFILE)

    def adapters(self):
        return [Adapter(DEMO_GUID, "Simulated Wi-Fi adapter", True)]

    def profiles(self, guid):
        return [DEMO_PROFILE, GUEST_PROFILE]

    def profile_ssid(self, guid, profile):
        return {DEMO_PROFILE: DEMO_SSID, GUEST_PROFILE: GUEST_SSID}[profile]

    def discover(self, guid, stop):
        check_cancel(stop)
        return [Network(DEMO_SSID, 3), Network(GUEST_SSID, 2)]

    def snapshot(self, guid):
        return self.active

    def scan(self, guid, ssid, stop):
        check_cancel(stop)
        return list(DEMO_APS) if ssid == DEMO_SSID else list(GUEST_APS) if ssid == GUEST_SSID else []

    def connect(self, guid, profile, ssid, bssid, stop, timeout=40):
        check_cancel(stop)
        self.active = Snapshot(bssid or DEMO_APS[0].bssid, ssid, profile)

    def ipv4(self, guid):
        return "192.0.2.10"

    def close(self):
        pass


class DemoEngine:
    def __init__(self, wlan, log):
        self.wlan, self.log = wlan, log

    def prepare(self, stop):
        check_cancel(stop)
        self.log("DEMO: simulated engine; no download or real speed test.")

    def measure(self, ipv4, server, stop, verify):
        check_cancel(stop)
        if not verify():
            raise OSError("Simulated BSSID changed.")
        index = int(self.wlan.active.bssid[-2:], 16) - 1
        return Measurement([35.2, 81.6, 112.4][index], [12.8, 29.1, 34.5][index],
                           [19.5, 12.3, 10.8][index], server or 123)
