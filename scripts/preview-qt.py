"""Save a Qt screenshot using clearly labeled simulated network results."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase
from duconnector.ui import MainWindow

app = QApplication([])
app.setStyle("Fusion")
# The Windows offscreen plugin has no system fonts until explicitly loaded.
if os.name == "nt":
    font_directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ["segoeui.ttf", "segoeuib.ttf", "consola.ttf"]:
        QFontDatabase.addApplicationFont(str(font_directory / filename))
window = MainWindow(demo=True, persist=False)
window.show()
window.consent.setChecked(True)
window.start_job("best")
deadline = time.monotonic() + 25
while window.worker and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
if window.worker:
    window.stop_job()
    window.worker.wait(5000)
    raise RuntimeError("Demo comparison did not finish")
app.processEvents()
output = Path(__file__).resolve().parents[1] / "docs" / "screenshot.png"
output.parent.mkdir(parents=True, exist_ok=True)
if not window.grab().save(str(output)):
    raise RuntimeError("Could not save Qt preview")
window.close()
print(output.resolve())
