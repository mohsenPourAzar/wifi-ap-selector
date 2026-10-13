import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

from duconnector.demo import DEMO_APS, DEMO_PROFILE, DEMO_SSID, DemoWlan, GUEST_APS, GUEST_PROFILE, GUEST_SSID
from duconnector.ui import MainWindow


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow(demo=True, persist=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        if self.window.worker:
            self.window.stop_job()
            self.wait_for_job()
        self.window.close()
        self.app.processEvents()

    def wait_for_job(self, timeout=20):
        deadline = time.monotonic() + timeout
        while self.window.worker and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertIsNone(self.window.worker, "Qt worker did not finish")

    def test_scan_and_manual_connection(self):
        self.window.start_job("scan")
        self.wait_for_job()
        self.assertEqual(self.window.table.rowCount(), 3)
        self.window.table.selectRow(2)
        self.window.start_job("connect")
        self.wait_for_job()
        self.assertEqual(self.window.wlan.active.bssid, DEMO_APS[2].bssid)
        self.assertEqual(self.window.table.item(2, 7).text(), "Connected")

    def test_full_comparison_keeps_ui_responsive(self):
        self.window.consent.setChecked(True)
        self.window.start_job("best")
        self.assertFalse(self.window.adapter_box.isEnabled())
        self.wait_for_job()
        self.assertEqual(self.window.wlan.active.bssid, DEMO_APS[2].bssid)
        self.assertEqual(self.window.table.item(2, 4).text(), "112.40")
        self.assertEqual(self.window.table.item(2, 7).text(), "Selected and connected")
        self.assertTrue(self.window.adapter_box.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())

    def test_consent_is_required_and_not_prechecked(self):
        self.assertFalse(self.window.consent.isChecked())
        with patch.object(QMessageBox, "information") as dialog:
            self.window.start_job("best")
            self.assertIsNone(self.window.worker)
            self.assertIn("Accept the Ookla", dialog.call_args.args[2])

    def test_ssid_change_invalidates_scan(self):
        self.window.start_job("scan")
        self.wait_for_job()
        self.window.ssid_edit.setText("other")
        self.assertEqual(self.window.table.rowCount(), 0)
        self.assertIsNone(self.window.list_context)

    def test_close_waits_for_stop_and_restore(self):
        self.window.consent.setChecked(True)
        self.window.start_job("best")
        self.app.processEvents()
        self.window.close()
        self.assertTrue(self.window.close_pending)
        self.wait_for_job()
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.window.wlan.active.bssid, DEMO_APS[0].bssid)

    def test_location_denial_still_selects_matching_saved_profile(self):
        with patch.object(self.window.wlan, "snapshot", side_effect=OSError("Access denied")):
            self.window.adapter_changed()
        self.assertEqual(self.window.profile_box.currentText(), DEMO_PROFILE)
        self.assertIn("Access denied", self.window.log_view.toPlainText())

    def test_profile_name_can_differ_from_ssid(self):
        self.assertEqual(self.window.target_ssid(), DEMO_SSID)
        self.assertEqual(self.window.profile_box.currentText(), DEMO_PROFILE)
        self.window.profile_box.setCurrentIndex(self.window.profile_box.findText(GUEST_PROFILE))
        self.assertEqual(self.window.target_ssid(), GUEST_SSID)

    def test_discovery_and_switching_to_another_network(self):
        self.window.start_job("networks")
        self.wait_for_job()
        self.assertEqual(len(self.window.networks), 2)
        self.window.network_box.setCurrentIndex(self.window.network_box.findData(GUEST_SSID))
        self.assertEqual(self.window.profile_box.currentText(), GUEST_PROFILE)
        self.window.start_job("scan")
        self.wait_for_job()
        self.assertEqual(self.window.table.rowCount(), 2)
        self.assertEqual(self.window.wlan.active.ssid, DEMO_SSID)
        self.window.table.selectRow(1)
        self.window.start_job("connect")
        self.wait_for_job()
        self.assertEqual(self.window.wlan.active.ssid, GUEST_SSID)
        self.assertEqual(self.window.wlan.active.bssid, GUEST_APS[1].bssid)

    def test_disconnected_start_has_no_fixed_network(self):
        backend = DemoWlan()
        backend.active = None
        another = MainWindow(wlan=backend, persist=False)
        try:
            self.assertEqual(another.target_ssid(allow_empty=True), b"")
            self.assertEqual(another.profile_box.currentIndex(), -1)
            self.assertGreaterEqual(another.network_box.count(), 2)
        finally:
            another.close()

    def test_unknown_manual_ssid_can_be_scanned_without_a_profile(self):
        self.window.ssid_edit.setText("Some Organization")
        self.assertEqual(self.window.target_ssid(), b"Some Organization")
        self.assertEqual(self.window.profile_box.currentIndex(), -1)
        self.window.start_job("scan")
        self.wait_for_job()
        self.assertEqual(self.window.table.rowCount(), 0)
