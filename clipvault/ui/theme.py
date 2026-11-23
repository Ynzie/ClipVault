"""Color themes + global stylesheet."""
from __future__ import annotations

THEMES = {
    # key: (label, accent, accent2, bg, panel)
    "ynzie":   ("Cotton Candy", "#ff7ac8", "#7fd6ff", "#0b0b10", "#14141c"),
    "kara":    ("Royal Purple", "#a970ff", "#e3b3ff", "#0c0911", "#16111f"),
    "kaleb":   ("Crimson",      "#ff3b4e", "#ff9aa5", "#0b0909", "#171112"),
    "james":   ("Toxic Green",  "#3dff8b", "#b6ffd3", "#080c0a", "#101813"),
    "dnarte":  ("Deep Blue",    "#3d7bff", "#9ec0ff", "#07090f", "#0f1422"),
    "medal":   ("Lime",         "#c6f432", "#eaffb0", "#0a0b08", "#141611"),
}


def palette(key: str) -> dict:
    label, a1, a2, bg, panel = THEMES.get(key, THEMES["ynzie"])
    return {
        "accent": a1, "accent2": a2, "bg": bg, "panel": panel,
        "panel2": _mix(panel, "#ffffff", 0.04), "panel3": _mix(panel, "#ffffff", 0.08),
        "border": _mix(panel, "#ffffff", 0.09), "text": "#ecebf3", "muted": "#8d8b9c", "faint": "#5d5b6b",
        "danger": "#ff4d5e", "ok": "#3ddc84", "warn": "#ffb547", "rec": "#ff3b4e",
        "accent_soft": _alpha(a1, 0.16), "accent_glow": _alpha(a1, 0.35),
        # text on accent-colored buttons: white, dark only on very bright accents (Lime) — same rule as RGB Studio
        "on_accent": "#0b0b10" if (0.299 * _hex(a1)[0] + 0.587 * _hex(a1)[1] + 0.114 * _hex(a1)[2]) / 255 > 0.72
        else "#ffffff",
    }


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    ra, ga, ba = _hex(a)
    rb, gb, bb = _hex(b)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


def _alpha(c, a):
    r, g, b = _hex(c)
    return f"rgba({r},{g},{b},{a})"


# ---- "Glass" controls, the same recipe RGB Studio uses: a sheen on the top half, a brighter top edge,
# translucent accent fills. Qt stylesheets have one background layer, so the sheen is pre-mixed into it.
EDGE, TOP = "rgba(255,255,255,0.13)", "rgba(255,255,255,0.28)"


def _sheen(base: str, k: float = 1.0) -> str:
    """Glass: the top half catches light (bright at the top, dim by the middle), a faint lift at the bottom."""
    w = "#ffffff"
    return (f"qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {_mix(base, w, .16 * k)}, stop:0.46 {_mix(base, w, .06 * k)}, "
            f"stop:0.54 {_mix(base, w, .01 * k)}, stop:1 {_mix(base, w, .035 * k)})")


def _accent_glass(base: str, top: float = .30) -> str:
    """Accent buttons: a strong white sheen fading out just past the middle, then the solid translucent accent."""
    w = "#ffffff"
    return (f"qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {_mix(base, w, top)}, stop:0.46 {_mix(base, w, top * .27)}, "
            f"stop:0.54 {base}, stop:1 {base})")


def stylesheet(p: dict) -> str:
    sec = _mix(_mix(p['panel'], '#ffffff', .04), p['accent'], .12)      # secondary glass: a hint of accent
    pri = _mix(p['bg'], p['accent'], .74)                                  # primary: accent at 74% over the page
    act = _mix(p['panel'], p['accent'], .16)                               # selected nav item / checked toggle buttons
    chip_on = _mix(p['bg'], p['accent'], .78)
    knob = ("qradialgradient(cx:0.35, cy:0.3, radius:0.75, fx:0.35, fy:0.3, stop:0 #ffffff, stop:0.45 #f1f1f6, "
            "stop:1 #c9c9d4)")
    return f"""
* {{ font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif; font-size: 10pt; color: {p['text']}; }}
QMainWindow, QWidget#root, QDialog, QMessageBox, QInputDialog, QFileDialog {{ background: {p['bg']}; }}
QWidget#sidebar {{ background: {p['panel']}; border-right: 1px solid {p['border']}; }}
QWidget#topbar {{ background: {p['panel']}; border-bottom: 1px solid {p['border']}; }}
QLabel#h1 {{ font-size: 18pt; font-weight: 700; }}
QLabel#h2 {{ font-size: 13pt; font-weight: 650; }}
QLabel#h3 {{ font-size: 10.5pt; font-weight: 650; }}
QLabel#muted {{ color: {p['muted']}; }}
QLabel#faint {{ color: {p['faint']}; font-size: 8.5pt; }}
QLabel#section {{ color: {p['faint']}; font-size: 8pt; font-weight: 700; letter-spacing: 1.5px; padding: 10px 12px 4px 12px; }}
QLabel#brand {{ font-size: 15pt; font-weight: 800; }}
QLabel#pill {{ background: {p['panel3']}; border-radius: 9px; padding: 2px 8px; font-size: 8.5pt; color: {p['muted']}; }}
QLabel#kbd {{ background: {p['panel3']}; border: 1px solid {p['border']}; border-bottom-width: 2px; border-radius: 5px;
             padding: 1px 6px; font-size: 8.5pt; font-weight: 700; }}

QPushButton {{ background: {_sheen(sec)}; border: 1px solid {EDGE}; border-top-color: {TOP}; border-radius: 10px;
    padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ background: {_sheen(_mix(sec, '#ffffff', .07), 1.3)}; border-color: rgba(255,255,255,0.18);
    border-top-color: rgba(255,255,255,0.34); }}
QPushButton:pressed {{ background: {_sheen(_mix(sec, '#000000', .1), .6)}; padding-top: 8px; padding-bottom: 6px; }}
QPushButton:disabled {{ color: {p['faint']}; background: {_sheen(_mix(p['panel'], '#ffffff', .03), .4)};
    border-color: rgba(255,255,255,0.06); }}
QPushButton:checked {{ background: {_sheen(act)}; border: 1px solid {_alpha(p['accent'], .55)};
    border-top-color: {_mix(p['accent'], '#ffffff', .35)}; color: {p['text']}; }}
QPushButton#primary {{ background: {_accent_glass(pri)}; color: {p['on_accent']}; border: 1px solid rgba(255,255,255,0.24);
    border-top-color: rgba(255,255,255,0.42); }}
QPushButton#primary:hover {{ background: {_accent_glass(_mix(pri, '#ffffff', .08), .36)}; }}
QPushButton#primary:pressed {{ background: {_accent_glass(_mix(pri, '#000000', .08), .22)}; padding-top: 8px; padding-bottom: 6px; }}
QPushButton#primary:disabled {{ background: {_accent_glass(_mix(p['bg'], p['accent'], .3), .12)};
    color: {_alpha(p['on_accent'], .55)}; }}
QPushButton#danger {{ background: {_sheen(_mix(p['panel'], p['danger'], .10), .7)}; color: {p['danger']};
    border: 1px solid {_alpha(p['danger'], .4)}; border-top-color: {_alpha(p['danger'], .6)}; }}
QPushButton#danger:hover {{ background: {_sheen(_mix(p['panel'], p['danger'], .18), .8)}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {p['muted']}; padding: 6px 8px; }}
QPushButton#ghost:hover {{ color: {p['text']}; background: {p['panel3']}; }}
QPushButton#rec {{ background: {_alpha(p['rec'], .15)}; color: {p['rec']}; border: 1px solid {_alpha(p['rec'], .5)}; }}
QPushButton#rec:hover {{ background: {_alpha(p['rec'], .25)}; }}
QPushButton#chip {{ background: {_sheen(_mix(p['panel'], '#ffffff', .03), .6)}; border: 1px solid {EDGE};
    border-top-color: {TOP}; border-radius: 15px; padding: 4px 12px; color: {p['muted']}; font-weight: 600; }}
QPushButton#chip:hover {{ color: {p['text']}; }}
QPushButton#chip:checked {{ background: {_accent_glass(chip_on, .28)}; color: {p['on_accent']};
    border: 1px solid rgba(255,255,255,0.22); border-top-color: rgba(255,255,255,0.4); }}

QPushButton#nav {{ background: transparent; border: 1px solid transparent; border-radius: 9px; text-align: left; padding: 7px 10px;
    color: {p['muted']}; font-weight: 600; }}
QPushButton#nav:hover {{ background: {p['panel3']}; color: {p['text']}; }}
QPushButton#nav:checked {{ background: {_sheen(act)}; color: {p['text']}; border: 1px solid {EDGE}; border-top-color: {TOP}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ background: {p['panel2']}; border: 1px solid {EDGE}; border-top-color: {TOP};
    border-radius: 9px; padding: 6px 9px; selection-background-color: {p['accent']}; selection-color: #000; }}
QComboBox {{ background: {_sheen(_mix(p['panel'], '#ffffff', .05), .7)}; border: 1px solid {EDGE}; border-top-color: {TOP};
    border-radius: 9px; padding: 6px 9px; selection-background-color: {p['accent']}; selection-color: #000; }}
QComboBox:hover {{ border-color: rgba(255,255,255,0.18); border-top-color: rgba(255,255,255,0.34); }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {p['accent']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {p['panel2']}; border: 1px solid {p['border']}; selection-background-color: {p['accent_soft']}; outline: none; padding: 4px; }}
QPlainTextEdit {{ font-family: Consolas, "Cascadia Mono", monospace; font-size: 8.5pt; }}

QSlider::groove:horizontal {{ height: 4px; background: {p['panel3']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {knob}; border: 1px solid rgba(0,0,0,0.25); width: 14px; height: 14px; margin: -6px 0;
    border-radius: 8px; }}
QSlider::handle:horizontal:hover {{ background: {p['accent2']}; }}

QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 3px 2px; }}
QScrollBar::handle:vertical {{ background: {_mix(p['panel3'], p['accent'], .22)}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{ background: {_mix(p['panel3'], p['accent'], .7)}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px 3px; }}
QScrollBar::handle:horizontal {{ background: {_mix(p['panel3'], p['accent'], .22)}; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{ background: {_mix(p['panel3'], p['accent'], .7)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QFrame#card {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {_mix(p['panel'], '#ffffff', .025)}, stop:0.3 {p['panel']},
    stop:1 {p['panel']}); border: 1px solid {p['border']}; border-top-color: {_mix(p['border'], '#ffffff', .1)}; border-radius: 12px; }}
QFrame#row {{ border-top: 1px solid {p['border']}; }}
QFrame#hint {{ background: {p['accent_soft']}; border: 1px solid {_alpha(p['accent'], .35)}; border-radius: 10px; }}
QFrame#warn {{ background: {_alpha(p['warn'], .1)}; border: 1px solid {_alpha(p['warn'], .35)}; border-radius: 10px; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {EDGE}; border-top-color: {TOP};
    background: {_sheen(_mix(p['panel'], '#ffffff', .06), .7)}; }}
QCheckBox::indicator:checked {{ background: {_accent_glass(p['accent'], .25)}; border: 1px solid rgba(255,255,255,0.25); }}
QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 8px; border: 1px solid {p['faint']}; background: {p['panel2']}; }}
QRadioButton::indicator:checked {{ background: {p['accent']}; border: 3px solid {p['panel2']}; }}

QMenu {{ background: {p['panel2']}; border: 1px solid {p['border']}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 7px 22px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p['accent_soft']}; }}
QMenu::separator {{ height: 1px; background: {p['border']}; margin: 4px 6px; }}
QToolTip {{ background: {p['panel3']}; color: {p['text']}; border: 1px solid {p['border']}; padding: 5px 8px; border-radius: 6px; }}
QProgressBar {{ background: {p['panel3']}; border: none; border-radius: 3px; height: 6px; text-align: center; font-size: 1px; }}
QProgressBar::chunk {{ background: {p['accent']}; border-radius: 3px; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ padding: 6px 8px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}
QListWidget::item:hover {{ background: {p['panel3']}; }}
QTabBar::tab {{ background: transparent; padding: 6px 12px; color: {p['muted']}; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:selected {{ color: {p['text']}; border-bottom-color: {p['accent']}; }}
"""
