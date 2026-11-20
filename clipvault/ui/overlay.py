"""In-game pop-up ("Clip saved", "Bookmarked", …) — Medal-style feedback over the game.

A small frameless, always-on-top window that never takes focus and lets every click through,
shown on the monitor being recorded. Works over windowed / borderless games and over
"fullscreen" games that Windows runs with fullscreen optimizations (most DX11/DX12 games);
a game in true exclusive fullscreen draws over everything, so there you only get the sound.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QRectF, QEasingCurve
from PySide6.QtGui import QPainter, QColor, QFont, QPainterPath, QFontMetrics, QGuiApplication, QPen
from PySide6.QtWidgets import QWidget

from .widgets import P

GLYPH = {"clip": "✂", "bookmark": "⚑", "screenshot": "◉", "start": "●", "stop": "■", "error": "!"}


class GameOverlay(QWidget):
    W, H = 300, 62

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput
                         | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.resize(self.W, self.H)
        self.kind, self.title, self.sub = "clip", "", ""
        self._hide = QTimer(self, singleShot=True, timeout=self._fade_out)
        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(lambda: self.windowOpacity() < 0.05 and self.hide())

    # ------------------------------------------------------------------
    def show_msg(self, kind: str, title: str, sub: str = "", screen_name: str | None = None,
                 pos: str = "top-right", ms: int = 2400):
        self.kind, self.title, self.sub = kind, title, sub
        f = QFont(self.font())
        f.setPointSizeF(10.5)
        f.setBold(True)
        f2 = QFont(self.font())
        f2.setPointSizeF(9)
        w = max(QFontMetrics(f).horizontalAdvance(title), QFontMetrics(f2).horizontalAdvance(sub)) + 78
        self.resize(max(220, min(420, w)), self.H)
        scr = None
        if screen_name:
            scr = next((s for s in QGuiApplication.screens() if s.name().lower() == screen_name.lower()), None)
        scr = scr or QGuiApplication.primaryScreen()
        g = scr.geometry()
        m = 28
        x = g.left() + m if "left" in pos else g.right() - self.width() - m
        y = g.top() + m if pos.startswith("top") else g.bottom() - self.height() - m - 40
        self.move(x, y)
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
        self._topmost()
        self.update()
        self._anim.stop()
        self._anim.setDuration(140)
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._hide.start(ms)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setDuration(350)
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _topmost(self):
        """Belt and braces on Windows: no-activate, click-through, tool window, top of the z-order."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            GWL_EXSTYLE = -20
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | 0x08000000 | 0x00000020 | 0x00080000 | 0x00000080 | 0x00000008)
            # HWND_TOPMOST, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE|SWP_SHOWWINDOW
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010 | 0x0040)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        p.fillPath(path, QColor(12, 12, 16, 228))
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.drawPath(path)
        col = QColor({"clip": P.get("accent", "#ff7ac8"), "bookmark": P.get("accent2", "#7fd6ff"),
                      "screenshot": P.get("accent2", "#7fd6ff"), "start": P.get("rec", "#ff3b4e"),
                      "stop": P.get("ok", "#3ddc84"), "error": P.get("danger", "#ff4d5e")}.get(self.kind, "#ffffff"))
        c = QRectF(r.left() + 14, r.center().y() - 17, 34, 34)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(c)
        gf = QFont(self.font())
        gf.setPixelSize(17)
        gf.setBold(True)
        p.setFont(gf)
        p.setPen(QColor("#0b0b10"))
        p.drawText(c, Qt.AlignCenter, GLYPH.get(self.kind, "•"))
        f = QFont(self.font())
        f.setPointSizeF(10.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        tx = c.right() + 12
        p.drawText(QRectF(tx, r.top() + 10, r.right() - tx - 12, 22), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        if self.sub:
            f2 = QFont(self.font())
            f2.setPointSizeF(9)
            p.setFont(f2)
            p.setPen(QColor(255, 255, 255, 165))
            p.drawText(QRectF(tx, r.top() + 31, r.right() - tx - 12, 20), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(f2).elidedText(self.sub, Qt.ElideRight, int(r.right() - tx - 12)))
        p.end()
