import json
import unittest

from duconnector.core import AccessPoint, Measurement, encode_ssid, parse_measurement, parse_output, pick_fastest


def sample(**changes):
    data = {"download": {"bandwidth": 12_500_000}, "upload": {"bandwidth": 2_500_000},
            "ping": {"latency": 12.5}, "server": {"id": 123},
            "result": {"url": "https://www.speedtest.net/result/c/test"}}
    data.update(changes)
    return json.dumps(data)


class CoreTests(unittest.TestCase):
    def test_ookla_units_and_url(self):
        item = parse_measurement(sample())
        self.assertEqual((item.download, item.upload, item.ping, item.server), (100, 20, 12.5, 123))
        self.assertTrue(item.url.endswith("/test"))

    def test_bad_measurements(self):
        for raw in ["{}", "{", "[]", sample(download={"bandwidth": -1}),
                    sample(download={"bandwidth": True}), sample(upload={"bandwidth": float("inf")}),
                    sample(ping={"latency": float("nan")}), sample(server={"id": 1.5}),
                    sample(server={"id": 0}), sample(server={"id": 2**31}),
                    sample(result={"url": 42}), sample(download={"bandwidth": 1e308})]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_measurement(raw)

    def test_diagnostic_lines_and_escaped_url(self):
        item = parse_output("Preparing...\n{\"error\": \"old\"}\n" + sample(result={"url": "https://example.com/a?x=\"b\""}) + "\n")
        self.assertEqual(item.url, 'https://example.com/a?x="b"')

    def test_download_gain_threshold_and_tie(self):
        first = Measurement(30, 10, 15, 1)
        weak = Measurement(100, 5, 30, 1)
        marginal = Measurement(33, 10, 5, 1)
        self.assertEqual(pick_fastest([first, weak, marginal], 0), 1)
        self.assertEqual(pick_fastest([first, None, marginal], 0), 0)
        self.assertEqual(pick_fastest([first, None, marginal], 0, gain=0), 2)
        self.assertEqual(pick_fastest([None] * 3), -1)
        self.assertEqual(pick_fastest([Measurement(5, 1, 1, 1), marginal], 0), 1)
        self.assertEqual(pick_fastest([Measurement(30, 1, 15, 1), Measurement(36, 1, 20, 1)], 0), 1)
        self.assertEqual(pick_fastest([Measurement(30, 1, 15, 1), Measurement(30, 1, 5, 1)], gain=0), 1)

    def test_utf8_ssid_limit_and_bands(self):
        self.assertEqual(encode_ssid("du"), b"du")
        self.assertEqual(len(encode_ssid("é" * 16)), 32)
        for text in ["", "é" * 17, "abc\x00"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                encode_ssid(text)
        self.assertEqual(AccessPoint("x", 1, -90, 2_437_000).band, "2.4 GHz")
        self.assertEqual(AccessPoint("x", 1, -90, 5_180_000).band, "5 GHz")
        self.assertEqual(AccessPoint("x", 1, -90, 5_955_000).band, "6 GHz")
