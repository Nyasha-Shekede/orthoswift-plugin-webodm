"""OrthoSWIFT menu adapter for Agisoft Metashape Professional 2.2+."""
from __future__ import annotations
import json, os, shutil, sys, traceback
from pathlib import Path
import Metashape
from PySide2 import QtCore, QtGui, QtWidgets

HERE = Path(__file__).resolve().parent
BUNDLE = HERE
CORE = HERE / "core"
RUNNER = HERE / "runner.py"
FONTS_DIR = HERE / "assets" / "fonts"
CONFIG = HERE / "orthoswift_config.json"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    from version import __version__ as PLUGIN_VERSION
except Exception:
    try:
        from .version import __version__ as PLUGIN_VERSION
    except Exception:
        PLUGIN_VERSION = "1.0.0"

MINIMUM_METASHAPE_MAJOR_MINOR = (2, 2)

_processes = set()

# ── Load Bundled Fonts into Qt Engine (Offline 1:1 Rendering) ───────────────
_font_ids = []
if FONTS_DIR.is_dir():
    for _font_file in FONTS_DIR.glob("*.ttf"):
        _fid = QtGui.QFontDatabase.addApplicationFont(str(_font_file))
        if _fid != -1:
            _font_ids.append(_fid)

# ── Brand QSS (Grey & White Theme matching WebODM & orthoswift.net) ─────────
OSW_QSS = """
QWidget {
    background-color: #5a5a5a;
    color: #ffffff;
    font-family: "Inter", system-ui, -apple-system, sans-serif;
    font-size: 13px;
}

QDialog {
    background-color: #5a5a5a;
}

QScrollArea {
    background-color: transparent;
    border: none;
}

/* ── Hero Header ── */
QLabel#hero_title {
    font-family: "Space Grotesk", "Inter", system-ui, sans-serif;
    font-size: 32px;
    font-weight: 800;
    font-style: italic;
    color: #ffffff;
    letter-spacing: -0.05em;
}

/* ── Deliverables Table ── */
QTableWidget {
    background-color: rgba(0, 0, 0, 0.35);
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 10px;
    gridline-color: rgba(255, 255, 255, 0.15);
    color: #ffffff;
    selection-background-color: transparent;
    font-size: 12px;
    outline: none;
}
QTableWidget::item {
    padding: 10px 14px;
    border: none;
}
QHeaderView::section {
    background-color: rgba(0, 0, 0, 0.65);
    color: #ffffff;
    font-weight: 700;
    font-size: 12px;
    padding: 12px 14px;
    border: none;
    border-bottom: 1px solid rgba(255, 255, 255, 0.2);
    border-right: 1px solid rgba(255, 255, 255, 0.2);
}

/* ── Text inputs ── */
QLineEdit {
    background-color: rgba(0, 0, 0, 0.45);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 12px;
}
QLineEdit:hover {
    border-color: rgba(255, 255, 255, 0.7);
    background-color: rgba(0, 0, 0, 0.55);
}
QLineEdit:focus {
    border-color: #ffffff;
    background-color: rgba(0, 0, 0, 0.65);
}

/* ── Dropdown selects (ComboBoxes) ── */
QComboBox {
    background-color: rgba(0, 0, 0, 0.45);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 12px;
    min-height: 18px;
}
QComboBox:hover {
    border-color: rgba(255, 255, 255, 0.7);
    background-color: rgba(0, 0, 0, 0.55);
}
QComboBox:focus {
    border-color: #ffffff;
    background-color: rgba(0, 0, 0, 0.65);
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 26px;
    border-left: 1px solid rgba(255, 255, 255, 0.25);
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
    background-color: rgba(0, 0, 0, 0.25);
}
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #ffffff;
    width: 0;
    height: 0;
    margin-right: 2px;
}
QComboBox QAbstractItemView {
    background-color: #383838;
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.35);
    selection-background-color: rgba(255, 255, 255, 0.2);
    selection-color: #ffffff;
    padding: 4px;
    outline: none;
}

/* ── Checkboxes & Toggles ── */
QCheckBox {
    color: #ffffff;
    font-size: 12px;
    font-weight: 600;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 2px solid rgba(255, 255, 255, 0.5);
    background-color: rgba(0, 0, 0, 0.4);
}
QCheckBox::indicator:hover {
    border-color: #ffffff;
    background-color: rgba(0, 0, 0, 0.6);
}
QCheckBox::indicator:checked {
    background-color: #16a34a;
    border: 2px solid #22c55e;
}

/* ── Primary run button ── */
QPushButton#run_btn {
    background-color: #ffffff;
    color: #000000;
    font-weight: 800;
    font-size: 13px;
    border: none;
    border-radius: 10px;
    padding: 12px 28px;
}
QPushButton#run_btn:hover {
    background-color: rgba(255, 255, 255, 0.9);
}
QPushButton#run_btn:disabled {
    background-color: rgba(255, 255, 255, 0.3);
    color: rgba(0, 0, 0, 0.5);
}

/* ── Secondary button ── */
QPushButton {
    background-color: rgba(0, 0, 0, 0.4);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: rgba(255, 255, 255, 0.1);
    border-color: rgba(255, 255, 255, 0.4);
}

/* ── Progress Dialog Card ── */
QProgressDialog {
    background-color: #5a5a5a;
    color: #ffffff;
    min-width: 480px;
    min-height: 180px;
}
QProgressDialog QLabel {
    color: #ffffff;
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 12px;
    background: transparent;
}
QProgressDialog QPushButton {
    background-color: rgba(0, 0, 0, 0.4);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 8px;
    padding: 8px 20px;
    font-size: 12px;
    font-weight: 600;
    margin-top: 16px;
}
QProgressDialog QPushButton:hover {
    background-color: rgba(255, 255, 255, 0.1);
    border-color: rgba(255, 255, 255, 0.4);
}
QProgressBar {
    background-color: rgba(0, 0, 0, 0.4);
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: #ffffff;
    font-size: 11px;
    font-weight: 700;
}
QProgressBar::chunk {
    background-color: #4ade80;
    border-radius: 5px;
}
"""


def _msg(text, title="OrthoSWIFT", is_error=False):
    """Styled OrthoSWIFT message dialog — replaces the plain Agisoft messageBox."""
    dlg = QtWidgets.QDialog(QtWidgets.QApplication.activeWindow())
    dlg.setWindowTitle(title)
    dlg.setWindowFlags(
        (dlg.windowFlags() | QtCore.Qt.CustomizeWindowHint)
        & ~QtCore.Qt.WindowContextHelpButtonHint
    )
    dlg.setStyleSheet(OSW_QSS)
    dlg.setMinimumWidth(420)

    root = QtWidgets.QVBoxLayout(dlg)
    root.setContentsMargins(28, 24, 28, 20)
    root.setSpacing(16)

    # Icon + message row
    row = QtWidgets.QHBoxLayout()
    row.setSpacing(14)
    icon_lbl = QtWidgets.QLabel("✕" if is_error else "✔")
    icon_lbl.setStyleSheet(
        f"font-size:22px;font-weight:900;color:{'#f87171' if is_error else '#4ade80'};"
        "background:transparent;border:none;"
    )
    icon_lbl.setAlignment(QtCore.Qt.AlignTop)
    icon_lbl.setFixedWidth(26)

    msg_lbl = QtWidgets.QLabel(str(text))
    msg_lbl.setWordWrap(True)
    msg_lbl.setStyleSheet("font-size:13px;color:#ffffff;background:transparent;border:none;line-height:1.5;")
    msg_lbl.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)

    row.addWidget(icon_lbl)
    row.addWidget(msg_lbl, 1)
    root.addLayout(row)

    # OK button
    btn = QtWidgets.QPushButton("OK")
    btn.setObjectName("run_btn")
    btn.setFixedWidth(100)
    btn.clicked.connect(dlg.accept)
    btn_row = QtWidgets.QHBoxLayout()
    btn_row.addStretch()
    btn_row.addWidget(btn)
    root.addLayout(btn_row)

    dlg.exec_()


def _success_card(archive: Path, out: Path):
    """Branded OrthoSWIFT success card shown after a completed job."""
    dlg = QtWidgets.QDialog(QtWidgets.QApplication.activeWindow())
    dlg.setWindowTitle("Job Complete")
    dlg.setWindowFlags(
        (dlg.windowFlags() | QtCore.Qt.CustomizeWindowHint)
        & ~QtCore.Qt.WindowContextHelpButtonHint
    )
    dlg.setStyleSheet(OSW_QSS)
    dlg.setMinimumWidth(480)

    root = QtWidgets.QVBoxLayout(dlg)
    root.setContentsMargins(28, 24, 28, 20)
    root.setSpacing(18)

    # ── Header ──────────────────────────────────────────────────────────────
    header = QtWidgets.QLabel(
        "<span style='font-size:22px;font-weight:900;color:#4ade80;'>&#10003;</span>"
        "&nbsp;&nbsp;<span style='font-size:17px;font-weight:800;color:#ffffff;'>"
        "OrthoSWIFT finished successfully</span>"
    )
    header.setStyleSheet("background:transparent;border:none;")
    root.addWidget(header)

    # ── Divider ──────────────────────────────────────────────────────────────
    line = QtWidgets.QFrame()
    line.setFrameShape(QtWidgets.QFrame.HLine)
    line.setStyleSheet("color:rgba(255,255,255,0.15);background:rgba(255,255,255,0.15);border:none;max-height:1px;")
    root.addWidget(line)

    # ── Path info card ───────────────────────────────────────────────────────
    card = QtWidgets.QFrame()
    card.setObjectName("success_path_card")
    card.setStyleSheet("""
        QFrame#success_path_card {
            background-color: rgba(0,0,0,0.3);
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 10px;
            padding: 16px 20px;
        }
        QFrame#success_path_card QLabel {
            background: transparent;
            border: none;
        }
    """)
    card_layout = QtWidgets.QVBoxLayout(card)
    card_layout.setSpacing(10)
    card_layout.setContentsMargins(0, 0, 0, 0)

    def _path_row(icon, label, path_str):
        h = QtWidgets.QHBoxLayout()
        h.setSpacing(10)
        ic = QtWidgets.QLabel(icon)
        ic.setStyleSheet(
            "font-size:9px;font-weight:800;color:#4ade80;"
            "background:transparent;border:none;letter-spacing:0.05em;"
        )
        ic.setFixedWidth(28)
        ic.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        lbl = QtWidgets.QLabel(f"<b style='color:rgba(255,255,255,0.6);font-size:11px;'>{label}</b>"
                               f"<br><span style='color:#ffffff;font-size:12px;'>{path_str}</span>")
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        h.addWidget(ic)
        h.addWidget(lbl, 1)
        return h

    card_layout.addLayout(_path_row("ZIP", "Deliverables ZIP", str(archive)))
    card_layout.addLayout(_path_row("DIR", "Results Folder", str(out)))
    root.addWidget(card)

    # ── Buttons ──────────────────────────────────────────────────────────────
    btn_row = QtWidgets.QHBoxLayout()
    btn_row.setSpacing(10)
    btn_row.addStretch()

    open_btn = QtWidgets.QPushButton("Open Folder")
    open_btn.setFixedWidth(120)
    open_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(out))))

    ok_btn = QtWidgets.QPushButton("Done")
    ok_btn.setObjectName("run_btn")
    ok_btn.setFixedWidth(100)
    ok_btn.clicked.connect(dlg.accept)

    btn_row.addWidget(open_btn)
    btn_row.addWidget(ok_btn)
    root.addLayout(btn_row)

    dlg.exec_()


def _python():
    configured = os.environ.get("ORTHOSWIFT_PYTHON")
    if CONFIG.is_file():
        try:
            configured = json.loads(CONFIG.read_text(encoding="utf-8")).get("python") or configured
        except Exception as exc:
            print(f"OrthoSWIFT warning: ignored invalid configuration {CONFIG}: {exc}")
    for candidate in (configured, shutil.which("python3"), shutil.which("python")):
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError(
        "OrthoSWIFT Python is not configured. Run orthoswift/install.py once, "
        "or set ORTHOSWIFT_PYTHON to the external Python executable."
    )


def _check_runtime():
    parts = tuple(int(value) for value in Metashape.app.version.split(".")[:2])
    if parts < MINIMUM_METASHAPE_MAJOR_MINOR:
        raise RuntimeError(
            f"OrthoSWIFT requires Metashape 2.2 or newer; detected {Metashape.app.version}."
        )
    if parts > MINIMUM_METASHAPE_MAJOR_MINOR:
        print(f"OrthoSWIFT notice: running on newer Metashape {Metashape.app.version}; verify one export before field use.")
    for required in (BUNDLE, RUNNER, CORE):
        if not required.exists():
            raise RuntimeError(f"Incomplete OrthoSWIFT installation: missing {required}")


class OrthoSwiftRunDialog(QtWidgets.QDialog):
    """Agisoft Metashape dialog matching the WebODM UI, supporting both active chunk and direct file pick."""
    def __init__(self, chunk=None, parent=None):
        super().__init__(parent)
        self.chunk = chunk
        self.setWindowTitle("Field Analysis")
        self.resize(1100, 780)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint | QtCore.Qt.WindowMinimizeButtonHint)
        self.setStyleSheet(OSW_QSS)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(20)

        # ── Hero Header ──
        hero_title = QtWidgets.QLabel("OrthoSWIFT")
        hero_title.setObjectName("hero_title")
        title_font = QtGui.QFont()
        title_font.setFamilies(["Space Grotesk", "SpaceGrotesk", "Arial Black", "Segoe UI Black", "sans-serif"])
        title_font.setPixelSize(32)
        title_font.setWeight(QtGui.QFont.ExtraBold)
        title_font.setItalic(True)
        title_font.setLetterSpacing(QtGui.QFont.PercentageSpacing, 95.0)
        hero_title.setFont(title_font)

        hero_sub = QtWidgets.QLabel("<p style='font-size:13px;color:rgba(255,255,255,0.75);margin:2px 0 0 0;'>Produce prescription files and agricultural analytics for free on your computer.</p>")
        layout.addWidget(hero_title)
        layout.addWidget(hero_sub)

        # ── Deliverables Table ──
        deliverables = [
            ("Fertilizer Zone Map", "Shapefile + controller packages (with offline MBTiles basemap)", "Drives variable-rate fertilizer spreaders. Includes full-resolution offline orthomosaic background map for your tractor or drone display."),
            ("Targeted Spray Map", "Shapefile + controller packages (with offline MBTiles basemap)", "Triggers spray nozzles over stressed crop patches. Includes full-resolution offline orthomosaic background map for your tractor or drone display."),
            ("Stress Hotspot Map", "GeoJSON + KML + CSV", "Pinpoints lowest-performing field zones for targeted ground scouting."),
            ("Field Health Summary", "PDF + PNG map", "Ready-to-share report combining zone maps, cover stats, and scouting targets."),
            ("Technical GIS & Audit Data", "GeoTIFF + CSV + JSON", "Raw spectral health layers (NDVI/NDRE/MSAVI2), per-zone statistics, and audit log."),
        ]

        table = QtWidgets.QTableWidget(len(deliverables), 3)
        table.setHorizontalHeaderLabels(["Deliverable", "Format", "What it's for"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        table.setShowGrid(True)
        table.setWordWrap(True)
        table.setTextElideMode(QtCore.Qt.ElideNone)
        
        # Responsive proportional column widths
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        table.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        table.setMinimumHeight(240)

        green_color = QtGui.QColor("#4ade80")
        for row, (name, fmt, desc) in enumerate(deliverables):
            item_name = QtWidgets.QTableWidgetItem(name)
            item_name.setForeground(green_color)
            font = item_name.font()
            font.setBold(True)
            item_name.setFont(font)
            
            item_fmt = QtWidgets.QTableWidgetItem(fmt)
            font_fmt = item_fmt.font()
            font_fmt.setBold(True)
            item_fmt.setFont(font_fmt)

            item_desc = QtWidgets.QTableWidgetItem(desc)
            table.setItem(row, 0, item_name)
            table.setItem(row, 1, item_fmt)
            table.setItem(row, 2, item_desc)

        layout.addWidget(table)

        # ── Teams Section Collapsible Accordion (Matching WebODM) ──
        teams_wrapper = QtWidgets.QWidget()
        teams_wrapper.setObjectName("teams_wrapper")
        teams_wrapper.setMaximumWidth(800)
        teams_wrap_layout = QtWidgets.QVBoxLayout(teams_wrapper)
        teams_wrap_layout.setContentsMargins(0, 0, 0, 0)
        teams_wrap_layout.setSpacing(0)

        self.teams_toggle = QtWidgets.QPushButton("Running multiple spray drones or tractors?  ▼")
        self.teams_toggle.setObjectName("teams_toggle_btn")
        self.teams_toggle.setStyleSheet("""
            QPushButton#teams_toggle_btn {
                background-color: #ffffff;
                color: #000000;
                font-weight: 800;
                font-size: 13px;
                text-align: left;
                padding: 12px 18px;
                border-radius: 10px;
                border: none;
            }
            QPushButton#teams_toggle_btn:hover {
                background-color: rgba(255, 255, 255, 0.95);
            }
        """)
        teams_wrap_layout.addWidget(self.teams_toggle)

        self.teams_box = QtWidgets.QFrame()
        self.teams_box.setObjectName("teams_dropdown_box")
        self.teams_box.setStyleSheet("""
            QFrame#teams_dropdown_box {
                background-color: rgba(0, 0, 0, 0.35);
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-top: none;
                border-bottom-left-radius: 10px;
                border-bottom-right-radius: 10px;
                padding: 22px 24px;
            }
            QFrame#teams_dropdown_box QLabel {
                background: transparent;
                border: none;
            }
        """)
        teams_layout = QtWidgets.QVBoxLayout(self.teams_box)
        teams_layout.setSpacing(16)
        teams_layout.setContentsMargins(0, 0, 0, 0)

        teams_header = QtWidgets.QLabel("<h2 style='font-size:16px;font-weight:700;color:#ffffff;margin:0 0 2px 0;'>OrthoSWIFT Cloud</h2><p style='font-size:12px;color:rgba(255,255,255,0.75);margin:0;'>Produce all standard prescriptions in the cloud, scaled for commercial operations.</p>")
        teams_layout.addWidget(teams_header)

        # Card container for bullets matching WebODM .osw-teams-card
        inner_card = QtWidgets.QFrame()
        inner_card.setObjectName("teams_inner_card")
        inner_card.setStyleSheet("""
            QFrame#teams_inner_card {
                background-color: rgba(0, 0, 0, 0.3);
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 10px;
                padding: 20px 24px;
            }
            QFrame#teams_inner_card QLabel {
                background: transparent;
                border: none;
            }
        """)
        inner_layout = QtWidgets.QVBoxLayout(inner_card)
        inner_layout.setSpacing(16)
        inner_layout.setContentsMargins(0, 0, 0, 0)

        features = [
            ("Orthomosaic Merging", "Merge overlapping orthomosaics from multiple flights or sensors automatically."),
            ("Drone Swarm Support", "Splits prescription packages into zones for swarm workflows."),
            ("Batch Field Processing", "Process multiple separate fields independently in a single job."),
            ("Fully Automated Pipeline", "Zero manual GIS work, with most jobs completing in under 1 hour."),
        ]

        for title, desc in features:
            item_row = QtWidgets.QHBoxLayout()
            item_row.setSpacing(12)
            item_row.setContentsMargins(0, 0, 0, 0)
            item_row.setAlignment(QtCore.Qt.AlignTop)

            bullet = QtWidgets.QLabel("<span style='color:#4ade80;font-size:18px;font-weight:900;'>&#8226;</span>")
            bullet.setFixedWidth(14)
            bullet.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)

            text_box = QtWidgets.QLabel(f"<b style='color:#4ade80;font-size:13px;line-height:1.2;'>{title}</b><p style='color:rgba(255,255,255,0.9);font-size:12px;margin:4px 0 0 0;line-height:1.5;'>{desc}</p>")
            text_box.setWordWrap(True)
            text_box.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)

            item_row.addWidget(bullet)
            item_row.addWidget(text_box, 1)
            inner_layout.addLayout(item_row)

        teams_layout.addWidget(inner_card)

        pricing_label = QtWidgets.QLabel("<div style='margin:4px 0 4px 0;'><span style='color:#4ade80;font-size:12px;font-weight:700;'>Export to all major controller formats.</span></div>")
        teams_layout.addWidget(pricing_label)

        start_proc_btn = QtWidgets.QPushButton("Start Processing")
        start_proc_btn.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                color: #000000;
                font-weight: 800;
                font-size: 12px;
                border-radius: 8px;
                padding: 10px 22px;
                max-width: 140px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.9);
            }
        """)
        start_proc_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://orthoswift.net")))
        teams_layout.addWidget(start_proc_btn)

        self.teams_box.setVisible(False)
        self.teams_toggle.clicked.connect(self._toggle_teams_accordion)
        teams_wrap_layout.addWidget(self.teams_box)
        layout.addWidget(teams_wrapper)

        # ── Fertilizer Rate Plan ─────────────────────────────────────────────
        rate_card = QtWidgets.QFrame()
        rate_card.setStyleSheet("QFrame{background-color:rgba(0,0,0,0.35);border:1px solid rgba(255,255,255,0.25);border-radius:10px;}")
        rate_card_layout = QtWidgets.QVBoxLayout(rate_card)
        rate_card_layout.setContentsMargins(18, 16, 18, 18)
        rate_card_layout.setSpacing(12)

        rate_header = QtWidgets.QHBoxLayout()
        rate_label_block = QtWidgets.QVBoxLayout()
        rate_title = QtWidgets.QLabel("Fertilizer rate plan")
        rate_title.setStyleSheet("font-size:13px;font-weight:700;color:#ffffff;border:none;background:transparent;")
        self._rate_desc = QtWidgets.QLabel("Default: relative 0-100% intensity. Enable to supply physical application rates that are encoded directly into controller files.")
        self._rate_desc.setStyleSheet("font-size:11px;color:rgba(255,255,255,0.65);border:none;background:transparent;")
        self._rate_desc.setWordWrap(True)
        rate_label_block.addWidget(rate_title)
        rate_label_block.addWidget(self._rate_desc)
        rate_header.addLayout(rate_label_block, 1)

        self._rate_toggle = QtWidgets.QCheckBox("Enable physical rate plan")
        self._rate_toggle.setStyleSheet("QCheckBox{color:#ffffff;font-size:12px;font-weight:600;border:none;background:transparent;}")
        self._rate_toggle.stateChanged.connect(self._on_rate_toggle)
        rate_header.addWidget(self._rate_toggle)
        rate_card_layout.addLayout(rate_header)

        # Fields (hidden by default)
        self._rate_fields_widget = QtWidgets.QWidget()
        self._rate_fields_widget.setStyleSheet("background:transparent;")
        rf = QtWidgets.QVBoxLayout(self._rate_fields_widget)
        rf.setContentsMargins(0, 8, 0, 0)
        rf.setSpacing(10)

        row1 = QtWidgets.QHBoxLayout()
        pn_block = QtWidgets.QVBoxLayout()
        pn_label = QtWidgets.QLabel("Product name")
        pn_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._product_name = QtWidgets.QLineEdit()
        self._product_name.setPlaceholderText("e.g. Urea 46-0-0")
        pn_block.addWidget(pn_label)
        pn_block.addWidget(self._product_name)

        ru_block = QtWidgets.QVBoxLayout()
        ru_label = QtWidgets.QLabel("Rate unit")
        ru_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._rate_unit = QtWidgets.QComboBox()
        self._rate_unit.addItems(["KG_HA  (kg / ha)", "L_HA  (L / ha)", "LB_AC  (lb / acre)", "GAL_AC  (gal / acre)", "SEEDS_HA  (seeds / ha)"])
        self._rate_unit.currentIndexChanged.connect(self._on_rate_unit_changed)
        ru_block.addWidget(ru_label)
        ru_block.addWidget(self._rate_unit)
        row1.addLayout(pn_block)
        row1.addLayout(ru_block)
        rf.addLayout(row1)

        row2 = QtWidgets.QHBoxLayout()
        st_block = QtWidgets.QVBoxLayout()
        st_label = QtWidgets.QLabel("Strategy")
        st_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._rate_strategy = QtWidgets.QComboBox()
        self._rate_strategy.addItems(["direct  (High vigor = High rate)", "inverse  (Low vigor = High rate)"])
        st_block.addWidget(st_label)
        st_block.addWidget(self._rate_strategy)

        rb_block = QtWidgets.QVBoxLayout()
        rb_label = QtWidgets.QLabel("Rate basis")
        rb_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._rate_basis = QtWidgets.QComboBox()
        self._rate_basis.addItems(["product  (Total product)", "active_ingredient  (Active ingredient)", "nutrient  (Nutrient)"])
        rb_block.addWidget(rb_label)
        rb_block.addWidget(self._rate_basis)
        row2.addLayout(st_block)
        row2.addLayout(rb_block)
        rf.addLayout(row2)

        row3 = QtWidgets.QHBoxLayout()
        self._min_label = QtWidgets.QLabel("Min rate (KG_HA)")
        self._min_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._min_rate = QtWidgets.QLineEdit()
        self._min_rate.setPlaceholderText("e.g. 80")
        self._min_rate.setValidator(QtGui.QDoubleValidator(0, 99999, 2, self._min_rate))
        minr_block = QtWidgets.QVBoxLayout()
        minr_block.addWidget(self._min_label)
        minr_block.addWidget(self._min_rate)

        self._max_label = QtWidgets.QLabel("Max rate (KG_HA)")
        self._max_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._max_rate = QtWidgets.QLineEdit()
        self._max_rate.setPlaceholderText("e.g. 180")
        self._max_rate.setValidator(QtGui.QDoubleValidator(0, 99999, 2, self._max_rate))
        maxr_block = QtWidgets.QVBoxLayout()
        maxr_block.addWidget(self._max_label)
        maxr_block.addWidget(self._max_rate)
        row3.addLayout(minr_block)
        row3.addLayout(maxr_block)
        rf.addLayout(row3)

        ab_label = QtWidgets.QLabel("Approved by (Agronomist / Operator)")
        ab_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._approved_by = QtWidgets.QLineEdit()
        self._approved_by.setPlaceholderText("Operator name or email")
        rf.addWidget(ab_label)
        rf.addWidget(self._approved_by)

        hint = QtWidgets.QLabel("OrthoSWIFT encodes these physical rates into controller shapefiles (including John Deere kg_p_ha / l_p_ha columns) and the PDF action report.")
        hint.setStyleSheet("font-size:10px;color:rgba(255,255,255,0.5);border:none;background:transparent;")
        hint.setWordWrap(True)
        rf.addWidget(hint)

        rate_card_layout.addWidget(self._rate_fields_widget)
        self._rate_fields_widget.setVisible(False)
        layout.addWidget(rate_card)

        # ── Spot Spraying Target Rate ────────────────────────────────────────
        spot_rate_card = QtWidgets.QFrame()
        spot_rate_card.setStyleSheet("QFrame{background-color:rgba(0,0,0,0.35);border:1px solid rgba(255,255,255,0.25);border-radius:10px;}")
        spot_card_layout = QtWidgets.QVBoxLayout(spot_rate_card)
        spot_card_layout.setContentsMargins(18, 16, 18, 18)
        spot_card_layout.setSpacing(12)

        spot_header = QtWidgets.QHBoxLayout()
        spot_label_block = QtWidgets.QVBoxLayout()
        spot_title = QtWidgets.QLabel("Spot spraying target rate")
        spot_title.setStyleSheet("font-size:13px;font-weight:700;color:#ffffff;border:none;background:transparent;")
        self._spot_rate_desc = QtWidgets.QLabel("Default: binary section control (100% on weed/stress hotspots, 0% off-target). Enable to supply a custom target spray rate.")
        self._spot_rate_desc.setStyleSheet("font-size:11px;color:rgba(255,255,255,0.65);border:none;background:transparent;")
        self._spot_rate_desc.setWordWrap(True)
        spot_label_block.addWidget(spot_title)
        spot_label_block.addWidget(self._spot_rate_desc)
        spot_header.addLayout(spot_label_block, 1)

        self._spot_rate_toggle = QtWidgets.QCheckBox("Enable custom target rate")
        self._spot_rate_toggle.setStyleSheet("QCheckBox{color:#ffffff;font-size:12px;font-weight:600;border:none;background:transparent;}")
        self._spot_rate_toggle.stateChanged.connect(self._on_spot_rate_toggle)
        spot_header.addWidget(self._spot_rate_toggle)
        spot_card_layout.addLayout(spot_header)

        # Fields (hidden by default)
        self._spot_rate_fields_widget = QtWidgets.QWidget()
        self._spot_rate_fields_widget.setStyleSheet("background:transparent;")
        srf = QtWidgets.QVBoxLayout(self._spot_rate_fields_widget)
        srf.setContentsMargins(0, 8, 0, 0)
        srf.setSpacing(10)

        srow1 = QtWidgets.QHBoxLayout()
        spn_block = QtWidgets.QVBoxLayout()
        spn_label = QtWidgets.QLabel("Product name")
        spn_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._spot_product_name = QtWidgets.QLineEdit()
        self._spot_product_name.setPlaceholderText("e.g. Roundup PowerMAX")
        spn_block.addWidget(spn_label)
        spn_block.addWidget(self._spot_product_name)

        sru_block = QtWidgets.QVBoxLayout()
        sru_label = QtWidgets.QLabel("Rate unit")
        sru_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._spot_rate_unit = QtWidgets.QComboBox()
        self._spot_rate_unit.addItems(["L_HA  (L / ha)", "GAL_AC  (gal / acre)"])
        self._spot_rate_unit.currentIndexChanged.connect(self._on_spot_rate_unit_changed)
        sru_block.addWidget(sru_label)
        sru_block.addWidget(self._spot_rate_unit)
        srow1.addLayout(spn_block)
        srow1.addLayout(sru_block)
        srf.addLayout(srow1)

        self._spot_target_label = QtWidgets.QLabel("Target application rate (L_HA)")
        self._spot_target_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._spot_target_rate = QtWidgets.QLineEdit()
        self._spot_target_rate.setPlaceholderText("e.g. 150")
        self._spot_target_rate.setValidator(QtGui.QDoubleValidator(0, 99999, 2, self._spot_target_rate))
        srf.addWidget(self._spot_target_label)
        srf.addWidget(self._spot_target_rate)

        sab_label = QtWidgets.QLabel("Approved by (Agronomist / Operator)")
        sab_label.setStyleSheet("font-size:11px;font-weight:700;color:rgba(255,255,255,0.8);border:none;background:transparent;")
        self._spot_approved_by = QtWidgets.QLineEdit()
        self._spot_approved_by.setPlaceholderText("Operator name or email")
        srf.addWidget(sab_label)
        srf.addWidget(self._spot_approved_by)

        spot_hint = QtWidgets.QLabel("OrthoSWIFT encodes these physical rates into targeted spot-spraying controller packages (such as DJI Agras/XAG/universal) for section control.")
        spot_hint.setStyleSheet("font-size:10px;color:rgba(255,255,255,0.5);border:none;background:transparent;")
        spot_hint.setWordWrap(True)
        srf.addWidget(spot_hint)

        spot_card_layout.addWidget(self._spot_rate_fields_widget)
        self._spot_rate_fields_widget.setVisible(False)
        layout.addWidget(spot_rate_card)

        # ── Analysis Form (Multispectral Orthomosaic File Selection) ──
        form_title = QtWidgets.QLabel("<h2 style='font-size:16px;font-weight:700;color:#ffffff;margin:16px 0 4px 0;'>Select multispectral orthomosaic</h2>")
        layout.addWidget(form_title)

        # WebODM style file input capsule
        file_capsule = QtWidgets.QFrame()
        file_capsule.setStyleSheet("""
            QFrame {
                background-color: rgba(0, 0, 0, 0.45);
                border: 1px solid rgba(255, 255, 255, 0.35);
                border-radius: 10px;
                padding: 6px 8px;
                max-width: 540px;
            }
        """)
        capsule_layout = QtWidgets.QHBoxLayout(file_capsule)
        capsule_layout.setContentsMargins(6, 6, 6, 6)
        capsule_layout.setSpacing(12)

        self.choose_file_btn = QtWidgets.QPushButton("Choose File")
        self.choose_file_btn.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                color: #000000;
                font-weight: 700;
                font-size: 12px;
                border: none;
                border-radius: 6px;
                padding: 7px 16px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.9);
            }
        """)
        self.choose_file_btn.clicked.connect(self._browse_source)
        capsule_layout.addWidget(self.choose_file_btn)

        has_chunk_ortho = bool(self.chunk and getattr(self.chunk, "orthomosaic", None))
        initial_file_text = f"active_chunk ({getattr(self.chunk, 'label', 'Chunk 1')})" if has_chunk_ortho else "No file chosen"
        self.selected_file_label = QtWidgets.QLabel(initial_file_text)
        self.selected_file_label.setStyleSheet("color:#ffffff;font-size:12px;font-weight:500;")
        capsule_layout.addWidget(self.selected_file_label, 1)

        layout.addWidget(file_capsule)

        subtext = QtWidgets.QLabel("<span style='font-size:11px;color:rgba(255,255,255,0.7);'>Select an orthomosaic GeoTIFF (.tif) directly from your computer, or leave default for active chunk.</span>")
        layout.addWidget(subtext)

        # ── Teams Wrapper (Fleet & Swarm Workflows) ──────────────────────────
        layout.addWidget(teams_wrapper)

        # ── Action Buttons (Matching WebODM Green CTA) ──
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.setSpacing(16)

        self.run_button = QtWidgets.QPushButton("Run analysis")
        self.run_button.setObjectName("run_btn")
        self.run_button.setStyleSheet("""
            QPushButton {
                background-color: #16a34a;
                color: #ffffff;
                border: 1px solid #22c55e;
                border-radius: 10px;
                font-weight: 800;
                font-size: 13px;
                padding: 13px 28px;
            }
            QPushButton:hover {
                background-color: #15803d;
            }
            QPushButton:disabled {
                background-color: rgba(255, 255, 255, 0.2);
                color: rgba(255, 255, 255, 0.4);
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
        """)
        self.run_button.clicked.connect(self._accept_if_valid)

        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(0, 0, 0, 0.4);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 10px;
                font-weight: 600;
                font-size: 13px;
                padding: 13px 24px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.1);
            }
        """)
        cancel_button.clicked.connect(self.reject)

        btn_layout.addWidget(self.run_button)
        btn_layout.addWidget(cancel_button)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        self._refresh_preflight()

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(container)
        main_layout.addWidget(scroll)

    def _toggle_teams_accordion(self):
        is_open = self.teams_box.isVisible()
        self.teams_box.setVisible(not is_open)
        arrow = "▲" if not is_open else "▼"
        self.teams_toggle.setText(f"Running multiple spray drones or tractors?  {arrow}")

    def _on_rate_toggle(self, state):
        enabled = state == QtCore.Qt.Checked
        self._rate_fields_widget.setVisible(enabled)
        self._rate_desc.setText(
            "Custom operator-supplied physical rate encoded directly into controller files."
            if enabled else
            "Default: relative 0-100% intensity. Enable to supply physical application rates that are encoded directly into controller files."
        )

    def _on_rate_unit_changed(self, _index):
        unit = self._rate_unit.currentText().split("  ")[0].strip()
        self._min_label.setText(f"Min rate ({unit})")
        self._max_label.setText(f"Max rate ({unit})")

    def _on_spot_rate_toggle(self, state):
        enabled = state == QtCore.Qt.Checked
        self._spot_rate_fields_widget.setVisible(enabled)
        self._spot_rate_desc.setText(
            "Custom operator-supplied spot spray rate encoded directly into controller files."
            if enabled else
            "Default: binary section control (100% on weed/stress hotspots, 0% off-target). Enable to supply a custom target spray rate."
        )

    def _on_spot_rate_unit_changed(self, _index):
        unit = self._spot_rate_unit.currentText().split("  ")[0].strip()
        self._spot_target_label.setText(f"Target application rate ({unit})")

    def _browse_source(self):
        value, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select Multispectral Orthomosaic GeoTIFF", "", "GeoTIFF Files (*.tif *.tiff);;All Files (*)"
        )
        if value:
            self.source_file_path = value
            self.selected_file_label.setText(Path(value).name)
            self._refresh_preflight()

    def _state(self):
        selected = getattr(self, "source_file_path", None)
        source_path = Path(selected).expanduser() if selected else None
        has_chunk = bool(self.chunk and getattr(self.chunk, "orthomosaic", None))
        valid_source = bool(source_path and source_path.is_file()) or has_chunk

        checks = {
            "Valid multispectral source selected": valid_source,
        }
        return (source_path, checks)

    def _refresh_preflight(self, *args):
        _, checks = self._state()
        self.run_button.setEnabled(all(checks.values()))

    def _accept_if_valid(self):
        _, checks = self._state()
        if all(checks.values()):
            self.accept()

    def config_values(self):
        source_path, checks = self._state()
        if not all(checks.values()):
            raise ValueError("Analysis preflight did not pass")

        if source_path:
            out_dir = source_path.parent / f"{source_path.stem}_orthoswift_results"
        else:
            out_dir = Path.home() / "Documents" / "OrthoSWIFT_Results"
        out_dir.mkdir(parents=True, exist_ok=True)

        fertilizer_rate_plan = None
        if self._rate_toggle.isChecked():
            unit = self._rate_unit.currentText().split("  ")[0].strip()
            strategy = self._rate_strategy.currentText().split("  ")[0].strip()
            basis = self._rate_basis.currentText().split("  ")[0].strip()
            try:
                min_r = float(self._min_rate.text()) if self._min_rate.text().strip() else 0.0
                max_r = float(self._max_rate.text()) if self._max_rate.text().strip() else 0.0
            except ValueError:
                min_r, max_r = 0.0, 0.0
            fertilizer_rate_plan = {
                "mode": "physical",
                "operation": "fertilizer",
                "product_name": self._product_name.text().strip() or "Fertilizer",
                "rate_basis": basis,
                "unit": unit,
                "strategy": strategy,
                "min_rate": min_r,
                "max_rate": max_r,
                "approved_by": self._approved_by.text().strip() or "Operator",
            }

        spot_spray_rate_plan = None
        if self._spot_rate_toggle.isChecked():
            spot_unit = self._spot_rate_unit.currentText().split("  ")[0].strip()
            try:
                spot_target = float(self._spot_target_rate.text()) if self._spot_target_rate.text().strip() else 0.0
            except ValueError:
                spot_target = 0.0
            spot_spray_rate_plan = {
                "mode": "physical",
                "operation": "spray",
                "product_name": self._spot_product_name.text().strip() or "Herbicide",
                "rate_basis": "product",
                "unit": spot_unit,
                "strategy": "target_hotspots",
                "min_rate": spot_target,
                "max_rate": spot_target,
                "approved_by": self._spot_approved_by.text().strip() or "Operator",
            }

        return {
            "source_path": str(source_path) if source_path else None,
            "out_dir": str(out_dir),
            "zones": 3,
            "offline_basemap": True,
            "fertilizer_rate_plan": fertilizer_rate_plan,
            "spot_spray_rate_plan": spot_spray_rate_plan,
        }


def _start_runner(cfg_path: Path, out: Path, log: Path):
    process = QtCore.QProcess(QtWidgets.QApplication.instance())
    _processes.add(process)
    env = QtCore.QProcessEnvironment.systemEnvironment()
    existing_pythonpath = env.value("PYTHONPATH", "")
    env.insert("PYTHONPATH", os.pathsep.join(value for value in (str(BUNDLE), existing_pythonpath) if value))
    env.insert("PYTHONUNBUFFERED", "1")
    process.setProcessEnvironment(env)

    # ── Open the log file BEFORE defining closures that reference it ──────────
    log_file = open(log, "w", encoding="utf-8")

    progress = QtWidgets.QProgressDialog(
        "Initialising OrthoSWIFT…", "Cancel analysis", 0, 100,
        QtWidgets.QApplication.activeWindow(),
    )
    progress.setStyleSheet(OSW_QSS)
    progress.setWindowTitle("Processing")
    progress.setWindowFlags((progress.windowFlags() | QtCore.Qt.CustomizeWindowHint) & ~QtCore.Qt.WindowContextHelpButtonHint)
    progress.resize(520, 200)
    progress.setWindowModality(QtCore.Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setValue(0)
    progress.canceled.connect(process.kill)

    # ── Heartbeat: animated dots + event pump so dialog stays alive ───────────
    _state = {"label": "Initialising OrthoSWIFT", "dots": 0}

    heartbeat = QtCore.QTimer()
    heartbeat.setInterval(400)

    def _pulse():
        if not progress.isVisible():
            return
        _state["dots"] = (_state["dots"] + 1) % 4
        dots = "." * _state["dots"] if _state["dots"] else ""
        progress.setLabelText(f"{_state['label']}{dots}")
        # Drain any buffered output that might not have triggered a signal yet
        _read_stdout()
        _read_stderr()
        QtWidgets.QApplication.processEvents()

    heartbeat.timeout.connect(_pulse)

    def _read_stdout():
        data = process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        if not data:
            return
        try:
            log_file.write(data)
            log_file.flush()
        except Exception:
            pass
        print(data, end="", flush=True)
        for line in data.splitlines():
            if "[PROGRESS " in line and "%]" in line:
                try:
                    start = line.index("[PROGRESS ") + 10
                    end = line.index("%]", start)
                    pct = int(line[start:end])
                    msg = line[end + 2:].strip() or _state["label"]
                    _state["label"] = msg
                    _state["dots"] = 0
                    progress.setValue(pct)
                    progress.setLabelText(f"{msg} · {pct}%")
                    QtWidgets.QApplication.processEvents()
                except Exception:
                    pass

    def _read_stderr():
        data = process.readAllStandardError().data().decode("utf-8", errors="replace")
        if not data:
            return
        try:
            log_file.write(data)
            log_file.flush()
        except Exception:
            pass
        print(data, end="", flush=True)
        QtWidgets.QApplication.processEvents()

    process.readyReadStandardOutput.connect(_read_stdout)
    process.readyReadStandardError.connect(_read_stderr)

    def finished(exit_code, exit_status):
        heartbeat.stop()
        _read_stdout()
        _read_stderr()
        try:
            log_file.close()
        except Exception:
            pass
        progress.close()
        _processes.discard(process)
        archive = out.parent / "orthoswift-deliverables.zip"
        if not archive.is_file():
            fallback_zip = out.parent / f"{out.name}.zip"
            if fallback_zip.is_file():
                archive = fallback_zip

        if exit_status == QtCore.QProcess.NormalExit and exit_code == 0 and archive.is_file():
            _success_card(archive, out)
        else:
            _msg(f"OrthoSWIFT failed or was cancelled.\n\nDiagnostic log:\n{log}", title="Analysis Failed", is_error=True)
        process.deleteLater()
        progress.deleteLater()

    process.finished.connect(finished)
    progress.show()
    process.start(_python(), [str(RUNNER), str(cfg_path)])
    if not process.waitForStarted(5000):
        heartbeat.stop()
        try:
            log_file.close()
        except Exception:
            pass
        progress.close()
        _processes.discard(process)
        raise RuntimeError(f"Could not start OrthoSWIFT Python. See {log}")

    heartbeat.start()


def run_analysis_dialog():
    try:
        _check_runtime()
        doc = getattr(Metashape.app, "document", None)
        chunk = getattr(doc, "chunk", None) if doc else None
        dialog = OrthoSwiftRunDialog(chunk, QtWidgets.QApplication.activeWindow())
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        config = dialog.config_values()
        out = Path(config["out_dir"]).resolve()

        if config.get("source_path"):
            exported = Path(config["source_path"]).resolve()
        else:
            if not chunk or not getattr(chunk, "orthomosaic", None):
                raise RuntimeError("No active orthomosaic found in chunk and no external GeoTIFF selected.")
            exported = out.parent / f"{out.name}_metashape_orthomosaic.tif"
            compression = Metashape.ImageCompression()
            compression.tiff_tiled = True
            compression.tiff_overviews = True
            chunk.exportRaster(
                str(exported), format=Metashape.RasterFormatTiles,
                image_format=Metashape.ImageFormatTIFF,
                source_data=Metashape.OrthomosaicData, save_alpha=True,
                clip_to_boundary=True, image_compression=compression,
            )

        config.update({
            "orthomosaic_path": str(exported),
            "host": {"name": "Metashape", "version": Metashape.app.version},
        })
        cfg_path = out.parent / f"{out.name}_config.json"
        cfg_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        log = out.parent / f"{out.name}_run.log"
        _start_runner(cfg_path, out, log)
    except Exception as exc:
        _msg(f"OrthoSWIFT error: {exc}\n\n{traceback.format_exc()}")


def register():
    try:
        Metashape.app.addMenuItem("OrthoSWIFT/Run multispectral field analysis…", run_analysis_dialog)
        print(f"OrthoSWIFT {PLUGIN_VERSION} registered successfully")
    except Exception as exc:
        print(f"OrthoSWIFT registration failed: {exc}")
        traceback.print_exc()


register()
