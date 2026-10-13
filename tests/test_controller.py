import threading
import unittest
from unittest.mock import Mock, patch

from duconnector.controller import Controller, Job
from duconnector.core import Cancelled, Measurement, Snapshot
from duconnector.demo import DEMO_APS, DEMO_GUID, DEMO_PROFILE, DEMO_SSID, DemoWlan


class FakeWlan(DemoWlan):
    def __init__(self):
        super().__init__()
        self.connections = []
        self.fail_calls = set()

    def connect(self, guid, profile, ssid, bssid, stop, timeout=40):
        self.connections.append((bssid, profile))
        if len(self.connections) in self.fail_calls:
            raise OSError("AP unavailable")
        super().connect(guid, profile, ssid, bssid, stop, timeout)


class FakeEngine:
    def __init__(self):
        self.fail = False
        self.calls = []

    def prepare(self, stop):
        if self.fail:
            raise OSError("Download failed")

    def measure(self, address, server, stop, verify):
        self.calls.append(server)
        return Measurement(100, 10, 12, server or 123)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.wlan, self.engine, self.events = FakeWlan(), FakeEngine(), []
        self.controller = Controller(self.wlan, self.engine, lambda key, data: self.events.append((key, data)))
        self.job = Job("best", DEMO_GUID, DEMO_SSID, DEMO_PROFILE, consent=True)

    def test_weak_signal_wins_and_one_server_is_used(self):
        calls = []
        def measure(job, ap, server):
            calls.append((ap.bssid, server))
            return Measurement([30, 50, 100][DEMO_APS.index(ap)], 10, 12, 123)
        self.controller.measure = measure
        self.assertTrue(self.controller.compare(self.job, DEMO_APS))
        self.assertEqual(self.wlan.active.bssid, DEMO_APS[2].bssid)
        self.assertEqual([server for _, server in calls], [0, 123, 123])
        self.assertEqual(len(calls), 3)

    def test_original_is_tested_first_and_gain_retains_it(self):
        self.wlan.active = Snapshot(DEMO_APS[1].bssid, DEMO_SSID, DEMO_PROFILE)
        order = []
        def measure(job, ap, server):
            order.append(ap.bssid)
            return Measurement(33 if ap == DEMO_APS[2] else 30, 10, 5, 123)
        self.controller.measure = measure
        self.controller.compare(self.job, DEMO_APS)
        self.assertEqual(order[0], DEMO_APS[1].bssid)
        self.assertEqual(self.wlan.active.bssid, DEMO_APS[1].bssid)

    def test_unreachable_winner_falls_back_to_next_result(self):
        self.controller.measure = lambda job, ap, server: Measurement(30 + DEMO_APS.index(ap) * 40, 10, 5, 123)
        self.wlan.fail_calls.add(4)
        self.assertTrue(self.controller.compare(self.job, DEMO_APS))
        self.assertEqual(self.wlan.active.bssid, DEMO_APS[1].bssid)

    def test_all_test_failures_restore_original(self):
        def fail(*args):
            raise OSError("Server unavailable")
        self.controller.measure = fail
        self.assertFalse(self.controller.compare(self.job, DEMO_APS))
        self.assertEqual(self.wlan.active.bssid, DEMO_APS[0].bssid)
        self.assertIn((DEMO_APS[0].bssid, DEMO_PROFILE), self.wlan.connections)

    def test_cancel_mid_round_restores_original(self):
        def cancel(*args):
            self.controller.stop.set()
            raise Cancelled()
        self.controller.measure = cancel
        with self.assertRaises(Cancelled):
            self.controller.compare(self.job, DEMO_APS)
        self.assertEqual(self.wlan.active.bssid, DEMO_APS[0].bssid)
        self.assertTrue(self.controller.stop.is_set())

    def test_restore_profile_fallback(self):
        self.wlan.fail_calls.add(1)
        self.controller.restore(self.job, Snapshot(DEMO_APS[0].bssid, DEMO_SSID, "original profile"))
        self.assertEqual(self.wlan.connections, [(DEMO_APS[0].bssid, "original profile"), (None, "original profile")])
        self.assertEqual(self.wlan.active.profile, "original profile")

    def test_engine_failure_does_not_switch_network(self):
        self.engine.fail = True
        self.controller.run(self.job)
        self.assertEqual(self.wlan.connections, [])
        self.assertEqual(self.events[-1], ("done", None))

    def test_consent_required_in_controller(self):
        self.controller.run(Job("best", DEMO_GUID, DEMO_SSID, DEMO_PROFILE))
        self.assertEqual(self.wlan.connections, [])
        self.assertTrue(any("Accept the Ookla" in str(data) for kind, data in self.events))

    def test_roaming_result_is_discarded(self):
        def roam(*args):
            self.wlan.active = Snapshot(DEMO_APS[1].bssid, DEMO_SSID, DEMO_PROFILE)
            return Measurement(200, 50, 5, 123)
        self.engine.measure = roam
        with patch.object(self.controller.stop, "wait", return_value=False):
            with self.assertRaisesRegex(OSError, "BSSID changed"):
                self.controller.measure(self.job, DEMO_APS[0])

    def test_mismatched_server_is_discarded(self):
        self.engine.measure = lambda *args: Measurement(200, 50, 5, 999)
        with patch.object(self.controller.stop, "wait", return_value=False):
            with self.assertRaisesRegex(OSError, "different server"):
                self.controller.measure(self.job, DEMO_APS[0], server=123)

    def test_empty_scan_leaves_connection_unchanged(self):
        self.assertFalse(self.controller.compare(self.job, []))
        self.assertEqual(self.wlan.connections, [])

    def test_monitor_changed_aps_waits_three_minutes(self):
        clock = [0]
        class MonitorStop:
            def is_set(self):
                return False

            def wait(self, seconds):
                clock[0] += seconds
                return clock[0] >= 240

        self.controller.stop = MonitorStop()
        self.wlan.scan = Mock(side_effect=[DEMO_APS, DEMO_APS[:2], DEMO_APS[:2], DEMO_APS[:2]])
        rounds = []
        self.controller.compare = lambda job, aps: rounds.append(clock[0])
        job = Job("best", DEMO_GUID, DEMO_SSID, DEMO_PROFILE, monitor=True, consent=True)
        with patch("duconnector.controller.time.monotonic", side_effect=lambda: clock[0]):
            self.controller.run(job)
        self.assertEqual(rounds, [0, 180])
        self.assertEqual(self.wlan.scan.call_count, 4)

    def test_monitor_signal_changes_do_not_trigger_retest(self):
        clock = [0]
        class MonitorStop:
            def is_set(self):
                return False

            def wait(self, seconds):
                clock[0] += seconds
                return clock[0] >= 240

        self.controller.stop = MonitorStop()
        from duconnector.core import AccessPoint
        changed_signal = [AccessPoint(ap.bssid, 1, -99, ap.frequency_khz) for ap in DEMO_APS]
        self.wlan.scan = Mock(side_effect=[DEMO_APS, changed_signal, changed_signal, changed_signal])
        rounds = []
        self.controller.compare = lambda job, aps: rounds.append(clock[0])
        job = Job("best", DEMO_GUID, DEMO_SSID, DEMO_PROFILE, monitor=True, consent=True)
        with patch("duconnector.controller.time.monotonic", side_effect=lambda: clock[0]):
            self.controller.run(job)
        self.assertEqual(rounds, [0])

    def test_network_discovery_needs_no_ssid_profile_or_consent(self):
        self.controller.run(Job("networks", DEMO_GUID, b""))
        networks = next(data for kind, data in self.events if kind == "networks")
        self.assertEqual(len(networks), 2)
        self.assertEqual(networks[0].ssid, DEMO_SSID)
        self.assertEqual(self.wlan.connections, [])
