"""English Qt Widgets desktop interface with queued worker-thread updates."""

from datetime import datetime
import os
from pathlib import Path
import threading

from PySide6.QtCore import QSettings, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import APP_ID, APP_NAME, __version__
from .controller import Controller, Job
from .core import Network, encode_ssid, ssid_label
from .demo import DemoEngine, DemoWlan
from .speedtest import SpeedtestEngine
from .wlan import WindowsWlan

STYLE = """
QMainWindow, QWidget#central { background: #f3f6fb; color: #18263c; }
QWidget { color: #18263c; font-family: 'Segoe UI'; font-size: 9pt; }
QFrame#hero { background: #14243c; border-radius: 8px; }
QLabel#title { color: white; font-size: 18pt; font-weight: 700; }
QLabel#subtitle { color: #bbcce2; }
QGroupBox { background: white; border: 1px solid #dce4ef; border-radius: 8px;
            margin-top: 10px; padding: 6px 8px 6px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #263f60; }
QGroupBox#comparison, QGroupBox#comparison::title,
QGroupBox#comparison QLabel, QGroupBox#comparison QCheckBox,
QGroupBox#comparison QSpinBox { color: #000000; }
QLineEdit, QComboBox, QSpinBox { background: white; color: #18263c; border: 1px solid #cbd6e5;
                               border-radius: 5px; padding: 4px; min-height: 16px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #2977d5; }
QPushButton { background: white; color: #263f60; border: 1px solid #cbd6e5;
              border-radius: 5px; padding: 6px 10px; font-weight: 600; }
QPushButton:hover { background: #eaf2fc; border-color: #8badd9; }
QPushButton#primary { background: #256bd2; color: white; border-color: #256bd2; }
QPushButton#primary:hover { background: #1c59b4; }
QPushButton#stop { color: #ac3434; }
QPushButton:disabled { color: #8896a9; background: #edf1f6; border-color: #dce4ef; }
QCheckBox { spacing: 8px; }
QTableWidget { background: white; alternate-background-color: #f7f9fd; color: #18263c;
               border: 1px solid #dce4ef; border-radius: 6px; gridline-color: #edf1f6; }
QHeaderView::section { background: #eaf0f8; color: #324f73; border: 0;
                       border-bottom: 1px solid #dce4ef; padding: 9px; font-weight: 600; }
QTableWidget::item { padding: 5px; }
QTableWidget::item:selected { background: #dceafe; color: #18263c; }
QPlainTextEdit { background: #14243c; color: #d9e5f5; border: 0; border-radius: 6px; padding: 8px; }
QLabel#status { color: #256bd2; font-weight: 600; }
QLabel#current { color: #324f73; }
QLabel#demo { background: #fff1d4; color: #805512; border-radius: 5px; padding: 5px; }
QSplitter::handle { background: #dce4ef; height: 4px; }
"""


class JobThread(QThread):
    event = Signal(str, object)

    def __init__(self, wlan, engine_factory, job, parent=None):
        super().__init__(parent)
        self.wlan, self.engine_factory, self.job = wlan, engine_factory, job
        self.stop_event = threading.Event()

    def run(self):
        emit = self.event.emit
        try:
            engine = self.engine_factory(lambda text: emit("log", text))
            Controller(self.wlan, engine, emit, self.stop_event).run(self.job)
        except Exception as exc:
            emit("log", f"Worker initialization failed: {exc}")
            emit("status", "Operation failed. See the diagnostic log.")


class MainWindow(QMainWindow):
    def __init__(self, demo=False, wlan=None, persist=True):
        super().__init__()
        self.demo, self.persist = demo, persist
        self.wlan = wlan
        self.adapters = []
        self.profile_ssids = {}
        self.networks = []
        self.aps = []
        self.list_context = None
        self.worker = None
        self.close_pending = False
        self.busy_widgets = []
        self.settings = QSettings(APP_ID, "Settings") if persist else None
        self.setWindowTitle(f"{APP_NAME} {__version__}" + (" [DEMO]" if demo else ""))
        self.setStyleSheet(STYLE)
        self._build()
        width, height = 1040, 760
        if QApplication.platformName() == "windows" and self.screen():
            available = self.screen().availableGeometry()
            width = min(width, available.width() - 32)
            height = min(height, available.height() - 64)
        self.setFixedSize(width, height)
        self._load_settings()
        try:
            if self.wlan is None:
                self.wlan = DemoWlan() if demo else WindowsWlan()
            self.refresh_adapters()
        except Exception as exc:
            self.append_log(f"Wi-Fi initialization failed: {exc}")
            self.status.setText("Wi-Fi unavailable. Check the diagnostic log and Windows settings.")
        self.append_log(f"{APP_NAME} {__version__}.")
        self.append_log("Choose a saved network or use Find networks. For a new network, connect once in Windows Wi-Fi settings and complete its login.")
        self.append_log("All visible APs are eligible, including weak signals. Ranking uses download speed, then lower ping.")
        self.append_log("Pause other downloads and disable VPN for comparable tests. Speedtest binds to the selected adapter's IPv4.")
        if demo:
            self.append_log("DEMO MODE: all access points and measurements are simulated; Wi-Fi is not changed.")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_connection)
        self.timer.start(1500)
        self.update_connection()

    def _button(self, text, callback, busy=False, name=None):
        button = QPushButton(text)
        if name:
            button.setObjectName(name)
        button.clicked.connect(callback)
        if busy:
            self.busy_widgets.append(button)
        return button

    def _build(self):
        central = QWidget(objectName="central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(7)
        hero = QFrame(objectName="hero")
        header = QHBoxLayout(hero)
        header.setContentsMargins(14, 8, 14, 8)
        header.addWidget(QLabel(APP_NAME, objectName="title"))
        header.addStretch()
        header.addWidget(QLabel("Compare access points. Choose the best connection.", objectName="subtitle"))
        root.addWidget(hero)
        if self.demo:
            demo_label = QLabel("DEMO MODE - Simulated network and speed results. Your real Wi-Fi connection is unchanged.", objectName="demo")
            demo_label.setWordWrap(True)
            root.addWidget(demo_label)

        network = QGroupBox("Network")
        grid = QGridLayout(network)
        grid.setContentsMargins(10, 12, 10, 8)
        grid.setVerticalSpacing(5)
        self.adapter_box = QComboBox()
        self.adapter_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.adapter_box.currentIndexChanged.connect(self.adapter_changed)
        self.profile_box = QComboBox()
        self.profile_box.currentIndexChanged.connect(self.profile_changed)
        self.network_box = QComboBox()
        self.network_box.setEditable(True)
        self.network_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.ssid_edit = self.network_box.lineEdit()
        self.ssid_edit.setPlaceholderText("Select a network or enter its exact SSID")
        self.network_box.editTextChanged.connect(self.network_changed)
        grid.addWidget(QLabel("Wi-Fi adapter"), 0, 0)
        grid.addWidget(self.adapter_box, 0, 1, 1, 3)
        grid.addWidget(self._button("Refresh", self.refresh_adapters, True), 0, 4)
        grid.addWidget(QLabel("SSID"), 1, 0)
        grid.addWidget(self.network_box, 1, 1)
        grid.addWidget(QLabel("Saved profile"), 1, 2)
        grid.addWidget(self.profile_box, 1, 3)
        grid.addWidget(self._button("Find networks", lambda: self.start_job("networks"), True), 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 2)
        root.addWidget(network)

        comparison = QGroupBox("Comparison and monitoring", objectName="comparison")
        layout = QVBoxLayout(comparison)
        layout.setContentsMargins(10, 12, 10, 8)
        layout.setSpacing(5)
        consent_row = QHBoxLayout()
        self.consent = QCheckBox("I accept Ookla's terms, privacy policy, and speed test data usage.")
        consent_row.addWidget(self.consent, 1)
        consent_row.addWidget(self._button("Read terms", self.show_terms))
        layout.addLayout(consent_row)
        options = QHBoxLayout()
        self.monitor = QCheckBox("Monitor while moving")
        self.interval = QSpinBox()
        self.interval.setRange(3, 1440)
        self.interval.setValue(15)
        self.interval.setSuffix(" min")
        self.gain = QSpinBox()
        self.gain.setRange(0, 100)
        self.gain.setValue(20)
        self.gain.setSuffix(" %")
        options.addWidget(self.monitor)
        options.addStretch()
        options.addWidget(QLabel("Retest interval"))
        options.addWidget(self.interval)
        options.addSpacing(16)
        options.addWidget(QLabel("Required improvement"))
        options.addWidget(self.gain)
        layout.addLayout(options)
        actions = QHBoxLayout()
        actions.addWidget(self._button("1. Scan", lambda: self.start_job("scan"), True))
        actions.addWidget(self._button("2. Test all and select", lambda: self.start_job("best"), True, "primary"))
        actions.addWidget(self._button("Connect selected", lambda: self.start_job("connect"), True))
        actions.addWidget(self._button("Test selected", lambda: self.start_job("test"), True))
        self.stop_button = self._button("Stop / restore", self.stop_job, name="stop")
        self.stop_button.setEnabled(False)
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)
        root.addWidget(comparison)
        self.busy_widgets.extend([self.adapter_box, self.profile_box, self.network_box, self.consent,
                                  self.monitor, self.interval, self.gain])
        self.status = QLabel("Ready - choose a Wi-Fi network and its saved connection profile.", objectName="status")
        self.status.setFixedHeight(20)
        root.addWidget(self.status)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["BSSID", "Signal", "RSSI", "Band", "Download Mbps", "Upload Mbps", "Ping ms", "Status"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(80)
        splitter.addWidget(self.table)
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("Diagnostic log"), 1)
        log_header.addWidget(self._button("Save log", self.save_log))
        log_layout.addLayout(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(6000)
        self.log_view.setFont(QFont("Consolas", 9))
        self.log_view.setMinimumHeight(50)
        log_layout.addWidget(self.log_view)
        log_panel.setMinimumHeight(90)
        splitter.addWidget(log_panel)
        splitter.setChildrenCollapsible(False)
        splitter.setMinimumHeight(174)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)
        self.current = QLabel("Current connection: checking...", objectName="current")
        self.current.setFixedHeight(22)
        root.addWidget(self.current)
        links = QHBoxLayout()
        for title, url in [("Wi-Fi settings", "ms-settings:network-wifi"),
                           ("Network login", "http://neverssl.com"),
                           ("Speedtest website", "https://www.speedtest.net"),
                           ("Location permissions", "ms-settings:privacy-location")]:
            links.addWidget(self._button(title, lambda checked=False, target=url: self.open_url(target)))
        links.addStretch()
        root.addLayout(links)

    def _load_settings(self):
        if self.settings:
            hexadecimal = self.settings.value("ssid_hex", "", type=str)
            try:
                remembered = bytes.fromhex(hexadecimal)
                if remembered:
                    self._set_network(remembered)
            except ValueError:
                pass
            self.interval.setValue(self.settings.value("interval", 15, type=int))
            self.gain.setValue(self.settings.value("gain", 20, type=int))
        # Consent and monitoring always require explicit activation in each session.

    def _save_settings(self):
        if self.settings:
            try:
                self.settings.setValue("ssid_hex", self.target_ssid(allow_empty=True).hex())
            except ValueError:
                pass
            guid = self.adapter_box.currentData()
            if guid and self.profile_box.currentText():
                self.settings.setValue(f"profile/{guid}", self.profile_box.currentText())
            self.settings.setValue("interval", self.interval.value())
            self.settings.setValue("gain", self.gain.value())

    def append_log(self, text):
        self.log_view.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    @Slot()
    def clear_results(self):
        self.aps = []
        self.list_context = None
        self.table.setRowCount(0)

    def target_ssid(self, allow_empty=False):
        text = self.network_box.currentText()
        if not text and allow_empty:
            return b""
        stored = self.network_box.currentData()
        if isinstance(stored, bytes) and text == ssid_label(stored):
            return stored
        return encode_ssid(text)

    def _set_network(self, ssid):
        self.network_box.blockSignals(True)
        index = self.network_box.findData(ssid)
        if index < 0:
            self.network_box.addItem(ssid_label(ssid), ssid)
            index = self.network_box.count() - 1
        self.network_box.setCurrentIndex(index)
        self.network_box.blockSignals(False)
        self.network_changed()

    def _fill_networks(self, networks, selected=b""):
        self.network_box.blockSignals(True)
        self.network_box.clear()
        known = {ssid: Network(ssid) for ssid in self.profile_ssids.values()}
        known.update({network.ssid: network for network in networks})
        if selected:
            known.setdefault(selected, Network(selected))
        for ssid, network in sorted(known.items()):
            self.network_box.addItem(ssid_label(ssid), ssid)
            detail = f"{network.ap_count} visible access points" if network.ap_count else "Saved network; use Scan to find its access points"
            self.network_box.setItemData(self.network_box.count() - 1, detail, Qt.ItemDataRole.ToolTipRole)
        self.network_box.setCurrentIndex(self.network_box.findData(selected) if selected else -1)
        if not selected:
            self.network_box.setEditText("")
        self.network_box.blockSignals(False)
        self.network_changed()

    @Slot()
    def network_changed(self):
        self.clear_results()
        try:
            ssid = self.target_ssid(allow_empty=True)
        except ValueError:
            ssid = b""
        current = self.profile_box.currentText()
        matching = [name for name, value in self.profile_ssids.items() if value == ssid]
        preferred = current if current in matching else matching[0] if matching else ""
        self.profile_box.blockSignals(True)
        self.profile_box.setCurrentIndex(self.profile_box.findText(preferred) if preferred else -1)
        self.profile_box.blockSignals(False)

    @Slot()
    def profile_changed(self):
        ssid = self.profile_ssids.get(self.profile_box.currentText())
        if ssid:
            self._set_network(ssid)

    @Slot()
    def refresh_adapters(self):
        if self.worker:
            return
        try:
            if self.wlan is None:
                self.wlan = DemoWlan() if self.demo else WindowsWlan()
            previous = self.adapter_box.currentData()
            self.adapters = self.wlan.adapters()
            self.adapter_box.blockSignals(True)
            self.adapter_box.clear()
            for adapter in self.adapters:
                self.adapter_box.addItem(adapter.description, adapter.guid)
            preferred = next((i for i, ap in enumerate(self.adapters) if ap.guid == previous),
                             next((i for i, ap in enumerate(self.adapters) if ap.connected), 0))
            self.adapter_box.setCurrentIndex(preferred if self.adapters else -1)
            self.adapter_box.blockSignals(False)
            self.adapter_changed()
            if not self.adapters:
                self.append_log("No Wi-Fi adapter found. Check Wi-Fi, its driver, and WLAN AutoConfig.")
        except Exception as exc:
            self.adapter_box.blockSignals(False)
            self.append_log(str(exc))

    @Slot()
    def adapter_changed(self):
        self.clear_results()
        try:
            selected = self.target_ssid(allow_empty=True)
        except ValueError:
            selected = b""
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_ssids = {}
        self.networks = []
        guid = self.adapter_box.currentData()
        if not guid or not self.wlan:
            self.profile_box.blockSignals(False)
            self._fill_networks([], selected)
            return
        try:
            profiles = self.wlan.profiles(guid)
            self.profile_box.addItems(profiles)
            failed = []
            for profile in profiles:
                try:
                    self.profile_ssids[profile] = self.wlan.profile_ssid(guid, profile)
                except (OSError, ValueError) as exc:
                    failed.append(str(exc))
            if failed:
                self.append_log(f"Could not read the SSID of {len(failed)} saved profiles; select the network/profile manually. {failed[0]}")
            preferred = self.settings.value(f"profile/{guid}", "", type=str) if self.settings else ""
            try:
                active = self.wlan.snapshot(guid)
                if active and (not selected or active.ssid == selected):
                    selected = active.ssid
                    preferred = active.profile
            except OSError as exc:
                self.append_log(str(exc))
            if not selected and preferred in self.profile_ssids:
                selected = self.profile_ssids[preferred]
            self.profile_box.setCurrentIndex(profiles.index(preferred) if preferred in profiles else -1)
            self.profile_box.blockSignals(False)
            self._fill_networks([], selected)
            if self.profile_box.currentIndex() < 0:
                self.append_log("Select a saved connection profile. If missing, connect once through Windows Wi-Fi settings.")
        except Exception as exc:
            self.profile_box.blockSignals(False)
            self.append_log(str(exc))
        self.update_connection()

    @Slot()
    def update_connection(self):
        guid = self.adapter_box.currentData()
        if not guid or not self.wlan:
            self._show_connection("Current connection: no Wi-Fi adapter selected.")
            return
        try:
            active = self.wlan.snapshot(guid)
            if active:
                self._show_connection(f"Current connection: {active.ssid.decode('utf-8', errors='replace')}   |   BSSID: {active.bssid}   |   Profile: {active.profile}")
            else:
                self._show_connection("Current connection: disconnected. Check the log / Windows Wi-Fi settings.")
        except Exception as exc:
            self._show_connection(f"Current connection unavailable: {exc}")

    def _show_connection(self, text):
        self.current.setToolTip(text)
        self.current.setText(self.current.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, max(1, self.current.width())))

    def _set_busy(self, busy):
        for widget in self.busy_widgets:
            widget.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def start_job(self, mode):
        if self.worker:
            return
        try:
            guid = self.adapter_box.currentData()
            if not guid or not self.wlan:
                raise ValueError("Select a Wi-Fi adapter first.")
            ssid = b"" if mode == "networks" else self.target_ssid()
            selected = None
            if mode in {"connect", "test"}:
                row = self.table.currentRow()
                if row < 0 or row >= len(self.aps) or self.list_context != (guid, ssid):
                    raise ValueError("Scan the selected adapter/SSID and select an access point first.")
                selected = self.aps[row]
            job = Job(mode, guid, ssid, self.profile_box.currentText(), selected,
                      self.monitor.isChecked() and mode == "best", self.interval.value(),
                      self.gain.value() / 100, self.consent.isChecked())
            job.validate()
            mapped = self.profile_ssids.get(job.profile)
            if mode in {"connect", "test", "best"} and mapped is not None and mapped != ssid:
                raise ValueError("The selected saved profile belongs to a different SSID. Choose the matching profile.")
        except ValueError as exc:
            QMessageBox.information(self, APP_NAME, str(exc))
            return
        self._save_settings()
        factory = (lambda log: DemoEngine(self.wlan, log)) if self.demo else (lambda log: SpeedtestEngine(log=log))
        self.worker = JobThread(self.wlan, factory, job, self)
        self.worker.event.connect(self.on_event, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self.job_finished)
        self._set_busy(True)
        self.status.setText("Starting operation...")
        self.worker.start()

    @Slot(str, object)
    def on_event(self, kind, value):
        if kind == "log":
            self.append_log(value)
        elif kind == "status":
            self.status.setText(value)
        elif kind == "networks":
            try:
                selected = self.target_ssid(allow_empty=True)
            except ValueError:
                selected = b""
            self.networks = value
            self._fill_networks(value, selected)
        elif kind == "list":
            self.aps = value
            self.list_context = (self.worker.job.guid, self.worker.job.ssid) if self.worker else None
            self.table.setRowCount(len(value))
            for row, ap in enumerate(value):
                fields = [ap.bssid, f"{ap.quality}%", f"{ap.rssi} dBm", ap.band, "-", "-", "-", "Ready"]
                for column, field in enumerate(fields):
                    self.table.setItem(row, column, QTableWidgetItem(field))
        elif kind == "row":
            bssid, state, result = value
            row = next((i for i, ap in enumerate(self.aps) if ap.bssid == bssid), None)
            if row is None:
                return
            self.table.item(row, 7).setText(state)
            # Failed/cancelled retests clear earlier successful measurements.
            numbers = [f"{result.download:.2f}", f"{result.upload:.2f}", f"{result.ping:.2f}"] if result else ["-"] * 3
            for column, number in enumerate(numbers, 4):
                self.table.item(row, column).setText(number)
            for column in range(self.table.columnCount()):
                brush = QBrush(QColor("#e3f3e8")) if state == "Selected and connected" else QBrush()
                self.table.item(row, column).setBackground(brush)
            if state == "Selected and connected":
                self.table.selectRow(row)

    @Slot()
    def job_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self._set_busy(False)
        self.update_connection()
        if self.close_pending:
            self.close()

    @Slot()
    def stop_job(self):
        if self.worker:
            self.worker.stop_event.set()
            self.stop_button.setEnabled(False)
            self.status.setText("Stopping... waiting for any previous connection restoration.")

    def closeEvent(self, event):
        if self.worker:
            self.close_pending = True
            self.stop_job()
            event.ignore()
            return
        self.timer.stop()
        self._save_settings()
        if self.wlan:
            self.wlan.close()
        event.accept()

    def open_url(self, url):
        if not QDesktopServices.openUrl(QUrl(url)):
            self.append_log(f"Could not open {url}; open it manually.")

    def show_terms(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Speedtest terms and data usage")
        dialog.setText("Speed tests can consume substantial data. Switching access points may temporarily interrupt your connection.")
        dialog.setInformativeText("Ookla receives test, IP, and network information under its privacy policy. Accept the terms before running tests. Scan and manual connection do not require acceptance.\n\nEULA: https://www.speedtest.net/about/eula\nTerms: https://www.speedtest.net/about/terms\nPrivacy: https://www.speedtest.net/about/privacy")
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        button = dialog.addButton("Open Ookla terms", QMessageBox.ButtonRole.ActionRole)
        button.clicked.connect(lambda: self.open_url("https://www.speedtest.net/about/eula"))
        dialog.exec()

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save diagnostic log", f"{APP_ID}-log-{datetime.now():%Y%m%d-%H%M%S}.txt", "Text files (*.txt)")
        if path:
            try:
                Path(path).write_text(self.log_view.toPlainText() + "\n", encoding="utf-8")
                self.append_log(f"Diagnostic log saved to {path}. Review network details before sharing.")
            except OSError as exc:
                QMessageBox.warning(self, "Save log failed", str(exc))


def run_gui(demo=False):
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    mutex = None
    if os.name == "nt" and not demo:
        import ctypes as C
        kernel = C.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [C.c_void_p, C.c_int32, C.c_wchar_p]
        kernel.CreateMutexW.restype = C.c_void_p
        kernel.CloseHandle.argtypes = [C.c_void_p]
        kernel.CloseHandle.restype = C.c_int32
        # Same name as the C application: two selectors must not roam concurrently.
        mutex = kernel.CreateMutexW(None, False, "Local\\DUConnector.Native.1")
        if not mutex:
            QMessageBox.critical(None, APP_NAME, f"Could not create the application mutex: {C.FormatError(C.get_last_error())}")
            return 1
        if C.get_last_error() == 183:
            kernel.CloseHandle(mutex)
            QMessageBox.information(None, APP_NAME, "Another Wi-Fi AP Selector instance is already running.")
            return 0
    try:
        window = MainWindow(demo=demo)
        window.show()
        return app.exec()
    finally:
        if mutex:
            kernel.CloseHandle(mutex)
