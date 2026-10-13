"""Verify PE metadata, bundled source freshness, and frozen diagnostic startup."""

import hashlib
import json
from pathlib import Path
import subprocess

import pefile
from PyInstaller.archive.readers import CArchiveReader

root = Path(__file__).resolve().parents[1]
executable = root / "dist" / "WiFiAPSelector.exe"
pe = pefile.PE(str(executable), fast_load=False)
assert pe.FILE_HEADER.Machine == 0x8664, "Build must target AMD64"
assert pe.OPTIONAL_HEADER.Subsystem == 2, "Build must use the Windows GUI subsystem"
pe.close()

archive = CArchiveReader(str(executable))
pyz = archive.open_embedded_archive("PYZ.pyz")
modules = ["core", "controller", "wlan", "speedtest", "demo", "ui", "__main__"]
for module in modules:
    name = f"duconnector.{module}"
    bundled = pyz.extract(name)
    source = (root / "duconnector" / f"{module}.py").read_text(encoding="utf-8")
    current = compile(source, bundled.co_filename, "exec", dont_inherit=True, optimize=0)
    assert bundled == current, f"Bundled source is stale: {name}"

# This command exits without opening a window, changing Wi-Fi, or using Internet.
result = subprocess.run([str(executable), "--demo", "--diagnose"], timeout=30, check=False)
assert result.returncode == 0, f"Frozen diagnostic startup failed: {result.returncode}"
record = {
    "file": str(executable.relative_to(root)),
    "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    "bytes": executable.stat().st_size,
    "architecture": "AMD64",
    "subsystem": "Windows GUI",
    "implementation": "Python 3.14.7 / PySide6 6.12.0 / PyInstaller 6.22.3",
    "checks": ["PE architecture and GUI subsystem", "All application modules match final Python source",
               "Frozen executable exits successfully in simulated read-only diagnostics"],
}
(root / "build").mkdir(exist_ok=True)
(root / "build" / "package-verification.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
print(json.dumps(record, indent=2))
