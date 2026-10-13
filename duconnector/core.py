"""Platform-independent models, result parsing, and download-based selection."""

from dataclasses import dataclass
import json
import math


@dataclass(frozen=True)
class AccessPoint:
    bssid: str
    quality: int
    rssi: int
    frequency_khz: int

    @property
    def band(self) -> str:
        ghz = self.frequency_khz / 1_000_000
        if 2.4 <= ghz < 2.5:
            return "2.4 GHz"
        if 5 <= ghz < 5.925:
            return "5 GHz"
        if 5.925 <= ghz <= 7.125:
            return "6 GHz"
        return f"{ghz:.3f} GHz"


@dataclass(frozen=True)
class Adapter:
    guid: str
    description: str
    connected: bool = False


@dataclass(frozen=True)
class Network:
    ssid: bytes
    ap_count: int = 0


def ssid_label(value: bytes) -> str:
    """Display exact UTF-8 SSIDs, or hexadecimal for non-UTF-8 network names."""
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "0x" + value.hex().upper()


@dataclass(frozen=True)
class Snapshot:
    bssid: str
    ssid: bytes
    profile: str


@dataclass(frozen=True)
class Measurement:
    download: float
    upload: float
    ping: float
    server: int
    url: str = ""


class Cancelled(Exception):
    """An operation was stopped by the user."""


def encode_ssid(text: str) -> bytes:
    value = text.encode("utf-8")
    if not 1 <= len(value) <= 32 or b"\x00" in value:
        raise ValueError("SSID must contain 1 to 32 UTF-8 bytes and no NUL characters.")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Measurement fields must be numbers.")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError("Measurement exceeds the numeric range.") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("Measurement fields must be finite and nonnegative.")
    return result


def parse_measurement(raw: str) -> Measurement:
    """Parse Ookla JSON; bandwidth is bytes/second, converted to decimal Mbps."""
    try:
        data = json.loads(raw)
        download = _number(data["download"]["bandwidth"]) * 8 / 1_000_000
        upload = _number(data["upload"]["bandwidth"]) * 8 / 1_000_000
        ping = _number(data["ping"]["latency"])
        server = _number(data["server"]["id"])
        if not server.is_integer() or not 1 <= server <= 2_147_483_647:
            raise ValueError("Invalid Speedtest server ID.")
        if not math.isfinite(download) or not math.isfinite(upload):
            raise ValueError("Bandwidth exceeds the numeric range.")
        result = data.get("result") or {}
        url = result.get("url", "")
        if not isinstance(url, str):
            raise ValueError("Result URL must be a string.")
        return Measurement(download, upload, ping, int(server), url)
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid official Speedtest result JSON.") from exc


def parse_output(output: str) -> Measurement:
    """Allow diagnostic lines before the final JSON result."""
    try:
        return parse_measurement(output.strip())
    except ValueError:
        for line in reversed(output.splitlines()):
            if line.lstrip().startswith("{"):
                try:
                    return parse_measurement(line)
                except ValueError:
                    continue
    raise ValueError("No valid Speedtest measurement found in process output.")


def pick_fastest(results: list[Measurement | None], original: int = -1,
                 minimum: float = 20.0, gain: float = 0.2) -> int:
    """Prefer download, then lower ping; retain a working original below the gain."""
    eligible = [i for i, item in enumerate(results) if item is not None
                and math.isfinite(item.download) and item.download >= 0
                and math.isfinite(item.ping) and item.ping >= 0]
    if not eligible:
        return -1
    best = min(eligible, key=lambda i: (-results[i].download, results[i].ping))
    if original in eligible:
        previous = results[original]
        if previous.download >= minimum and results[best].download < previous.download * (1 + gain):
            return original
    return best
