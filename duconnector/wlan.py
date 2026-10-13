"""Native Windows WLAN and IP Helper bindings, with explicit ctypes ABI types.

No localized command output is parsed. Saved profiles retain Windows-managed
credentials. WLAN scans wait for completion notifications before reading BSSs.
"""

import ctypes as C
import ipaddress
import os
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from . import APP_NAME
from .core import AccessPoint, Adapter, Cancelled, Network, Snapshot

DWORD = C.c_uint32
LONG = C.c_int32
WORD = C.c_uint16
BYTE = C.c_uint8
BOOL = C.c_int32
HANDLE = C.c_void_p
WCHAR = C.c_wchar
MAC = BYTE * 6


class GUID(C.Structure):
    _fields_ = [("data1", DWORD), ("data2", WORD), ("data3", WORD), ("data4", BYTE * 8)]

    @classmethod
    def parse(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)

    def text(self):
        return str(uuid.UUID(bytes_le=bytes(self)))


class SSID(C.Structure):
    _fields_ = [("length", DWORD), ("data", BYTE * 32)]

    @classmethod
    def create(cls, value):
        if not 1 <= len(value) <= 32:
            raise ValueError("SSID must contain 1 to 32 bytes.")
        result = cls()
        result.length = len(value)
        result.data[:len(value)] = value
        return result

    def value(self):
        return bytes(self.data[:min(self.length, 32)])


class InterfaceInfo(C.Structure):
    _fields_ = [("guid", GUID), ("description", WCHAR * 256), ("state", DWORD)]


class ListHeader(C.Structure):
    _fields_ = [("count", DWORD), ("index", DWORD)]


class ProfileInfo(C.Structure):
    _fields_ = [("name", WCHAR * 256), ("flags", DWORD)]


class RateSet(C.Structure):
    _fields_ = [("length", DWORD), ("rates", WORD * 126)]


class BssEntry(C.Structure):
    _fields_ = [("ssid", SSID), ("phy_id", DWORD), ("bssid", MAC),
                ("bss_type", DWORD), ("phy_type", DWORD), ("rssi", LONG),
                ("quality", DWORD), ("in_reg_domain", BYTE), ("beacon_period", WORD),
                ("timestamp", C.c_uint64), ("host_timestamp", C.c_uint64),
                ("capabilities", WORD), ("frequency", DWORD), ("rate_set", RateSet),
                ("ie_offset", DWORD), ("ie_size", DWORD)]


class BssList(C.Structure):
    _fields_ = [("total_size", DWORD), ("count", DWORD), ("entries", BssEntry * 1)]


class Association(C.Structure):
    _fields_ = [("ssid", SSID), ("bss_type", DWORD), ("bssid", MAC),
                ("phy_type", DWORD), ("phy_index", DWORD), ("quality", DWORD),
                ("rx_rate", DWORD), ("tx_rate", DWORD)]


class Security(C.Structure):
    _fields_ = [("enabled", BOOL), ("one_x", BOOL), ("auth", DWORD), ("cipher", DWORD)]


class ConnectionAttributes(C.Structure):
    _fields_ = [("state", DWORD), ("mode", DWORD), ("profile", WCHAR * 256),
                ("association", Association), ("security", Security)]


class ObjectHeader(C.Structure):
    _fields_ = [("type", BYTE), ("revision", BYTE), ("size", WORD)]


class BssidList(C.Structure):
    _fields_ = [("header", ObjectHeader), ("count", DWORD), ("total", DWORD), ("macs", MAC * 1)]


class ConnectionParameters(C.Structure):
    _fields_ = [("mode", DWORD), ("profile", C.c_wchar_p), ("ssid", C.POINTER(SSID)),
                ("desired", C.POINTER(BssidList)), ("bss_type", DWORD), ("flags", DWORD)]


class Notification(C.Structure):
    _fields_ = [("source", DWORD), ("code", DWORD), ("guid", GUID),
                ("size", DWORD), ("data", C.c_void_p)]


class ConnectionNotice(C.Structure):
    _fields_ = [("mode", DWORD), ("profile", WCHAR * 256), ("ssid", SSID),
                ("bss_type", DWORD), ("security", BOOL), ("reason", DWORD), ("flags", DWORD)]


class SockaddrInet(C.Union):
    _fields_ = [("family", WORD), ("raw", BYTE * 28), ("alignment", DWORD)]


class UnicastRow(C.Structure):
    _fields_ = [("address", SockaddrInet), ("luid", C.c_uint64), ("index", DWORD),
                ("prefix_origin", DWORD), ("suffix_origin", DWORD),
                ("valid_lifetime", DWORD), ("preferred_lifetime", DWORD),
                ("prefix_length", BYTE), ("skip_source", BYTE), ("dad_state", DWORD),
                ("scope_id", DWORD), ("created", C.c_int64)]


class UnicastTable(C.Structure):
    _fields_ = [("count", DWORD), ("rows", UnicastRow * 1)]


def mac_text(value):
    return ":".join(f"{byte:02X}" for byte in value)


def check_cancel(stop):
    if stop.is_set():
        raise Cancelled("Operation stopped.")


def profile_ssid_from_xml(xml):
    """Extract the actual SSID, which need not equal the Windows profile name."""
    try:
        root = ET.fromstring(xml)
        node = root.find("./{*}SSIDConfig/{*}SSID")
        if node is None:
            raise ValueError("Saved profile has no SSID configuration.")
        hexadecimal = node.find("{*}hex")
        name = node.find("{*}name")
        if hexadecimal is not None and hexadecimal.text:
            value = bytes.fromhex(hexadecimal.text)
        elif name is not None and name.text:
            value = name.text.encode("utf-8")
        else:
            raise ValueError("Saved profile has no SSID.")
        if not 1 <= len(value) <= 32:
            raise ValueError("Saved profile SSID must contain 1 to 32 bytes.")
        return value
    except ET.ParseError as exc:
        raise ValueError("Invalid saved Wi-Fi profile XML.") from exc


class WlanError(OSError):
    def __init__(self, operation, code):
        detail = C.FormatError(code).strip() if os.name == "nt" else str(code)
        hint = ""
        if code == 5:
            hint = " Enable Windows Location services and desktop-app location access; check saved-profile permissions."
        elif code == 1062:
            hint = " Enable Wi-Fi and the WLAN AutoConfig service."
        super().__init__(code, f"{operation} failed. Windows error {code}: {detail}.{hint}")


class WindowsWlan:
    def __init__(self):
        if os.name != "nt":
            raise OSError(f"{APP_NAME} requires Windows 10 or 11.")
        if C.sizeof(C.c_void_p) != 8:
            raise OSError("Use a 64-bit Python installation.")
        self.api = C.WinDLL("wlanapi.dll")
        self.ip = C.WinDLL("iphlpapi.dll")
        callback_type = C.WINFUNCTYPE(None, C.POINTER(Notification), C.c_void_p)
        self._callback = callback_type(self._notify)
        self._scan_event = threading.Event()
        self._scan_guid = None
        self._scan_failed = False
        self._reason = 0
        signatures = {
            "WlanOpenHandle": [DWORD, C.c_void_p, C.POINTER(DWORD), C.POINTER(HANDLE)],
            "WlanCloseHandle": [HANDLE, C.c_void_p],
            "WlanFreeMemory": [C.c_void_p],
            "WlanEnumInterfaces": [HANDLE, C.c_void_p, C.POINTER(C.c_void_p)],
            "WlanGetProfileList": [HANDLE, C.POINTER(GUID), C.c_void_p, C.POINTER(C.c_void_p)],
            "WlanGetProfile": [HANDLE, C.POINTER(GUID), C.c_wchar_p, C.c_void_p, C.POINTER(C.c_void_p), C.POINTER(DWORD), C.POINTER(DWORD)],
            "WlanQueryInterface": [HANDLE, C.POINTER(GUID), DWORD, C.c_void_p, C.POINTER(DWORD), C.POINTER(C.c_void_p), C.c_void_p],
            "WlanScan": [HANDLE, C.POINTER(GUID), C.c_void_p, C.c_void_p, C.c_void_p],
            "WlanGetNetworkBssList": [HANDLE, C.POINTER(GUID), C.c_void_p, DWORD, BOOL, C.c_void_p, C.POINTER(C.c_void_p)],
            "WlanConnect": [HANDLE, C.POINTER(GUID), C.POINTER(ConnectionParameters), C.c_void_p],
            "WlanRegisterNotification": [HANDLE, DWORD, BOOL, callback_type, C.c_void_p, C.c_void_p, C.c_void_p],
            "WlanReasonCodeToString": [DWORD, DWORD, C.c_wchar_p, C.c_void_p],
        }
        for name, args in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = args
            function.restype = None if name == "WlanFreeMemory" else DWORD
        self.ip.ConvertInterfaceGuidToLuid.argtypes = [C.POINTER(GUID), C.POINTER(C.c_uint64)]
        self.ip.ConvertInterfaceGuidToLuid.restype = DWORD
        self.ip.GetUnicastIpAddressTable.argtypes = [WORD, C.POINTER(C.c_void_p)]
        self.ip.GetUnicastIpAddressTable.restype = DWORD
        self.ip.FreeMibTable.argtypes = [C.c_void_p]
        self.ip.FreeMibTable.restype = None
        self.handle = HANDLE()
        negotiated = DWORD()
        self._check("WlanOpenHandle", self.api.WlanOpenHandle(2, None, C.byref(negotiated), C.byref(self.handle)))
        try:
            self._check("WlanRegisterNotification", self.api.WlanRegisterNotification(self.handle, 8, False, self._callback, None, None, None))
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check(operation, code):
        if code:
            raise WlanError(operation, code)

    def close(self):
        if self.handle:
            self.api.WlanCloseHandle(self.handle, None)
            self.handle = HANDLE()

    def _notify(self, pointer, context):
        notification = pointer.contents
        if notification.source != 8 or notification.guid.text() != self._scan_guid:
            return
        if notification.code in (7, 8):  # ACM scan complete / scan failed
            self._scan_failed = notification.code == 8
            self._scan_event.set()
        elif notification.code in (10, 11) and notification.data and notification.size >= C.sizeof(ConnectionNotice):
            self._reason = C.cast(notification.data, C.POINTER(ConnectionNotice)).contents.reason

    def adapters(self):
        pointer = C.c_void_p()
        self._check("WlanEnumInterfaces", self.api.WlanEnumInterfaces(self.handle, None, C.byref(pointer)))
        try:
            header = C.cast(pointer, C.POINTER(ListHeader)).contents
            if header.count > 4096:
                raise OSError("WLAN returned too many interfaces.")
            entries = (InterfaceInfo * header.count).from_address(pointer.value + C.sizeof(ListHeader))
            return [Adapter(item.guid.text(), item.description, item.state == 1) for item in entries]
        finally:
            self.api.WlanFreeMemory(pointer)

    def profiles(self, guid_text):
        guid, pointer = GUID.parse(guid_text), C.c_void_p()
        self._check("WlanGetProfileList", self.api.WlanGetProfileList(self.handle, C.byref(guid), None, C.byref(pointer)))
        try:
            header = C.cast(pointer, C.POINTER(ListHeader)).contents
            if header.count > 4096:
                raise OSError("WLAN returned too many profiles.")
            entries = (ProfileInfo * header.count).from_address(pointer.value + C.sizeof(ListHeader))
            return [item.name for item in entries]
        finally:
            self.api.WlanFreeMemory(pointer)

    def snapshot(self, guid_text):
        guid, pointer, size = GUID.parse(guid_text), C.c_void_p(), DWORD()
        code = self.api.WlanQueryInterface(self.handle, C.byref(guid), 7, None, C.byref(size), C.byref(pointer), None)
        if code == 5023:  # ERROR_INVALID_STATE: interface is disconnected
            return None
        self._check("WlanQueryInterface", code)
        try:
            if size.value < C.sizeof(ConnectionAttributes):
                raise OSError("WLAN returned incomplete connection attributes.")
            item = C.cast(pointer, C.POINTER(ConnectionAttributes)).contents
            if item.state != 1:
                return None
            return Snapshot(mac_text(item.association.bssid), item.association.ssid.value(), item.profile)
        finally:
            self.api.WlanFreeMemory(pointer)

    def profile_ssid(self, guid_text, profile):
        guid, pointer, flags, access = GUID.parse(guid_text), C.c_void_p(), DWORD(0), DWORD()
        # Zero flags: never request or log a plaintext Wi-Fi key.
        self._check("WlanGetProfile", self.api.WlanGetProfile(
            self.handle, C.byref(guid), profile, None, C.byref(pointer), C.byref(flags), C.byref(access)))
        try:
            return profile_ssid_from_xml(C.wstring_at(pointer))
        finally:
            self.api.WlanFreeMemory(pointer)

    def discover(self, guid_text, stop):
        counts = {}
        for ssid, ap in self.scan(guid_text, None, stop):
            if ssid:
                counts[ssid] = counts.get(ssid, 0) + 1
        return [Network(ssid, count) for ssid, count in sorted(counts.items())]

    def scan(self, guid_text, ssid, stop):
        check_cancel(stop)
        guid = GUID.parse(guid_text)
        self._scan_guid = guid_text
        self._scan_failed = False
        self._scan_event.clear()
        self._check("WlanScan", self.api.WlanScan(self.handle, C.byref(guid), None, None, None))
        deadline = time.monotonic() + 15
        while not self._scan_event.wait(0.1):
            check_cancel(stop)
            if time.monotonic() >= deadline:
                raise TimeoutError("Wi-Fi scan timed out. Cached access points will not be tested.")
        check_cancel(stop)
        if self._scan_failed:
            raise OSError("Wi-Fi scan failed. Check Wi-Fi, Location permissions, and WLAN AutoConfig.")
        pointer = C.c_void_p()
        self._check("WlanGetNetworkBssList", self.api.WlanGetNetworkBssList(self.handle, C.byref(guid), None, 3, False, None, C.byref(pointer)))
        try:
            header = C.cast(pointer, C.POINTER(BssList)).contents
            offset = BssList.entries.offset
            if header.count > 4096 or offset + header.count * C.sizeof(BssEntry) > header.total_size:
                raise OSError("Invalid or oversized WLAN BSS list.")
            entries = (BssEntry * header.count).from_address(pointer.value + offset)
            now = int((time.time() + 11_644_473_600) * 10_000_000)
            found = {}
            for item in entries:
                network_ssid = item.ssid.value()
                if (ssid is not None and network_ssid != ssid) or item.bss_type != 1:
                    continue
                if item.host_timestamp and now - item.host_timestamp > 900_000_000:
                    continue
                bssid = mac_text(item.bssid)
                ap = AccessPoint(bssid, item.quality, item.rssi, item.frequency)
                key = (network_ssid, bssid)
                if key not in found or ap.quality > found[key][1].quality:
                    found[key] = (network_ssid, ap)
            ordered = sorted(found.values(), key=lambda pair: (-pair[1].quality, pair[1].bssid))
            return [ap for network_ssid, ap in ordered] if ssid is not None else ordered
        finally:
            self.api.WlanFreeMemory(pointer)

    def connect(self, guid_text, profile, ssid, bssid, stop, timeout=40):
        check_cancel(stop)
        self._scan_guid, self._reason = guid_text, 0
        guid, target_ssid = GUID.parse(guid_text), SSID.create(ssid)
        desired = BssidList()
        desired.header = ObjectHeader(0x80, 1, C.sizeof(BssidList))
        desired.count = desired.total = 1
        if bssid:
            mac = bytes.fromhex(bssid.replace(":", ""))
            if len(mac) != 6:
                raise ValueError("BSSID must contain six MAC address bytes.")
            desired.macs[0][:] = mac
        active = self.snapshot(guid_text)
        if not (active and active.ssid == ssid and active.profile == profile and bssid and active.bssid == bssid):
            parameters = ConnectionParameters(0, profile, C.pointer(target_ssid), C.pointer(desired) if bssid else None, 1, 0)
            self._check("WlanConnect", self.api.WlanConnect(self.handle, C.byref(guid), C.byref(parameters), None))
        deadline, stable = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            check_cancel(stop)
            active = self.snapshot(guid_text)
            if active and active.ssid == ssid and active.profile == profile and (bssid is None or active.bssid == bssid):
                if stable is None:
                    stable = time.monotonic()
                if time.monotonic() - stable >= 3:
                    return
            else:
                stable = None
            stop.wait(0.25)
        detail = ""
        if self._reason:
            message = C.create_unicode_buffer(1024)
            self.api.WlanReasonCodeToString(self._reason, len(message), message, None)
            detail = f" WLAN reason {self._reason}: {message.value}"
        actual = f" Windows is connected to {active.bssid}." if active else " Windows is disconnected."
        raise TimeoutError(f"Could not verify requested BSSID/profile within {timeout} seconds.{actual}{detail}")

    def ipv4(self, guid_text):
        guid, luid, pointer = GUID.parse(guid_text), C.c_uint64(), C.c_void_p()
        self._check("ConvertInterfaceGuidToLuid", self.ip.ConvertInterfaceGuidToLuid(C.byref(guid), C.byref(luid)))
        self._check("GetUnicastIpAddressTable", self.ip.GetUnicastIpAddressTable(2, C.byref(pointer)))
        try:
            header = C.cast(pointer, C.POINTER(UnicastTable)).contents
            if header.count > 65536:
                raise OSError("IP Helper returned too many addresses.")
            rows = (UnicastRow * header.count).from_address(pointer.value + UnicastTable.rows.offset)
            for row in rows:
                if row.luid != luid.value or row.address.family != 2 or row.dad_state != 4 or row.skip_source:
                    continue
                address = ipaddress.IPv4Address(bytes(row.address.raw[4:8]))
                if not (address.is_link_local or address.is_loopback or address.is_unspecified):
                    return str(address)
            return None
        finally:
            self.ip.FreeMibTable(pointer)
