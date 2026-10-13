"""Prepare and run the official Ookla CLI without blocking the Qt event loop."""

import os
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile

from .core import parse_output
from . import APP_ID, __version__
from .wlan import check_cancel

ENGINE_URL = "https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-win64.zip"
MAX_ARCHIVE = 20 * 1024 * 1024
MAX_EXECUTABLE = 40 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024


def valid_pe(path: Path) -> bool:
    """Check a downloaded file is an AMD64 PE, not an HTML login response."""
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return False
            offset = struct.unpack_from("<I", header, 60)[0]
            if not 64 <= offset <= 1_048_576:
                return False
            stream.seek(offset)
            signature = stream.read(6)
            return signature == b"PE\x00\x00\x64\x86"
    except OSError:
        return False


def run_process(command, stop, verify=None, timeout=180, log=lambda text: None):
    """Drain output in a reader thread while monitoring cancellation and BSSID."""
    check_cancel(stop)
    if verify and not verify():
        raise OSError("BSSID changed before Speedtest started; result discarded.")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, creationflags=flags, shell=False)
    chunks = queue.Queue(maxsize=64)
    reader_done = threading.Event()
    reader_stop = threading.Event()

    def reader():
        try:
            while not reader_stop.is_set():
                chunk = process.stdout.read1(8192)
                if not chunk:
                    break
                while not reader_stop.is_set():
                    try:
                        chunks.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except OSError:
            pass
        finally:
            reader_done.set()

    thread = threading.Thread(target=reader, name="Speedtest-output", daemon=True)
    thread.start()
    output, total = [], 0
    started = last_notice = last_verify = time.monotonic()
    try:
        while True:
            check_cancel(stop)
            now = time.monotonic()
            if now - started > timeout:
                raise TimeoutError(f"Speedtest timed out after {timeout} seconds.")
            if verify and now - last_verify >= 0.7:
                last_verify = now
                if not verify():
                    raise OSError("BSSID changed/disconnected during Speedtest; result discarded.")
            if now - last_notice >= 15:
                log(f"Speedtest is still running ({int(now - started)} seconds elapsed).")
                last_notice = now
            try:
                chunk = chunks.get(timeout=0.1)
                total += len(chunk)
                if total > MAX_OUTPUT:
                    raise OSError("Speedtest output exceeded the 4 MiB limit.")
                output.append(chunk)
            except queue.Empty:
                pass
            if process.poll() is not None and reader_done.is_set() and chunks.empty():
                break
        check_cancel(stop)
        if verify and not verify():
            raise OSError("BSSID changed at Speedtest completion; result discarded.")
        raw = b"".join(output).decode("utf-8", errors="replace")
        if process.returncode:
            raise OSError(f"Speedtest failed (exit {process.returncode}): {raw[-3000:]}")
        return raw
    finally:
        reader_stop.set()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        thread.join(timeout=5)
        process.stdout.close()


class SpeedtestEngine:
    def __init__(self, directory=None, log=lambda text: None):
        if directory is None:
            local = os.environ.get("LOCALAPPDATA")
            if not local:
                raise OSError("Cannot locate the Windows local application data directory.")
            directory = Path(local) / APP_ID / "ookla-1.2.0" / "engine"
            legacy = Path(local) / "DUConnector" / "ookla-1.2.0" / "engine"
            if not valid_pe(directory / "speedtest.exe") and valid_pe(legacy / "speedtest.exe"):
                directory = legacy
        self.directory = Path(directory)
        self.executable = self.directory / "speedtest.exe"
        self.log = log

    def prepare(self, stop):
        check_cancel(stop)
        if valid_pe(self.executable):
            self.log("Using the prepared official Ookla Speedtest engine.")
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log(f"Downloading the official Ookla CLI 1.2.0 from {ENGINE_URL}")
        # Temporary files stay on the destination volume for atomic replacement.
        with tempfile.TemporaryDirectory(prefix="download-", dir=self.directory) as temporary:
            archive_path = Path(temporary) / "engine.zip"
            request = urllib.request.Request(ENGINE_URL, headers={"User-Agent": f"{APP_ID}/{__version__}"})
            total, deadline = 0, time.monotonic() + 180
            with urllib.request.urlopen(request, timeout=15) as response, archive_path.open("wb") as output:
                if response.status != 200 or not response.url.startswith("https://"):
                    raise OSError("Official engine download did not return a valid HTTPS response.")
                while True:
                    check_cancel(stop)
                    if time.monotonic() > deadline:
                        raise TimeoutError("Engine download exceeded 180 seconds.")
                    chunk = response.read(32768)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE:
                        raise OSError("Official engine archive exceeded the 20 MiB limit.")
                    output.write(chunk)
            check_cancel(stop)
            # Read only the known executable; never extract arbitrary ZIP paths.
            with zipfile.ZipFile(archive_path) as archive:
                matches = [item for item in archive.infolist()
                           if Path(item.filename.replace("\\", "/")).name.lower() == "speedtest.exe"
                           and not item.is_dir()]
                if len(matches) != 1 or not 10_000 <= matches[0].file_size <= MAX_EXECUTABLE:
                    raise OSError("Official archive does not contain a valid-sized Speedtest executable.")
                candidate = Path(temporary) / "speedtest.exe"
                extracted = 0
                with archive.open(matches[0]) as source, candidate.open("wb") as output:
                    while chunk := source.read(32768):
                        check_cancel(stop)
                        extracted += len(chunk)
                        if extracted > MAX_EXECUTABLE:
                            raise OSError("Extracted engine exceeded the 40 MiB limit.")
                        output.write(chunk)
            if not valid_pe(candidate):
                raise OSError("The downloaded engine is not a Windows x64 executable.")
            check_cancel(stop)
            candidate.replace(self.executable)
        self.log(f"Official Speedtest engine is ready: {self.executable}")

    def measure(self, ipv4, server, stop, verify):
        command = [str(self.executable), "--accept-license", "--accept-gdpr",
                   "--format=json", "--progress=no", f"--ip={ipv4}"]
        if server:
            command.append(f"--server-id={server}")
        self.log(f"Running official Speedtest; Wi-Fi IPv4 {ipv4}, server {server or 'automatic'}.")
        result = parse_output(run_process(command, stop, verify, log=self.log))
        if server and result.server != server:
            raise OSError("Speedtest returned a different server; incomparable result discarded.")
        self.log(f"Download {result.download:.2f} Mbps | Upload {result.upload:.2f} Mbps | Ping {result.ping:.2f} ms | Server {result.server}")
        if result.url:
            self.log(f"Official result: {result.url}")
        return result
