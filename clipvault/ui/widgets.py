"""Reusable widgets: toggle, mic-sensitivity meter, setting rows, hotkey capture, glyph icons."""
from __future__ import annotations

import math
import sys

from PySide6.QtCore import QObject, Qt, Signal, QRectF, QPointF, QPropertyAnimation, Property, QEasingCurve, QSize, QTimer
from PySide6.QtGui import (QColor, QPainter, QFont, QIcon, QPixmap, QPen, QLinearGradient, QKeySequence,
                           QPainterPath, QFontDatabase)
from PySide6.QtWidgets import (QWidget, QAbstractButton, QHBoxLayout, QVBoxLayout, QLabel, QFrame, QLineEdit,
                               QPushButton, QSizePolicy, QButtonGroup, QSlider)

P: dict = {}  # current palette, set by MainWindow


# ----------------------------------------------------------------------------- glyph icons (Segoe Fluent)
G = {
    "library": "", "video": "", "camera": "", "settings": "", "star": "",
    "star_on": "", "trash": "", "folder": "", "play": "", "pause": "",
    "rec": "", "bookmark": "", "mic": "", "volume": "", "mute": "",
    "cut": "", "save": "", "game": "", "search": "", "refresh": "",
    "back": "", "desktop": "", "image": "", "clock": "", "flag": "",
    "copy": "", "rename": "", "more": "", "add": "", "restore": "",
    "zoom_in": "", "zoom_out": "", "info": "", "sun": "", "storage": "",
    "bell": "", "color": "", "keyboard": "", "full": "", "stop": "",
    "check": "", "export": "", "film": "", "hdr": "", "eq": "",
    "prev": "", "next": "", "close": "",
}
_ICON_FONT = None


def icon_font_family() -> str:
    global _ICON_FONT
    if _ICON_FONT is None:
        fams = set(QFontDatabase.families())
        _ICON_FONT = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), "")
    return _ICON_FONT


def icon(name: str, color: str | None = None, size: int = 18) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    fam = icon_font_family()
    if fam:
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(fam)
        f.setPixelSize(int(size * 1.5))
        p.setFont(f)
        p.setPen(QColor(color or P.get("text", "#eee")))
        p.drawText(pm.rect(), Qt.AlignCenter, G.get(name, name))
        p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def glyph_label(name: str, size: int = 16, color: str | None = None) -> QLabel:
    lab = QLabel(G.get(name, "") if icon_font_family() else "")
    f = QFont(icon_font_family() or "Segoe UI")
    f.setPixelSize(size)
    lab.setFont(f)
    # the app stylesheet sets a font on every widget (*), which beat setFont() and turned page-header icons
    # into a tiny empty box — name the icon font in the label's own stylesheet so it wins
    lab.setStyleSheet(f"color: {color or P.get('muted', '#999')}; background: transparent; "
                      f"font-family: '{icon_font_family() or 'Segoe UI'}'; font-size: {size}px;")
    return lab


# ----------------------------------------------------------------------------- toggle
class Toggle(QAbstractButton):
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._knob = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _get_knob(self):
        return self._knob

    def _set_knob(self, v):
        self._knob = v
        self.update()

    # NOTE: must not be called "pos" — that's QWidget's own position property, and animating it
    # moved the whole switch to the corner of its row.
    knob = Property(float, _get_knob, _set_knob)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = QColor(P.get("accent", "#ff7ac8"))
        off = QColor(P.get("panel3", "#333"))
        c = QColor(int(off.red() + (on.red() - off.red()) * self._knob), int(off.green() + (on.green() - off.green()) * self._knob),
                   int(off.blue() + (on.blue() - off.blue()) * self._knob))
        if not self.isEnabled():
            c.setAlpha(90)
        # glass track (RGB Studio style): sheen on the top half, brighter top edge
        track = QRectF(0.5, 2.5, 41, 19)
        k = self._knob
        base = QColor(c) if k > 0.02 else QColor(P.get("panel2", "#222"))
        if not self.isEnabled():
            base.setAlpha(90)
        g = QLinearGradient(0, track.top(), 0, track.bottom())
        w = 0.25 if k > 0.5 else 0.12
        g.setColorAt(0.0, _lift(base, w))
        g.setColorAt(0.55, base)
        g.setColorAt(1.0, base)
        p.setPen(QPen(QColor(255, 255, 255, 64 if k > 0.5 else 34), 1))
        p.setBrush(g)
        p.drawRoundedRect(track, 9.5, 9.5)
        p.setPen(QPen(QColor(255, 255, 255, 70 if k > 0.5 else 46), 1))  # top highlight
        p.drawLine(QPointF(track.left() + 9, track.top() + 0.5), QPointF(track.right() - 9, track.top() + 0.5))
        # pearl knob with a small shadow
        x = 3 + k * 20
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawEllipse(QRectF(x, 6, 14, 14))
        from PySide6.QtGui import QRadialGradient
        rg = QRadialGradient(x + 14 * 0.35, 5 + 14 * 0.3, 11)
        rg.setColorAt(0.0, QColor("#ffffff"))
        rg.setColorAt(0.45, QColor("#f1f1f6"))
        rg.setColorAt(1.0, QColor("#c9c9d4"))
        p.setBrush(rg)
        p.drawEllipse(QRectF(x, 5, 14, 14))

    def sizeHint(self):
        return QSize(42, 24)


# ----------------------------------------------------------------------------- layout helpers
class Card(QFrame):
    def __init__(self, title: str = "", subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(18, 14, 18, 14)
        self.lay.setSpacing(0)
        if title:
            t = QLabel(title)
            t.setObjectName("h3")
            self.lay.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("muted")
            s.setWordWrap(True)
            self.lay.addWidget(s)
        if title or subtitle:
            self.lay.addSpacing(8)
        self._first = True

    def add_row(self, title: str, desc: str, control: QWidget | None, stretch_control=False) -> QWidget:
        row = QFrame()
        if not self._first:
            row.setObjectName("row")
        self._first = False
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 10, 0, 10)
        h.setSpacing(16)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        a = QLabel(title)
        a.setStyleSheet("font-weight: 600;")
        txt.addWidget(a)
        if desc:
            b = QLabel(desc)
            b.setObjectName("muted")
            b.setWordWrap(True)
            b.setStyleSheet("font-size: 9pt;")
            txt.addWidget(b)
        h.addLayout(txt, 1)
        if control is not None:
            h.addWidget(control, 1 if stretch_control else 0, Qt.AlignRight | Qt.AlignVCenter)
        self.lay.addWidget(row)
        return row

    def add(self, w: QWidget):
        self.lay.addWidget(w)
        self._first = False
        return w


class Segmented(QWidget):
    """Row of exclusive chip buttons."""
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], value: str, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for key, label in options:
            b = QPushButton(label)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(key == value)
            b.clicked.connect(lambda _=False, k=key: self.changed.emit(k))
            self.group.addButton(b)
            self.buttons[key] = b
            h.addWidget(b)

    def set_value(self, key):
        if key in self.buttons:
            self.buttons[key].setChecked(True)


class SliderValue(QWidget):
    """Slider + live value label."""
    valueChanged = Signal(float)

    def __init__(self, lo: float, hi: float, value: float, step: float = 1.0, fmt="{:.0f}", width=220, parent=None):
        super().__init__(parent)
        self.lo, self.step, self.fmt = lo, step, fmt
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.s = QSlider(Qt.Horizontal)
        self.s.setRange(0, int(round((hi - lo) / step)))
        self.s.setFixedWidth(width)
        self.lab = QLabel()
        self.lab.setMinimumWidth(56)
        self.lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lab.setObjectName("muted")
        h.addWidget(self.s)
        h.addWidget(self.lab)
        self.set_value(value)
        self.s.valueChanged.connect(self._changed)

    def value(self) -> float:
        return self.lo + self.s.value() * self.step

    def set_value(self, v: float):
        self.s.blockSignals(True)
        self.s.setValue(int(round((v - self.lo) / self.step)))
        self.s.blockSignals(False)
        self.lab.setText(self.fmt.format(self.value()))

    def _changed(self, _):
        self.lab.setText(self.fmt.format(self.value()))
        self.valueChanged.emit(self.value())


QT_KEY_NAMES = {
    Qt.Key_Insert: "Insert", Qt.Key_Delete: "Delete", Qt.Key_Home: "Home", Qt.Key_End: "End",
    Qt.Key_PageUp: "PageUp", Qt.Key_PageDown: "PageDown", Qt.Key_Pause: "Pause", Qt.Key_ScrollLock: "ScrollLock",
    Qt.Key_Print: "PrintScreen", Qt.Key_Space: "Space", Qt.Key_Tab: "Tab", Qt.Key_CapsLock: "CapsLock",
    Qt.Key_QuoteLeft: "`", Qt.Key_Minus: "-", Qt.Key_Equal: "=", Qt.Key_BracketLeft: "[", Qt.Key_BracketRight: "]",
    Qt.Key_Semicolon: ";", Qt.Key_Apostrophe: "'", Qt.Key_Comma: ",", Qt.Key_Period: ".", Qt.Key_Slash: "/",
    Qt.Key_Backslash: "\\",
}


class HotkeyEdit(QPushButton):
    """Click, press a combo; Esc cancels, Backspace clears."""
    changed = Signal(str)
    suspend_hook = None  # set by SettingsView: pauses global hotkeys while listening

    def __init__(self, combo: str, parent=None):
        super().__init__(combo or "Not set", parent)
        self.combo = combo
        self.setCheckable(True)
        self.setMinimumWidth(140)
        self.setCursor(Qt.PointingHandCursor)
        self.toggled.connect(self._arm)

    def _arm(self, on):
        self.setText("Press keys…" if on else (self.combo or "Not set"))
        if HotkeyEdit.suspend_hook:
            HotkeyEdit.suspend_hook(on)
        if on:
            self.grabKeyboard()
        else:
            self.releaseKeyboard()

    def focusOutEvent(self, e):
        if self.isChecked():
            self.setChecked(False)
        super().focusOutEvent(e)

    def hideEvent(self, e):
        if self.isChecked():
            self.setChecked(False)
        super().hideEvent(e)

    def keyPressEvent(self, e):
        if not self.isChecked():
            return super().keyPressEvent(e)
        k = e.key()
        if k in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return
        if k == Qt.Key_Escape:
            self.setChecked(False)
            return
        if k in (Qt.Key_Backspace, Qt.Key_Delete):
            self.combo = ""
        else:
            mods = []
            m = e.modifiers()
            if m & Qt.ControlModifier:
                mods.append("Ctrl")
            if m & Qt.AltModifier:
                mods.append("Alt")
            if m & Qt.ShiftModifier:
                mods.append("Shift")
            name = QT_KEY_NAMES.get(k) or QKeySequence(k).toString() or ""
            if Qt.Key_F1 <= k <= Qt.Key_F24:
                name = f"F{k - Qt.Key_F1 + 1}"
            if e.modifiers() & Qt.KeypadModifier and name.isdigit():
                name = f"Num{name}"
            from ..core.audio import VK
            if name not in VK and name.upper() not in VK:
                self.setChecked(False)
                self.setText(f"{name or 'That key'} isn't supported")
                QTimer.singleShot(1500, lambda: self.setText(self.combo or "Not set"))
                return
            self.combo = "+".join(mods + [name])
        self.setChecked(False)
        self.changed.emit(self.combo)

    def mousePressEvent(self, e):
        if self.isChecked() and e.button() in (Qt.XButton1, Qt.XButton2):
            self.combo = "Mouse4" if e.button() == Qt.XButton1 else "Mouse5"
            self.setChecked(False)
            self.changed.emit(self.combo)
            return
        super().mousePressEvent(e)


def kbd(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("kbd")
    return lab


def hline() -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {P.get('border', '#333')};")
    return f


# ----------------------------------------------------------------------------- meters
DB_MIN, DB_MAX = -70.0, 0.0


def db_to_x(dbv: float, w: float) -> float:
    return (min(DB_MAX, max(DB_MIN, dbv)) - DB_MIN) / (DB_MAX - DB_MIN) * w


class LevelMeter(QWidget):
    """Plain horizontal level meter (dBFS)."""

    def __init__(self, parent=None, height=10):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setMinimumWidth(160)
        self.level = DB_MIN
        self.peak = DB_MIN
        self._peak_t = 0

    def set_level(self, dbv: float):
        self.level = max(self.level - 2.5, dbv)  # fall-off smoothing
        if dbv >= self.peak:
            self.peak, self._peak_t = dbv, 30
        elif self._peak_t > 0:
            self._peak_t -= 1
        else:
            self.peak = max(DB_MIN, self.peak - 1.0)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(P.get("panel3", "#333")))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        w = db_to_x(self.level, r.width())
        g = QLinearGradient(0, 0, r.width(), 0)
        g.setColorAt(0, QColor(P.get("ok", "#3ddc84")))
        g.setColorAt(0.78, QColor(P.get("ok", "#3ddc84")))
        g.setColorAt(0.9, QColor(P.get("warn", "#ffb547")))
        g.setColorAt(1, QColor(P.get("danger", "#ff4d5e")))
        p.setBrush(g)
        p.drawRoundedRect(QRectF(0, 0, w, r.height()), r.height() / 2, r.height() / 2)
        px = db_to_x(self.peak, r.width())
        p.setBrush(QColor("#ffffff"))
        p.drawRect(QRectF(max(0, px - 1.5), 0, 2, r.height()))


class SensitivityMeter(QWidget):
    """Discord-style input sensitivity bar: live mic level with a draggable threshold.

    Left of the handle (quieter) = muted; right of it = transmitted. The bar turns green
    while the gate is open, so you can see exactly when your wife's voice would get through.
    """
    thresholdChanged = Signal(float)
    thresholdCommitted = Signal(float)

    def __init__(self, threshold: float = -45.0, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(54)
        self.setMinimumWidth(320)
        self.setCursor(Qt.SizeHorCursor)
        self.threshold = threshold
        self.level = DB_MIN
        self.gate_open = False
        self.auto = False
        self.markers: dict[str, float] = {}  # calibration results: label -> dB
        self._drag = False
        self._hist = [DB_MIN] * 90

    def set_level(self, dbv: float, gate_open: bool):
        self.level = max(self.level - 3.0, dbv)
        self.gate_open = gate_open
        self._hist = self._hist[1:] + [dbv]
        self.update()

    def set_threshold(self, v: float):
        self.threshold = max(DB_MIN, min(DB_MAX, v))
        self.update()

    def _bar(self) -> QRectF:
        return QRectF(8, 18, self.width() - 16, 14)

    def _x_to_db(self, x: float) -> float:
        b = self._bar()
        return DB_MIN + (x - b.left()) / b.width() * (DB_MAX - DB_MIN)

    def mousePressEvent(self, e):
        if self.auto:
            return
        self._drag = True
        self.set_threshold(round(self._x_to_db(e.position().x()), 1))
        self.thresholdChanged.emit(self.threshold)

    def mouseMoveEvent(self, e):
        if self._drag:
            self.set_threshold(round(self._x_to_db(e.position().x()), 1))
            self.thresholdChanged.emit(self.threshold)

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.thresholdCommitted.emit(self.threshold)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        b = self._bar()
        tx = b.left() + db_to_x(self.threshold, b.width())
        # track: muted zone (left) / pass zone (right)
        p.setPen(Qt.NoPen)
        muted = QColor(P.get("panel3", "#333"))
        p.setBrush(muted)
        p.drawRoundedRect(b, 7, 7)
        pass_zone = QColor(P.get("accent", "#ff7ac8"))
        pass_zone.setAlpha(40)
        p.setBrush(pass_zone)
        p.drawRoundedRect(QRectF(tx, b.top(), b.right() - tx, b.height()), 7, 7)
        # live level
        lx = db_to_x(self.level, b.width())
        p.setPen(Qt.NoPen)
        col = QColor(P.get("ok", "#3ddc84")) if self.gate_open else QColor(P.get("faint", "#666"))
        p.setBrush(col)
        p.drawRoundedRect(QRectF(b.left(), b.top() + 3, lx, b.height() - 6), 4, 4)
        # calibration markers
        f = self.font()
        f.setPointSizeF(7.5)
        p.setFont(f)
        for label, v in self.markers.items():
            mx = b.left() + db_to_x(v, b.width())
            c = QColor(P.get("accent2", "#7fd6ff"))
            p.setPen(QPen(c, 1, Qt.DashLine))
            p.drawLine(QPointF(mx, b.top() - 2), QPointF(mx, b.bottom() + 2))
            p.setPen(c)
            p.drawText(QRectF(mx - 50, b.bottom() + 4, 100, 14), Qt.AlignCenter, label)
        # threshold handle
        hc = QColor(P.get("accent", "#ff7ac8")) if not self.auto else QColor(P.get("faint", "#666"))
        p.setPen(Qt.NoPen)
        p.setBrush(hc)
        p.drawRoundedRect(QRectF(tx - 2, b.top() - 6, 4, b.height() + 12), 2, 2)
        p.drawEllipse(QPointF(tx, b.top() - 7), 6, 6)
        p.setPen(QColor(P.get("text", "#eee")))
        p.drawText(QRectF(tx - 60, 0, 120, 12), Qt.AlignCenter, f"{self.threshold:.0f} dB" + (" (auto)" if self.auto else ""))
        # scale
        p.setPen(QColor(P.get("faint", "#666")))
        for d in (-60, -40, -20, 0):
            x = b.left() + db_to_x(d, b.width())
            p.drawText(QRectF(x - 20, b.bottom() + (18 if self.markers else 4), 40, 12), Qt.AlignCenter, f"{d}")


class Toast(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 16, 10)
        self.ic = glyph_label("check", 16, P.get("accent"))
        lay.addWidget(self.ic)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.t = QLabel()
        self.t.setStyleSheet("font-weight: 700;")
        self.s = QLabel()
        self.s.setObjectName("muted")
        col.addWidget(self.t)
        col.addWidget(self.s)
        lay.addLayout(col)
        self.hide()
        self._timer = QTimer(self, singleShot=True, timeout=self.hide)

    def show_msg(self, title: str, sub: str = "", error=False):
        self.t.setText(title)
        self.s.setText(sub)
        self.s.setVisible(bool(sub))
        self.ic.setText(G["info" if error else "check"] if icon_font_family() else "")
        self.ic.setStyleSheet(f"color: {P.get('danger' if error else 'accent')}; background: transparent;")
        self.adjustSize()
        par = self.parentWidget()
        self.move(par.width() - self.width() - 24, par.height() - self.height() - 24)
        self.raise_()
        self.show()
        self._timer.start(4500 if error else 2600)


def audio_output(parent):
    """QAudioOutput, or a silent stand-in on machines without an audio stack (headless CI)."""
    import os
    if os.environ.get("CLIPVAULT_NO_AUDIO_OUT"):
        class _Null:
            def setVolume(self, v): pass
        return _Null()
    from PySide6.QtMultimedia import QAudioOutput
    return QAudioOutput(parent)


class _Invoker(QObject):
    call = Signal(object)


_invoker = None


def init_invoker():
    """Create on the GUI thread (MainWindow does this) so emits from worker threads are queued to it."""
    global _invoker
    if _invoker is None:
        _invoker = _Invoker()
        _invoker.call.connect(lambda fn: fn())


def ui_call(fn):
    """Run fn on the GUI thread (safe to call from any worker thread)."""
    init_invoker()
    _invoker.call.emit(fn)



def _lift(c: QColor, t: float) -> QColor:
    return QColor(int(c.red() + (255 - c.red()) * t), int(c.green() + (255 - c.green()) * t),
                  int(c.blue() + (255 - c.blue()) * t), c.alpha())


def add_glow(btn, color: str | None = None, blur: int = 22, dy: int = 5):
    """Soft colored glow under an accent button (stylesheets can't draw shadows)."""
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    eff = QGraphicsDropShadowEffect(btn)
    c = QColor(color or P.get("accent", "#ff7ac8"))
    c.setAlpha(100)
    eff.setColor(c)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    btn.setGraphicsEffect(eff)


def glow_primary_buttons(root):
    from PySide6.QtWidgets import QPushButton as _B
    for b in root.findChildren(_B):
        if b.objectName() == "primary" and b.graphicsEffect() is None:
            add_glow(b)


# ---------------------------------------------------------------------------- window chrome
class GlowPane(QWidget):
    """Page background: plain theme color with a soft, static accent glow low in the window.
    The glow sits near the bottom so the top edge stays the plain color the title bar is painted in
    (no seam under the minimize / maximize / close buttons). Cached, so scrolling doesn't redraw it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache = None
        self._key = None

    def paintEvent(self, e):
        from PySide6.QtGui import QRadialGradient
        key = (self.width(), self.height(), P.get("bg"), P.get("accent"), P.get("accent2"))
        if self._key != key:
            pm = QPixmap(max(1, self.width()), max(1, self.height()))
            pm.fill(QColor(P["bg"]))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            for cx, cy, rad, col, a in ((0.42, 1.12, 0.62, P["accent"], 34), (0.88, 1.05, 0.42, P["accent2"], 20)):
                g = QRadialGradient(w * cx, h * cy, max(w, h) * rad)
                c0 = QColor(col)
                c0.setAlpha(a)
                c1 = QColor(col)
                c1.setAlpha(0)
                g.setColorAt(0.0, c0)
                g.setColorAt(1.0, c1)
                p.fillRect(pm.rect(), g)
            p.end()
            self._cache, self._key = pm, key
        p = QPainter(self)
        p.drawPixmap(e.rect(), self._cache, e.rect())
        p.end()


def style_title_bar(win, bg: str | None = None, text: str | None = None):
    """Paint the native Windows title bar in the app's own colors (Windows 11; Windows 10 gets dark mode)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.c_void_p(int(win.winId()))
        dwm = ctypes.windll.dwmapi

        def ref(hx):
            c = QColor(hx)
            return c.red() | (c.green() << 8) | (c.blue() << 16)
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)  # immersive dark mode
        for attr, col in ((35, bg or P["bg"]), (34, bg or P["bg"]), (36, text or P["text"])):  # caption, border, text
            v = ctypes.c_uint(ref(col))
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
    except Exception:
        pass


def style_new_windows(app):
    """Dialogs (message boxes, pickers…) get the themed title bar too, the first time they take focus.
    Uses the focus-window signal rather than an app-wide event filter, so it costs nothing per event."""
    def on_focus(win):
        if win is None or win.flags() & (Qt.FramelessWindowHint | Qt.ToolTip) or win.property("cv_styled"):
            return
        win.setProperty("cv_styled", True)
        style_title_bar(win)
    app.focusWindowChanged.connect(on_focus)
