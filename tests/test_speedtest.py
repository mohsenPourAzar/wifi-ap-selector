import io
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from duconnector.core import Cancelled
from duconnector.speedtest import SpeedtestEngine, run_process, valid_pe


class DownloadResponse(io.BytesIO):
    status = 200
    url = "https://install.speedtest.net/test.zip"


def fake_pe():
    raw = bytearray(10_000)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 128)
    raw[128:134] = b"PE\x00\x00\x64\x86"
    return bytes(raw)


def archive_bytes(filename="speedtest.exe", payload=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(filename, payload if payload is not None else fake_pe())
        archive.writestr("../../outside.txt", "must not be extracted")
    return stream.getvalue()


class SpeedtestTests(unittest.TestCase):
    def test_capture_large_output_without_pipe_deadlock(self):
        raw = run_process([sys.executable, "-c", "print('x'*200000)"], threading.Event(), timeout=10)
        self.assertEqual(len(raw.strip()), 200_000)

    def test_failed_process_reports_output(self):
        with self.assertRaisesRegex(OSError, "unavailable"):
            run_process([sys.executable, "-c", "import sys; print('unavailable'); sys.exit(3)"], threading.Event())

    def test_cancel_terminates_child(self):
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                run_process([sys.executable, "-c", "import time; time.sleep(20)"], stop)
        finally:
            timer.cancel()

    def test_timeout_terminates_child(self):
        with self.assertRaises(TimeoutError):
            run_process([sys.executable, "-c", "import time; time.sleep(20)"], threading.Event(), timeout=0.2)

    def test_roaming_terminates_child(self):
        verification = iter([True, False])
        with self.assertRaisesRegex(OSError, "BSSID changed"):
            run_process([sys.executable, "-c", "import time; time.sleep(20)"], threading.Event(), verify=lambda: next(verification))

    def test_download_only_known_executable_and_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = SpeedtestEngine(Path(folder) / "engine")
            with patch("urllib.request.urlopen", return_value=DownloadResponse(archive_bytes())) as request:
                engine.prepare(threading.Event())
                self.assertTrue(valid_pe(engine.executable))
                self.assertFalse((Path(folder) / "outside.txt").exists())
                engine.prepare(threading.Event())
                self.assertEqual(request.call_count, 1)
                self.assertEqual(list(engine.directory.iterdir()), [engine.executable])

    def test_bad_download_leaves_no_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = SpeedtestEngine(folder)
            with patch("urllib.request.urlopen", return_value=DownloadResponse(archive_bytes(payload=b"HTML" * 3000))):
                with self.assertRaisesRegex(OSError, "not a Windows x64"):
                    engine.prepare(threading.Event())
            self.assertFalse(engine.executable.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_wrong_server_rejected_and_arguments_bind_ip(self):
        output = '{"download":{"bandwidth":1},"upload":{"bandwidth":1},"ping":{"latency":2},"server":{"id":999}}'
        with tempfile.TemporaryDirectory() as folder, patch("duconnector.speedtest.run_process", return_value=output) as process:
            engine = SpeedtestEngine(folder)
            with self.assertRaisesRegex(OSError, "different server"):
                engine.measure("192.0.2.10", 123, threading.Event(), lambda: True)
            command = process.call_args.args[0]
            self.assertIn("--ip=192.0.2.10", command)
            self.assertIn("--server-id=123", command)
