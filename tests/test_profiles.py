import unittest

from duconnector.core import ssid_label
from duconnector.wlan import profile_ssid_from_xml


class ProfileTests(unittest.TestCase):
    def test_profile_name_is_not_used_as_the_ssid(self):
        xml = '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1"><name>Corporate credentials</name><SSIDConfig><SSID><hex>4F6666696365</hex><name>Office</name></SSID></SSIDConfig></WLANProfile>'
        self.assertEqual(profile_ssid_from_xml(xml), b"Office")

    def test_hex_preserves_exact_bytes_and_takes_priority(self):
        xml = '<WLANProfile><SSIDConfig><SSID><hex>FFFE</hex><name>Wrong display name</name></SSID></SSIDConfig></WLANProfile>'
        self.assertEqual(profile_ssid_from_xml(xml), b"\xff\xfe")
        self.assertEqual(ssid_label(b"\xff\xfe"), "0xFFFE")

    def test_name_fallback_preserves_case_and_spaces(self):
        xml = '<WLANProfile><SSIDConfig><SSID><name>Office Wi-Fi</name></SSID></SSIDConfig></WLANProfile>'
        self.assertEqual(profile_ssid_from_xml(xml), b"Office Wi-Fi")

    def test_invalid_profile_does_not_guess(self):
        for xml in ['<WLANProfile><name>Office</name></WLANProfile>', '<bad',
                    '<WLANProfile><SSIDConfig><SSID><hex>ZZ</hex></SSID></SSIDConfig></WLANProfile>']:
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                profile_ssid_from_xml(xml)
