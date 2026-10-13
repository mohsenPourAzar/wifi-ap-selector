import ctypes as C
import os
import threading
import time
import unittest
from unittest.mock import Mock

from duconnector import wlan as w
from duconnector.demo import DEMO_GUID


@unittest.skipUnless(os.name == "nt", "Windows ABI required")
class WlanTests(unittest.TestCase):
    def test_abi_layouts(self):
        sizes = {w.GUID: 16, w.SSID: 36, w.InterfaceInfo: 532, w.ProfileInfo: 516,
                 w.BssEntry: 360, w.ConnectionAttributes: 604, w.BssidList: 20,
                 w.ConnectionParameters: 40, w.Notification: 40, w.UnicastRow: 80}
        for cls, size in sizes.items():
            with self.subTest(structure=cls.__name__):
                self.assertEqual(C.sizeof(cls), size)
        self.assertEqual(w.BssList.entries.offset, 8)
        self.assertEqual(w.UnicastTable.rows.offset, 8)
        self.assertEqual(w.UnicastRow.luid.offset, 32)
        self.assertEqual(w.UnicastRow.dad_state.offset, 64)
        self.assertEqual(w.GUID.parse(DEMO_GUID).text(), DEMO_GUID)

    def test_scan_uses_completion_and_keeps_weak_signals(self):
        backend = w.WindowsWlan.__new__(w.WindowsWlan)
        backend._scan_event = threading.Event()
        backend.handle = w.HANDLE(1)
        backend.api = Mock()
        class Buffer(C.Structure):
            _fields_ = [("total_size", w.DWORD), ("count", w.DWORD), ("entries", w.BssEntry * 4)]
        buffer = Buffer()
        buffer.total_size, buffer.count = C.sizeof(buffer), 4
        for i, entry in enumerate(buffer.entries):
            entry.ssid = w.SSID.create(b"du")
            entry.bss_type = 1
            entry.bssid[:] = [2, 0, 0, 0, 0, i + 1]
            entry.quality = 2 if i == 0 else 90
            entry.rssi = -99 if i == 0 else -45
            entry.frequency = 5_180_000
        buffer.entries[2].ssid = w.SSID.create(b"DU")
        buffer.entries[3].host_timestamp = int((time.time() + 11_644_473_600 - 120) * 10_000_000)
        def scan(*args):
            notification = w.Notification(8, 7, w.GUID.parse(DEMO_GUID), 0, None)
            backend._notify(C.pointer(notification), None)
            return 0
        def get_bss(*args):
            C.cast(args[-1], C.POINTER(C.c_void_p))[0] = C.addressof(buffer)
            return 0
        backend.api.WlanScan.side_effect = scan
        backend.api.WlanGetNetworkBssList.side_effect = get_bss
        aps = backend.scan(DEMO_GUID, b"du", threading.Event())
        self.assertEqual([ap.quality for ap in aps], [90, 2])
        backend.api.WlanFreeMemory.assert_called_once()
        networks = backend.discover(DEMO_GUID, threading.Event())
        self.assertEqual({item.ssid: item.ap_count for item in networks}, {b"du": 2, b"DU": 1})

    def test_native_profile_read_does_not_request_plaintext_keys(self):
        backend = w.WindowsWlan.__new__(w.WindowsWlan)
        backend.handle, backend.api = w.HANDLE(1), Mock()
        buffer = C.create_unicode_buffer('<WLANProfile><SSIDConfig><SSID><name>Office</name></SSID></SSIDConfig></WLANProfile>')
        def get_profile(*args):
            self.assertEqual(C.cast(args[5], C.POINTER(w.DWORD))[0], 0)
            C.cast(args[4], C.POINTER(C.c_void_p))[0] = C.addressof(buffer)
            return 0
        backend.api.WlanGetProfile.side_effect = get_profile
        self.assertEqual(backend.profile_ssid(DEMO_GUID, "Staff profile"), b"Office")
        backend.api.WlanFreeMemory.assert_called_once()

    def test_ipv4_filters_other_adapter_and_link_local(self):
        backend = w.WindowsWlan.__new__(w.WindowsWlan)
        backend.ip = Mock()
        class Buffer(C.Structure):
            _fields_ = [("count", w.DWORD), ("rows", w.UnicastRow * 3)]
        buffer = Buffer()
        buffer.count = 3
        for i, row in enumerate(buffer.rows):
            row.address.family, row.luid, row.dad_state = 2, 77, 4
            row.address.raw[4:8] = [192, 0, 2, i + 10]
        buffer.rows[0].luid = 999
        buffer.rows[1].address.raw[4:8] = [169, 254, 1, 1]
        def get_luid(guid, result):
            C.cast(result, C.POINTER(C.c_uint64))[0] = 77
            return 0
        def get_table(family, result):
            C.cast(result, C.POINTER(C.c_void_p))[0] = C.addressof(buffer)
            return 0
        backend.ip.ConvertInterfaceGuidToLuid.side_effect = get_luid
        backend.ip.GetUnicastIpAddressTable.side_effect = get_table
        self.assertEqual(backend.ipv4(DEMO_GUID), "192.0.2.12")
        backend.ip.FreeMibTable.assert_called_once()
