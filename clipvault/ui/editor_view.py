"""Clip editor: smooth playback (mpv), zoomable timeline with waveform + bookmarks, trim,
per-track audio, export. Works for ClipVault recordings and linked Medal/NVIDIA/OBS files."""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
import textwrap
import uuid
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QScrollArea,
                               QLineEdit, QCheckBox, QComboBox, QListWidget, QListWidgetItem, QInputDialog,
                               QProgressBar, QSizePolicy, QMenu)

from ..core import ffmpeg as ff
from ..core.library import track_title, nice_title
from . import widgets as W
from .widgets import P, icon, Card
from .library_view import human_dur, human_size
from .player import VideoPlayer


def tc(s: float, ms=False) -> str:
    s = max(0.0, s)
    h, m, sec = int(s // 3600), int(s // 60 % 60), s % 60
    base = f"{h}:{m:02d}:" if h else f"{m}:"
    return base + (f"{sec:05.2f}" if ms else f"{int(sec):02d}")


class Timeline(QWidget):
    """Ruler + waveform are rendered once into a cached pixmap (re-rendered only on zoom/scroll/resize),
    so moving the playhead every frame costs almost nothing."""
    seek = Signal(float, bool)            # time, final (mouse released)
    trimChanged = Signal(float, float)
    bookmarkClicked = Signal(int)
    viewChanged = Signal()                # zoom / scroll changed (long files load waveform detail on demand)

    RULER_H, TRACK_TOP = 22, 30

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(118)
        self.setMouseTracking(True)
        self.duration = 1.0
        self.pos = 0.0
        self.a, self.b = 0.0, 1.0
        self.v0, self.v1 = 0.0, 1.0      # visible window
        self.bookmarks: list[dict] = []
        self.parts: list[dict] = []            # [{a, b, speed}] cuts joined into one clip
        self.peaks: np.ndarray | None = None  # whole-file waveform (short recordings)
        self.detail = None                     # (t0, t1, peaks) for the zoomed-in region of long recordings
        self.wave_state = "loading"            # loading | ready | none | zoom | detail_loading
        self._drag = None
        self._hover_x = None
        self._cache: QPixmap | None = None
        self._cache_key = None
        self.setFocusPolicy(Qt.ClickFocus)

    def load(self, duration: float, bookmarks):
        self.duration = max(0.1, duration)
        self.a, self.b = 0.0, self.duration
        self.v0, self.v1 = 0.0, self.duration
        self.bookmarks = bookmarks
        self.parts = []
        self.peaks = None
        self.detail = None
        self.wave_state = "loading"
        self.invalidate()

    def set_duration(self, d: float):
        if d <= 0 or abs(d - self.duration) < 0.05:
            return
        full_view = self.v0 <= 0.01 and self.v1 >= self.duration - 0.05
        full_trim = self.b >= self.duration - 0.05
        self.duration = d
        if full_view:
            self.v0, self.v1 = 0.0, d
        if full_trim or self.b > d:
            self.b = d
        self.trimChanged.emit(self.a, self.b)
        self.invalidate()
        self.viewChanged.emit()

    def set_detail(self, t0, t1, peaks):
        self.detail = (t0, t1, peaks) if peaks is not None and len(peaks) else None
        if self.detail is None and self.wave_state == "detail_loading":
            self.wave_state = "zoom"
        self.invalidate()

    def set_wave_state(self, state):
        if state != self.wave_state:
            self.wave_state = state
            self.invalidate()

    def set_peaks(self, peaks):
        self.peaks = peaks
        self.wave_state = "ready" if peaks is not None and len(peaks) else "none"
        self.invalidate()

    def invalidate(self):
        self._cache = None
        self.update()

    # coords
    def _x(self, t):
        return 12 + (t - self.v0) / max(1e-6, self.v1 - self.v0) * (self.width() - 24)

    def _t(self, x):
        return min(self.duration, max(0.0, self.v0 + (x - 12) / max(1, self.width() - 24) * (self.v1 - self.v0)))

    def zoom(self, factor: float, around: float | None = None):
        span = self.v1 - self.v0
        new = min(self.duration, max(2.0, span / factor))
        c = around if around is not None else self.pos
        frac = (c - self.v0) / span if span else 0.5
        self.v0 = max(0.0, c - frac * new)
        self.v1 = min(self.duration, self.v0 + new)
        self.v0 = max(0.0, self.v1 - new)
        self.invalidate()
        self.viewChanged.emit()

    def ensure_visible(self, t):
        span = self.v1 - self.v0
        if t < self.v0 or t > self.v1:
            self.v0 = max(0.0, min(self.duration - span, t - span * 0.1))
            self.v1 = self.v0 + span
            self.invalidate()
            self.viewChanged.emit()

    # events
    def wheelEvent(self, e):
        d = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.ShiftModifier or (e.angleDelta().x() and not e.angleDelta().y()):
            span = self.v1 - self.v0
            shift = -span * 0.1 * (1 if d > 0 else -1)
            self.v0 = min(max(0.0, self.v0 + shift), self.duration - span)
            self.v1 = self.v0 + span
            self.invalidate()
            self.viewChanged.emit()
        else:
            self.zoom(1.25 if d > 0 else 0.8, self._t(e.position().x()))

    def resizeEvent(self, e):
        self._cache = None
        super().resizeEvent(e)

    def _hit(self, x, y):
        for i, bm in enumerate(self.bookmarks):
            if abs(self._x(bm["t"]) - x) < 7 and y < self.TRACK_TOP + 4:
                return ("bm", i)
        if abs(self._x(self.a) - x) < 8 and y > self.TRACK_TOP - 4:
            return ("a", None)
        if abs(self._x(self.b) - x) < 8 and y > self.TRACK_TOP - 4:
            return ("b", None)
        return ("seek", None)

    def mousePressEvent(self, e):
        x, y = e.position().x(), e.position().y()
        kind, i = self._hit(x, y)
        if kind == "bm":
            self.bookmarkClicked.emit(i)
            self.seek.emit(self.bookmarks[i]["t"], True)
            return
        self._drag = kind
        if kind == "seek":
            self.pos = self._t(x)
            self.seek.emit(self.pos, False)
            self.update()

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        self._hover_x = x
        if self._drag == "seek":
            self.pos = self._t(x)
            self.seek.emit(self.pos, False)
        elif self._drag == "a":
            self.a = min(self._t(x), self.b - 0.5)
            self.trimChanged.emit(self.a, self.b)
        elif self._drag == "b":
            self.b = max(self._t(x), self.a + 0.5)
            self.trimChanged.emit(self.a, self.b)
        else:
            k, _ = self._hit(x, y)
            self.setCursor(Qt.SizeHorCursor if k in ("a", "b") else Qt.PointingHandCursor if k == "bm" else Qt.IBeamCursor)
        self.update()

    def mouseReleaseEvent(self, e):
        if self._drag == "seek":
            self.seek.emit(self._t(e.position().x()), True)
        self._drag = None

    def leaveEvent(self, e):
        self._hover_x = None
        self.update()

    # painting
    def _render_cache(self) -> QPixmap:
        w, h = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor(P["panel"]))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        span = self.v1 - self.v0
        steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]
        step = next((s for s in steps if span / s <= 10), 7200)
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(QColor(P["faint"]))
        t = (self.v0 // step) * step
        while t <= self.v1 + step:
            x = self._x(t)
            if -60 <= x <= w:
                p.drawLine(QPointF(x, 14), QPointF(x, self.RULER_H))
                p.drawText(QRectF(x + 3, 0, 70, 14), Qt.AlignLeft | Qt.AlignVCenter, tc(t, ms=step < 1))
                for k in range(1, 5):
                    xx = self._x(t + step * k / 5)
                    p.drawLine(QPointF(xx, 18), QPointF(xx, self.RULER_H))
            t += step
        tr = QRectF(12, self.TRACK_TOP, w - 24, h - self.TRACK_TOP - 10)
        path = QPainterPath()
        path.addRoundedRect(tr, 8, 8)
        p.fillPath(path, QColor(P["panel2"]))
        src = None
        if self.detail and self.detail[0] <= self.v0 + 1e-6 and self.detail[1] >= self.v1 - 1e-6:
            src = self.detail
        elif self.peaks is not None and len(self.peaks) and self.duration > 0:
            src = (0.0, self.duration, self.peaks)
        if src is not None:
            s0, s1, arr = src
            n = len(arr)
            cols = np.arange(int(tr.left()), int(tr.right()), 2, dtype=np.float64)
            t0 = self.v0 + (cols - 12) / max(1, w - 24) * span
            scale = n / max(1e-6, s1 - s0)
            i0 = np.clip(((t0 - s0) * scale).astype(np.int64), 0, n - 1)
            i1 = np.clip(((t0 - s0 + 2 * span / max(1, w - 24)) * scale).astype(np.int64), 0, n)
            i1 = np.minimum(np.maximum(i1, i0 + 1), n)
            # exact max over each column's [i0, i1) range, vectorized: reduceat over (start, end) pairs
            pk = np.append(arr, np.float32(0))
            idx = np.empty(len(i0) * 2, dtype=np.int64)
            idx[0::2], idx[1::2] = i0, i1
            vals = np.maximum.reduceat(pk, idx)[0::2] if len(i0) else np.array([])
            col = QColor(P["accent2"])
            col.setAlpha(150)
            p.setPen(QPen(col, 1))
            mid = tr.center().y()
            half = tr.height() / 2 - 4
            for x, v in zip(cols, vals):
                hh = max(1.0, float(v) * half)
                p.drawLine(QPointF(x, mid - hh), QPointF(x, mid + hh))
        else:
            p.setPen(QColor(P["faint"]))
            p.drawText(tr, Qt.AlignCenter, {"loading": "Loading waveform…", "detail_loading": "Loading waveform…",
                                            "zoom": "Long recording — zoom in (mouse wheel) to see the audio waveform",
                                            }.get(self.wave_state, "No audio"))
        p.end()
        return pm

    def paintEvent(self, e):
        key = (self.width(), self.height(), round(self.v0, 4), round(self.v1, 4), id(self.peaks), id(self.detail),
               self.wave_state)
        if self._cache is None or self._cache_key != key:
            self._cache = self._render_cache()
            self._cache_key = key
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap(0, 0, self._cache)
        w = self.width()
        tr = QRectF(12, self.TRACK_TOP, w - 24, self.height() - self.TRACK_TOP - 10)
        # dim outside trim
        xa, xb = self._x(self.a), self._x(self.b)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        if xa > tr.left():
            p.drawRect(QRectF(tr.left(), tr.top(), min(xa, tr.right()) - tr.left(), tr.height()))
        if xb < tr.right():
            p.drawRect(QRectF(max(xb, tr.left()), tr.top(), tr.right() - max(xb, tr.left()), tr.height()))
        # parts already added (numbered, in the second accent color)
        pc = QColor(P["accent2"])
        pf = QFont(self.font())
        pf.setPointSizeF(7.5)
        pf.setBold(True)
        for n, part in enumerate(self.parts, 1):
            x0, x1 = self._x(part["a"]), self._x(part["b"])
            if x1 < tr.left() or x0 > tr.right():
                continue
            x0, x1 = max(x0, tr.left()), min(x1, tr.right())
            fill = QColor(pc)
            fill.setAlpha(60)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRect(QRectF(x0, tr.top(), x1 - x0, tr.height()))
            p.setBrush(pc)
            p.drawRect(QRectF(x0, tr.bottom() - 4, x1 - x0, 4))
            lab = QRectF(x0 + 3, tr.top() + 3, 16 + (10 if abs(part.get("speed", 1) - 1) > 1e-3 else 0), 14)
            p.drawRoundedRect(lab, 3, 3)
            p.setPen(QColor("#0b0b10"))
            p.setFont(pf)
            sp = part.get("speed", 1)
            p.drawText(lab, Qt.AlignCenter, f"{n}" + (f" {sp:g}×" if abs(sp - 1) > 1e-3 else ""))
        acc = QColor(P["accent"])
        p.setPen(QPen(acc, 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(xa, tr.top(), xb - xa, tr.height()), 6, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(acc)
        for x in (xa, xb):
            if tr.left() - 6 <= x <= tr.right() + 6:
                p.drawRoundedRect(QRectF(x - 4, tr.top() + tr.height() / 2 - 14, 8, 28), 3, 3)
        for bm in self.bookmarks:
            x = self._x(bm["t"])
            if not (0 <= x <= w):
                continue
            c = QColor(P["accent2"] if not bm.get("clip") else P["ok"])
            p.setPen(QPen(c, 1, Qt.DashLine))
            p.drawLine(QPointF(x, self.TRACK_TOP - 6), QPointF(x, tr.bottom()))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            flag = QPainterPath()
            flag.moveTo(x, self.TRACK_TOP - 12)
            flag.lineTo(x + 9, self.TRACK_TOP - 9)
            flag.lineTo(x, self.TRACK_TOP - 5)
            flag.closeSubpath()
            p.drawPath(flag)
        if self._hover_x is not None:
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            p.drawLine(QPointF(self._hover_x, self.RULER_H), QPointF(self._hover_x, tr.bottom()))
        x = self._x(self.pos)
        if 0 <= x <= w:
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawLine(QPointF(x, 6), QPointF(x, tr.bottom()))
            p.setBrush(QColor("#ffffff"))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(x, 6), 5, 5)


class EditorView(QWidget):
    back = Signal()
    open_item = Signal(dict)

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.item: dict | None = None
        self.fps = 60.0
        self._load_token = 0
        self._scrub_target = None
        self.track_titles: list[str] = []
        self.track_gain: list[float] = []
        self.track_on: list[bool] = []
        self.cv_layout = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------- left: player + timeline
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(20, 14, 16, 14)
        lv.setSpacing(10)
        top = QHBoxLayout()
        b = QPushButton(" Library")
        b.setObjectName("ghost")
        b.setIcon(icon("back", P.get("muted")))
        b.clicked.connect(self._back)
        top.addWidget(b)
        self.name = QLabel()
        self.name.setObjectName("h2")
        top.addWidget(self.name, 1)
        self.engine_lbl = QLabel()
        self.engine_lbl.setObjectName("faint")
        top.addWidget(self.engine_lbl)
        lv.addLayout(top)

        self.player = VideoPlayer()
        self.player.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.player.durationChanged.connect(self._dur)
        self.player.playingChanged.connect(self._state)
        self.player.clicked.connect(self.toggle_play)
        self.player.failed.connect(lambda m: self.window().toast("Can't play this file", m[:140], error=True))
        self.engine_lbl.setText("GPU playback" if self.player.engine_name == "mpv" else "Basic playback")
        lv.addWidget(self.player, 1)

        ctr = QHBoxLayout()
        ctr.setSpacing(6)
        self.play_btn = QPushButton()
        self.play_btn.setObjectName("primary")
        self.play_btn.setIcon(icon("play", "#0b0b10"))
        self.play_btn.setFixedWidth(46)
        self.play_btn.clicked.connect(self.toggle_play)
        ctr.addWidget(self.play_btn)
        for ic, tip, fn in (("prev", "Previous bookmark", lambda: self._jump_bm(-1)),
                            ("next", "Next bookmark", lambda: self._jump_bm(1))):
            bb = QPushButton()
            bb.setIcon(icon(ic))
            bb.setToolTip(tip)
            bb.clicked.connect(fn)
            ctr.addWidget(bb)
        self.time = QLabel("0:00.00 / 0:00")
        self.time.setStyleSheet("font-family: Consolas, monospace; padding: 0 8px;")
        self.time.setMinimumWidth(170)
        ctr.addWidget(self.time)
        ctr.addStretch(1)
        bm = QPushButton(" Bookmark")
        bm.setIcon(icon("bookmark"))
        bm.setToolTip("Add bookmark at playhead (B)")
        bm.clicked.connect(self.add_bookmark)
        ctr.addWidget(bm)
        sin = QPushButton("Set start [I]")
        sin.clicked.connect(lambda: self._set_trim(a=self.timeline.pos))
        sout = QPushButton("Set end [O]")
        sout.clicked.connect(lambda: self._set_trim(b=self.timeline.pos))
        ctr.addWidget(sin)
        ctr.addWidget(sout)
        self.speed = QComboBox()
        for s in (0.25, 0.5, 1.0, 1.5, 2.0):
            self.speed.addItem(f"{s:g}×", s)
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(lambda: self.player.set_speed(self.speed.currentData()))
        ctr.addWidget(self.speed)
        ctr.addWidget(W.glyph_label("volume", 14))
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(90)
        self.vol.valueChanged.connect(self.player.set_volume)
        self.player.set_volume(80)
        ctr.addWidget(self.vol)
        zo = QPushButton()
        zo.setIcon(icon("zoom_out"))
        zo.setToolTip("Zoom out")
        zo.clicked.connect(lambda: self.timeline.zoom(1 / 1.6))
        zi = QPushButton()
        zi.setIcon(icon("zoom_in"))
        zi.setToolTip("Zoom in")
        zi.clicked.connect(lambda: self.timeline.zoom(1.6))
        ctr.addWidget(zo)
        ctr.addWidget(zi)
        lv.addLayout(ctr)

        self.timeline = Timeline()
        self.timeline.seek.connect(self._scrub)
        self.timeline.trimChanged.connect(self._trim_changed)
        lv.addWidget(self.timeline)
        hint = QLabel("Space or click video: play/pause · ←/→ 5s · Shift 1s · , . frame · I/O trim · A add part · "
                      "B bookmark · wheel zoom · Shift+wheel scroll")
        hint.setObjectName("faint")
        lv.addWidget(hint)
        root.addWidget(left, 1)

        # playhead/time polling — 30 fps, independent of the video engine's frame rate
        self._tick = QTimer(self, interval=33, timeout=self._poll)
        self._scrub_timer = QTimer(self, singleShot=True, interval=45, timeout=self._apply_scrub)
        # long recordings: waveform for the zoomed-in region only (reading a 35 GB file end to end is too slow)
        self._long_wave = False
        self._detail_busy = None
        self._detail_timer = QTimer(self, singleShot=True, interval=350, timeout=self._maybe_detail)
        self.timeline.viewChanged.connect(lambda: self._long_wave and self._detail_timer.start())

        # ---------------- right: inspector
        side = QScrollArea()
        side.setWidgetResizable(True)
        side.setFixedWidth(380)
        side.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side.setStyleSheet(f"QScrollArea {{ background: {P['panel']}; border-left: 1px solid {P['border']}; }}")
        inner = QWidget()
        # never wider than the panel (long file names / titles used to push the right side off screen)
        inner.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        inner.setObjectName("inspector")
        inner.setStyleSheet(f"QWidget#inspector {{ background: {P['panel']}; }}")
        self.sv = QVBoxLayout(inner)
        self.sv.setContentsMargins(16, 16, 16, 16)
        self.sv.setSpacing(14)
        side.setWidget(inner)
        root.addWidget(side)

        ex = Card("Export", "Save clip keeps full quality and is instant (cuts land on the nearest keyframe, ≤1 s). "
                            "Precise re-encodes on your GPU for frame-exact cuts.")
        self.trim_lbl = QLabel()
        self.trim_lbl.setObjectName("muted")
        ex.add(self.trim_lbl)
        ex.lay.addSpacing(8)
        self.out_name = QLineEdit()
        self.out_name.setPlaceholderText("Clip name")
        ex.add(self.out_name)
        ex.lay.addSpacing(6)
        self.keep_sep = QCheckBox("Keep separate audio tracks")
        ex.add(self.keep_sep)
        ex.lay.addSpacing(10)
        row = QHBoxLayout()
        self.save_fast = QPushButton(" Save clip")
        self.save_fast.setObjectName("primary")
        self.save_fast.setIcon(icon("cut", "#0b0b10"))
        self.save_fast.clicked.connect(lambda: self.export(precise=False))
        self.save_precise = QPushButton("Precise")
        self.save_precise.setToolTip("Re-encodes on your GPU — frame-exact cuts")
        self.save_precise.clicked.connect(lambda: self.export(precise=True))
        row.addWidget(self.save_fast, 1)
        row.addWidget(self.save_precise)
        ex.lay.addLayout(row)
        ex.lay.addSpacing(6)
        shot = QPushButton(" Save this frame as screenshot")
        shot.setIcon(icon("camera"))
        shot.clicked.connect(self.frame_screenshot)
        ex.add(shot)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        ex.lay.addSpacing(6)
        ex.add(self.progress)
        self.sv.addWidget(ex)

        # parts: several cuts joined into one clip
        pc = Card("Parts", "Combine several moments into one clip: select a range, then Add (or press A). "
                           "Save clip joins the parts in order.")
        prow = QHBoxLayout()
        self.add_part_btn = QPushButton(" Add selection")
        self.add_part_btn.setIcon(icon("add"))
        self.add_part_btn.clicked.connect(self.add_part)
        self.clear_parts_btn = QPushButton("Clear")
        self.clear_parts_btn.clicked.connect(self.clear_parts)
        prow.addWidget(self.add_part_btn, 1)
        prow.addWidget(self.clear_parts_btn)
        pc.lay.addLayout(prow)
        self.parts_list = QListWidget()
        self.parts_list.setMaximumHeight(120)
        self.parts_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.parts_list.customContextMenuRequested.connect(self._parts_menu)
        self.parts_list.itemDoubleClicked.connect(lambda li: self.seek(self.timeline.parts[li.data(Qt.UserRole)]["a"]))
        pc.add(self.parts_list)
        self.parts_hint = QLabel("Right-click a part to change its speed or remove it.")
        self.parts_hint.setObjectName("faint")
        self.parts_hint.setWordWrap(True)
        pc.add(self.parts_hint)
        self.sv.addWidget(pc)

        # style: vertical crop, speed, caption
        stc = Card("Style", "Format, speed and caption re-encode the clip on your GPU.")
        self.fmt = QComboBox()
        for k, label in (("", "Original"), ("9:16", "Vertical 9:16 (TikTok · Shorts · Reels)"), ("1:1", "Square 1:1"),
                         ("4:5", "Portrait 4:5 (Instagram)")):
            self.fmt.addItem(label, k)
        self.fmt.currentIndexChanged.connect(self._style_changed)
        stc.add(QLabel("Format"))
        stc.add(self.fmt)
        self.crop_row = QWidget()
        crl = QHBoxLayout(self.crop_row)
        crl.setContentsMargins(0, 4, 0, 0)
        crl.addWidget(QLabel("Crop position"))
        self.crop_x = QSlider(Qt.Horizontal)
        self.crop_x.setRange(0, 100)
        self.crop_x.setValue(50)
        self.crop_x.valueChanged.connect(self._style_changed)
        crl.addWidget(self.crop_x, 1)
        stc.add(self.crop_row)
        stc.lay.addSpacing(6)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Speed"))
        self.clip_speed = QComboBox()
        for sp in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            self.clip_speed.addItem(f"{sp:g}×" + ("  slow-mo" if sp < 1 else ""), sp)
        self.clip_speed.setCurrentIndex(3)
        self.clip_speed.setToolTip("Speed of the selection — and of each part you add from now on")
        self.clip_speed.currentIndexChanged.connect(self._style_changed)
        srow.addWidget(self.clip_speed, 1)
        stc.lay.addLayout(srow)
        stc.lay.addSpacing(6)
        stc.add(QLabel("Caption"))
        self.caption = QLineEdit()
        self.caption.setPlaceholderText("Text on the clip (optional)")
        self.caption.setMaxLength(90)
        self._cap_timer = QTimer(self, singleShot=True, interval=350, timeout=self._style_changed)
        self.caption.textChanged.connect(lambda: self._cap_timer.start())
        stc.add(self.caption)
        self.cap_pos = QComboBox()
        self.cap_pos.addItem("Top", "top")
        self.cap_pos.addItem("Bottom", "bottom")
        self.cap_pos.currentIndexChanged.connect(self._style_changed)
        stc.add(self.cap_pos)
        self.style_note = QLabel()
        self.style_note.setObjectName("faint")
        self.style_note.setWordWrap(True)
        stc.add(self.style_note)
        self.sv.addWidget(stc)
        self._preview_dir = Path(tempfile.mkdtemp(prefix="clipvault_cap_"))
        self._style_changed()

        self.tracks_card = Card("Audio tracks", "Choose which tracks go into the export and rebalance them.")
        self.tracks_box = QVBoxLayout()
        self.tracks_card.lay.addLayout(self.tracks_box)
        self.sv.addWidget(self.tracks_card)

        bmc = Card("Bookmarks")
        self.bm_list = QListWidget()
        self.bm_list.setMinimumHeight(140)
        self.bm_list.itemClicked.connect(lambda i: self.seek(i.data(Qt.UserRole)["t"]))
        self.bm_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.bm_list.customContextMenuRequested.connect(self._bm_menu)
        bmc.add(self.bm_list)
        tip = QLabel("Click to jump · right-click to rename, delete or clip around it")
        tip.setObjectName("faint")
        tip.setWordWrap(True)
        bmc.add(tip)
        self.sv.addWidget(bmc)

        self.sess_card = Card("Clips from this session")
        self.sess_list = QListWidget()
        self.sess_list.setMinimumHeight(90)
        self.sess_list.itemDoubleClicked.connect(lambda i: i.data(Qt.UserRole) and self.open_item.emit(i.data(Qt.UserRole)))
        self.sess_card.add(self.sess_list)
        self.sv.addWidget(self.sess_card)

        self.details = Card("Details")
        self.det_lbl = QLabel()
        self.det_lbl.setObjectName("muted")
        self.det_lbl.setWordWrap(True)
        self.det_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details.add(self.det_lbl)
        self.sv.addWidget(self.details)
        self.sv.addStretch(1)

        self.setFocusPolicy(Qt.StrongFocus)
        for btn in left.findChildren(QPushButton) + [self.speed]:
            btn.setFocusPolicy(Qt.NoFocus)  # otherwise Space re-clicks the last button instead of play/pause

    # ------------------------------------------------------------------ load
    def load(self, item: dict):
        self._load_token += 1
        token = self._load_token
        self._long_wave = False
        self._detail_busy = None
        self.item = dict(item)
        title = nice_title(item)
        self.name.setText(title)
        self.out_name.setText(title.replace(":", "").replace("·", "-") + " (edit)")
        dur = float(item.get("duration") or 0)
        self.timeline.load(dur or 1.0, list(item.get("bookmarks") or []))
        self.timeline.pos = 0.0
        self._trim_changed(0, self.timeline.b)
        self._fill_bookmarks()
        self._fill_parts()
        self.caption.setText("")
        self.clip_speed.setCurrentIndex(3)
        self._fill_tracks(item.get("audio_tracks") or [])
        self._fill_session()
        self._fill_details(item, {})
        self.player.load(item["path"], autoplay=True)
        self._tick.start()
        self.setFocus()
        # probe + waveform in the background: opening never hitches the UI
        path = item["path"]

        def work():
            info = ff.probe(path, timeout=120)  # unfinished Medal sessions can take a while to read
            W.ui_call(lambda: self._probe_ready(token, info))
            try:
                dur = float(info.get("format", {}).get("duration") or 0) or float(item.get("duration") or 0)
            except ValueError:
                dur = float(item.get("duration") or 0)
            if dur > self.FULL_WAVE_MAX:
                W.ui_call(lambda: self._start_long_wave(token))
                return
            peaks = self._compute_peaks(path)
            W.ui_call(lambda: self._peaks_ready(token, peaks))
        threading.Thread(target=work, daemon=True, name="EditorProbe").start()

    def _probe_ready(self, token, info):
        if token != self._load_token or not self.item:
            return
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        try:
            n, d = (v.get("avg_frame_rate") or "60/1").split("/")
            self.fps = float(n) / float(d) if float(d) else 60.0
        except Exception:
            self.fps = 60.0
        auds = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        titles = [track_title(s, i) for i, s in enumerate(auds)]
        if titles != self.track_titles:
            self.item["audio_tracks"] = titles
            self._fill_tracks(titles)
        try:
            dur = float(info.get("format", {}).get("duration") or 0)
        except ValueError:
            dur = 0
        if dur:
            self.timeline.set_duration(dur)
        self._fill_details(self.item, v)

    FULL_WAVE_MAX = 600      # whole-file waveform up to 10 min; longer files load it per zoomed region
    DETAIL_MAX_SPAN = 300    # zoom to ≤ 5 min to see the waveform of a long recording

    def _start_long_wave(self, token):
        if token != self._load_token:
            return
        self._long_wave = True
        self.timeline.set_wave_state("zoom")
        self._maybe_detail()

    def _maybe_detail(self):
        if not self._long_wave or not self.item:
            return
        tl = self.timeline
        span = tl.v1 - tl.v0
        if span > self.DETAIL_MAX_SPAN:
            tl.set_wave_state("zoom")
            return
        d = tl.detail
        if d and d[0] <= tl.v0 and d[1] >= tl.v1:
            return
        t0 = max(0.0, tl.v0 - 20)
        t1 = min(tl.duration, tl.v1 + 20)
        if self._detail_busy and self._detail_busy[0] <= tl.v0 and self._detail_busy[1] >= tl.v1:
            return  # already loading this area
        self._detail_busy = (t0, t1)
        tl.set_wave_state("detail_loading")
        token, path = self._load_token, self.item["path"]

        def work():
            peaks = self._compute_peaks(path, start=t0, length=t1 - t0, buckets=int((t1 - t0) * 50) + 1)

            def done():
                if token != self._load_token:
                    return
                if self._detail_busy == (t0, t1):
                    self._detail_busy = None
                tl.set_detail(t0, t1, peaks)
                self._maybe_detail()  # the view may have moved on while this was loading
            W.ui_call(done)
        threading.Thread(target=work, daemon=True, name="WaveDetail").start()

    def _compute_peaks(self, path, start=None, length=None, buckets: int = 8000):
        try:
            r = subprocess.run(ff.waveform_command(path, 0, 2000, start, length), capture_output=True, timeout=1800,
                               creationflags=ff.LOW_PRIORITY)
            a = np.frombuffer(r.stdout, dtype=np.int16)
            if a.size == 0:
                return None
            n = max(1, a.size // buckets)
            a = np.abs(a[: n * (a.size // n)].astype(np.float32)).reshape(-1, n).max(axis=1) / 32768.0
            return np.sqrt(a).astype(np.float32)  # perceptual
        except Exception:
            return None

    def _peaks_ready(self, token, peaks):
        if token == self._load_token:
            self._long_wave = False
            self.timeline.set_peaks(peaks)

    def _fill_details(self, item, v):
        dur = self.timeline.duration
        src = {"medal": "Medal", "nvidia": "NVIDIA ShadowPlay", "obs": "OBS"}.get(item.get("source") or "", "")
        det = [f"<b>Game</b>  {item.get('game')}",
               f"<b>Recorded</b>  {datetime.fromtimestamp(item['created']):%b %d, %Y %I:%M %p}",
               f"<b>Length</b>  {human_dur(dur)}", f"<b>Size</b>  {human_size(item.get('size', 0))}"]
        if v:
            det.append(f"<b>Video</b>  {v.get('width')}×{v.get('height')} · {str(v.get('codec_name', '?')).upper()} · {self.fps:.0f} fps")
        if item.get("linked"):
            det.append(f"<b>Source</b>  linked{' from ' + src if src else ''} — not made by ClipVault")
        else:
            det.append(f"<b>HDR</b>  {'kept as HDR10' if item.get('hdr') == 'hdr10' else 'auto-fixed to SDR' if item.get('hdr') == 'tonemap' else 'SDR'}")
        det.append(f"<b>Player</b>  {'GPU (mpv)' if self.player.engine_name == 'mpv' else 'Qt (basic)'}")
        # long paths have no spaces: add invisible break points so the panel can wrap them instead of widening
        path = re.sub(r"([\\/_.-])", "\\1\u200b", item["path"])
        path = html.escape(re.sub(r"([^\s\u200b]{24})", "\\1\u200b", path))
        det.append(f"<b>File</b>  {path}")
        self.det_lbl.setText("<br>".join(det))

    def _fill_bookmarks(self):
        self.bm_list.clear()
        self.timeline.bookmarks.sort(key=lambda b: b["t"])
        for bm in self.timeline.bookmarks:
            li = QListWidgetItem(f"{tc(bm['t'])}   {bm.get('label') or 'Bookmark'}")
            li.setData(Qt.UserRole, bm)
            li.setIcon(icon("check" if bm.get("clip") else "flag", P["ok"] if bm.get("clip") else P["accent2"]))
            self.bm_list.addItem(li)
        if not self.timeline.bookmarks:
            li = QListWidgetItem("No bookmarks — press B to add one")
            li.setFlags(Qt.NoItemFlags)
            self.bm_list.addItem(li)
        self.timeline.update()

    # -- audio tracks: ClipVault files have [Mixed, Game, Mic]; others (Medal/OBS/NVIDIA) are taken as-is
    def _fill_tracks(self, titles: list[str]):
        while self.tracks_box.count():
            w = self.tracks_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.track_titles = list(titles)
        self.cv_layout = len(titles) >= 3 and titles[0].lower().startswith("mix")
        self.track_gain = [1.0] * len(titles)
        self.track_on = [True] * len(titles) if self.cv_layout else [i == 0 for i in range(len(titles))]
        self.keep_sep.setEnabled(len(titles) > 1)
        if not titles:
            lab = QLabel("No audio")
            lab.setObjectName("muted")
            self.tracks_box.addWidget(lab)
            return
        for i, t in enumerate(titles):
            if self.cv_layout and i == 0:
                note = QLabel(f"<b>{t}</b> — rebuilt from the tracks below when you change them")
                note.setObjectName("faint")
                note.setWordWrap(True)
                self.tracks_box.addWidget(note)
                continue
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 4, 0, 4)
            on = QCheckBox()
            on.setChecked(self.track_on[i])
            on.setToolTip("Include this track in the export")
            lab = QLabel(t.replace("Game / PC audio", "Game"))
            sl = W.SliderValue(0, 200, 100, 5, "{:.0f}%", width=96)
            sl.lab.setMinimumWidth(40)

            def upd(_=None, i=i, on=on, sl=sl):
                self.track_on[i] = on.isChecked()
                self.track_gain[i] = sl.value() / 100
            on.toggled.connect(upd)
            sl.valueChanged.connect(upd)
            listen = QPushButton()
            listen.setIcon(icon("play"))
            listen.setToolTip("Preview just this track")
            listen.setFocusPolicy(Qt.NoFocus)
            listen.clicked.connect(lambda _=False, i=i: self._solo(i))
            h.addWidget(on)
            h.addWidget(lab, 1)
            h.addWidget(sl)
            h.addWidget(listen)
            self.tracks_box.addWidget(row)

    def _solo(self, i):
        self.player.set_audio_track(i)
        self.window().toast("Previewing track", self.track_titles[i] if i < len(self.track_titles) else "")

    def _audio_mix(self):
        """None = keep the default track as-is (lossless copy); else [(track_index, gain), ...] to mix."""
        n = len(self.track_titles)
        if n == 0:
            return None
        if self.cv_layout:
            parts = [(i, self.track_gain[i]) for i in range(1, n) if self.track_on[i]]
            if len(parts) == n - 1 and all(abs(g - 1) < 1e-3 for _, g in parts):
                return None
            return parts
        parts = [(i, self.track_gain[i]) for i in range(n) if self.track_on[i]]
        if parts == [(0, 1.0)] or (len(parts) == 1 and parts[0][0] == 0 and abs(parts[0][1] - 1) < 1e-3):
            return None
        return parts

    def _fill_session(self):
        self.sess_list.clear()
        sid = self.item.get("session_id")
        clips = [i for i in self.ctl.library.snapshot()
                 if sid and i.get("session_id") == sid and i["id"] != self.item["id"] and not i.get("trashed_at")]
        self.sess_card.setVisible(bool(sid))
        for c in sorted(clips, key=lambda c: c["created"]):
            li = QListWidgetItem(f"{nice_title(c)}  ·  {human_dur(c.get('duration', 0))}")
            li.setData(Qt.UserRole, c)
            li.setIcon(icon("film"))
            self.sess_list.addItem(li)
        if not clips:
            li = QListWidgetItem("No clips saved from this recording yet")
            li.setFlags(Qt.NoItemFlags)
            self.sess_list.addItem(li)

    # ------------------------------------------------------------------ playback
    def toggle_play(self):
        if not self.item:
            return
        if not self.player.is_playing() and self.timeline.pos >= self.timeline.b - 0.05:
            self.seek(self.timeline.a)
        self.player.toggle()

    def seek(self, t: float, exact: bool = True):
        t = max(0.0, min(t, self.timeline.duration))
        self.player.seek(t, exact)
        self.timeline.pos = t
        self.timeline.update()
        self._update_time(t)

    def _scrub(self, t: float, final: bool):
        """Timeline drag: show the playhead instantly, seek the video at most ~20×/s."""
        self._scrub_target = t
        self._update_time(t)
        if final:
            self._scrub_timer.stop()
            self.seek(t, exact=True)
            self._scrub_target = None
        elif not self._scrub_timer.isActive():
            self._scrub_timer.start()

    def _apply_scrub(self):
        if self._scrub_target is not None:
            self.player.seek(self._scrub_target, exact=True)

    def _poll(self):
        if not self.item or self._scrub_target is not None:
            return
        t = self.player.position()
        if abs(t - self.timeline.pos) > 0.001:
            self.timeline.pos = t
            if self.player.is_playing():
                self.timeline.ensure_visible(t)
                if self.timeline.b < self.timeline.duration - 0.05 and t >= self.timeline.b:
                    self.player.pause()
            self.timeline.update()
            self._update_time(t)

    def _update_time(self, t):
        txt = f"{tc(t, True)} / {tc(self.timeline.duration)}"
        if self.time.text() != txt:
            self.time.setText(txt)

    def _dur(self, d):
        self.timeline.set_duration(d)
        self._update_time(self.timeline.pos)

    def _state(self, playing):
        self.play_btn.setIcon(icon("pause" if playing else "play", "#0b0b10"))

    def _jump_bm(self, d):
        bms = sorted(self.timeline.bookmarks, key=lambda b: b["t"])
        t = self.timeline.pos
        cand = [b for b in bms if (b["t"] > t + 0.3 if d > 0 else b["t"] < t - 0.3)]
        if cand:
            self.seek(cand[0]["t"] if d > 0 else cand[-1]["t"])

    def _set_trim(self, a=None, b=None):
        if a is not None:
            self.timeline.a = max(0.0, min(a, self.timeline.b - 0.5))
        if b is not None:
            self.timeline.b = min(self.timeline.duration, max(b, self.timeline.a + 0.5))
        self._trim_changed(self.timeline.a, self.timeline.b)
        self.timeline.update()

    def _trim_changed(self, a, b):
        self.trim_lbl.setText(f"Selection  {tc(a, True)} → {tc(b, True)}   ({human_dur(b - a)})")

    # ------------------------------------------------------------------ parts & style
    def add_part(self):
        if not self.item:
            return
        a, b = self.timeline.a, self.timeline.b
        if b - a < 0.3:
            return
        self.timeline.parts.append({"a": a, "b": b, "speed": float(self.clip_speed.currentData() or 1.0)})
        self.timeline.parts.sort(key=lambda x: x["a"])
        self._fill_parts()
        self.window().toast("Part added", f"{tc(a)} → {tc(b)} — select the next moment and add it too")

    def clear_parts(self):
        self.timeline.parts.clear()
        self._fill_parts()

    def _fill_parts(self):
        self.parts_list.clear()
        total = 0.0
        for n, part in enumerate(self.timeline.parts):
            sp = part.get("speed", 1.0)
            total += (part["b"] - part["a"]) / sp
            li = QListWidgetItem(f"{n + 1}.  {tc(part['a'])} → {tc(part['b'])}   ({human_dur(part['b'] - part['a'])})"
                                 + (f"   {sp:g}×" if abs(sp - 1) > 1e-3 else ""))
            li.setData(Qt.UserRole, n)
            self.parts_list.addItem(li)
        n = len(self.timeline.parts)
        self.parts_list.setVisible(bool(n))
        self.parts_hint.setVisible(bool(n))
        self.clear_parts_btn.setEnabled(bool(n))
        self.save_fast.setText(f" Save clip ({n} parts · {human_dur(total)})" if n > 1 else
                               " Save clip (1 part)" if n == 1 else " Save clip")
        self.timeline.update()

    def _parts_menu(self, pos):
        li = self.parts_list.itemAt(pos)
        if not li:
            return
        i = li.data(Qt.UserRole)
        m = QMenu(self)
        sm = m.addMenu("Speed")
        for sp in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            act = sm.addAction(f"{sp:g}×")
            act.setCheckable(True)
            act.setChecked(abs(self.timeline.parts[i].get("speed", 1) - sp) < 1e-3)
            act.triggered.connect(lambda _=False, sp=sp: (self.timeline.parts[i].update(speed=sp), self._fill_parts()))
        m.addAction("Jump to it", lambda: self.seek(self.timeline.parts[i]["a"]))
        m.addAction("Remove", lambda: (self.timeline.parts.pop(i), self._fill_parts()))
        m.exec(self.parts_list.mapToGlobal(pos))

    def _caption_text(self) -> str:
        # emoji aren't in the caption font (they'd show as boxes); wrap long text onto up to 3 lines
        txt = "".join(ch for ch in self.caption.text().strip() if ord(ch) <= 0xFFFF and not 0x2600 <= ord(ch) <= 0x27BF)
        txt = " ".join(txt.split())
        width = 18 if self.fmt.currentData() in ("9:16", "4:5") else 30
        return "\n".join(textwrap.wrap(txt, width)[:3])

    def _write_caption(self, folder: Path) -> Path | None:
        txt = self._caption_text()
        if not txt:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"cap_{uuid.uuid4().hex[:8]}.txt"
        f.write_text(txt, "utf-8")
        return f

    def _effects(self, caption_file) -> dict:
        return {"aspect": self.fmt.currentData() or None, "crop_x": self.crop_x.value() / 100,
                "caption_file": caption_file, "caption_pos": self.cap_pos.currentData()}

    def _style_changed(self):
        aspect = self.fmt.currentData() or None
        self.crop_row.setVisible(bool(aspect))
        sp = float(self.clip_speed.currentData() or 1.0)
        has = bool(aspect or self._caption_text() or abs(sp - 1) > 1e-3)
        self.save_precise.setEnabled(not has)
        self.save_fast.setToolTip("Re-encodes on your GPU (format / speed / caption)" if has else
                                  "Full quality, instant — cuts land on the nearest keyframe")
        # live preview of crop + caption (speed you'll see in the export)
        for old in self._preview_dir.glob("cap_*.txt"):
            old.unlink(missing_ok=True)
        cap = self._write_caption(self._preview_dir)
        graph = ff.effects_filter(aspect, self.crop_x.value() / 100, 1.0, cap, self.cap_pos.currentData(), scale=False)
        ok = self.player.set_preview_filter(graph)
        self.style_note.setText("" if ok or not graph else "Preview needs GPU playback — the export will still have it.")

    # ------------------------------------------------------------------ bookmarks
    def add_bookmark(self):
        if not self.item:
            return
        t = self.timeline.pos
        self.timeline.bookmarks.append({"t": t, "label": f"Bookmark {len(self.timeline.bookmarks) + 1}"})
        self._persist_bookmarks()

    def _persist_bookmarks(self):
        self._fill_bookmarks()
        if self.item:
            self.ctl.library.update(self.item["id"], bookmarks=list(self.timeline.bookmarks))

    def _bm_menu(self, pos):
        li = self.bm_list.itemAt(pos)
        if not li or not li.data(Qt.UserRole):
            return
        bm = li.data(Qt.UserRole)
        m = QMenu(self)
        m.addAction(icon("rename"), "Rename…", lambda: self._rename_bm(bm))
        m.addAction(icon("cut"), "Select 30s around it",
                    lambda: self._set_trim(max(0, bm["t"] - 20), min(self.timeline.duration, bm["t"] + 10)))
        m.addAction(icon("cut"), "Select 60s before it", lambda: self._set_trim(max(0, bm["t"] - 60), bm["t"] + 2))
        m.addSeparator()
        m.addAction(icon("trash", P["danger"]), "Delete", lambda: self._del_bm(bm))
        m.exec(self.bm_list.mapToGlobal(pos))

    def _rename_bm(self, bm):
        name, ok = QInputDialog.getText(self, "Rename bookmark", "Label:", text=bm.get("label", ""))
        if ok:
            for b in self.timeline.bookmarks:
                if abs(b["t"] - bm["t"]) < 1e-6:
                    b["label"] = name
            self._persist_bookmarks()

    def _del_bm(self, bm):
        self.timeline.bookmarks = [b for b in self.timeline.bookmarks if abs(b["t"] - bm["t"]) > 1e-6]
        self._persist_bookmarks()

    # ------------------------------------------------------------------ export
    def export(self, precise: bool):
        if not self.item:
            return
        a, b = self.timeline.a, self.timeline.b
        src = self.item["path"]
        game = self.item.get("game")
        name = "".join(c for c in (self.out_name.text().strip() or "Clip") if c not in '<>:"/\\|?*').strip() or "Clip"
        out = self.ctl.library.folder("clip", game) / f"{name}.mp4"
        k = 2
        while out.exists():
            out = out.with_name(f"{name} ({k}).mp4")
            k += 1
        mix = self._audio_mix()
        work_dir = out.parent / f".clipvault_parts_{uuid.uuid4().hex[:8]}"
        cap = self._write_caption(work_dir)
        fx = self._effects(cap)
        parts = [dict(x) for x in self.timeline.parts] or [{"a": a, "b": b,
                                                           "speed": float(self.clip_speed.currentData() or 1.0)}]
        # every part must come out with the same codec settings to be joined: one re-encodes -> all re-encode
        if any(abs(x["speed"] - 1) > 1e-3 for x in parts) or fx["aspect"] or cap:
            precise = True
        kw = dict(precise=precise, audio_mix=mix, keep_separate=self.keep_sep.isChecked() and self.keep_sep.isEnabled(),
                  cfg=self.ctl.cfg, hdr10=self.item.get("hdr") == "hdr10", **fx)
        self.progress.show()
        self.save_fast.setEnabled(False)
        self.save_precise.setEnabled(False)
        item = self.item
        bms = [dict(bm, t=bm["t"] - a) for bm in self.timeline.bookmarks if a <= bm["t"] <= b] if len(parts) == 1 \
            and not self.timeline.parts else []

        def one(pa, pb, dst, speed, **extra):
            k = dict(kw, speed=speed, **extra)
            r = ff.run(ff.export_command(src, str(dst), pa, pb, **k), timeout=7200)
            if r.returncode != 0 and not k["precise"]:
                # some linked files (e.g. odd audio codecs) can't be stream-copied into .mp4: re-encode audio
                r = ff.run(ff.export_command(src, str(dst), pa, pb, **dict(k, audio_reencode=True)), timeout=7200)
            return r

        def work():
            try:
                if len(parts) == 1:
                    r = one(parts[0]["a"], parts[0]["b"], out, parts[0]["speed"])
                    if r.returncode != 0 and not kw["precise"]:  # last resort: full re-encode
                        r = one(parts[0]["a"], parts[0]["b"], out, parts[0]["speed"], precise=True)
                else:
                    def run_parts(**extra):
                        files = []
                        for n, part in enumerate(parts):
                            dst = work_dir / f"part{n:03d}.mp4"
                            rr = one(part["a"], part["b"], dst, part["speed"], **extra)
                            if rr.returncode != 0:
                                return rr
                            files.append(dst)
                        lst = work_dir / "parts.txt"
                        lst.write_text("".join(f"file '{f.name}'\n" for f in files), "utf-8")  # relative: game folders can have apostrophes
                        return ff.run(ff.concat_parts_command(lst, str(out)), timeout=7200)
                    work_dir.mkdir(parents=True, exist_ok=True)
                    r = run_parts()
                    if r.returncode != 0 and not kw["precise"]:
                        r = run_parts(precise=True, audio_reencode=True)
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
            ok = r.returncode == 0 and out.exists()
            new = None
            if ok:
                sid = item.get("session_id") or (item["id"] if item["kind"] == "session" else None)
                new = self.ctl.library.add(out, "clip", game, title=name, session_id=sid, bookmarks=bms,
                                           hdr=item.get("hdr"), seen=True)
                if item["kind"] == "session" and not item.get("session_id"):
                    self.ctl.library.update(item["id"], session_id=item["id"])
            W.ui_call(lambda: self._export_done(ok, out, r.stderr[-400:] if not ok else "", new))
        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, ok, out, err, new):
        self.progress.hide()
        self.save_fast.setEnabled(True)
        self._style_changed()
        if ok:
            self.window().toast("Clip saved", out.name)
            if self.item and new:
                self.item["session_id"] = self.item.get("session_id") or new.get("session_id")
            self._fill_session()
        else:
            self.window().toast("Export failed", err.strip().splitlines()[-1] if err.strip() else "", error=True)

    def frame_screenshot(self):
        if not self.item:
            return
        t = self.timeline.pos
        out = self.ctl.library.folder("screenshot", self.item.get("game")) / \
            f"{Path(self.item['path']).stem} @ {tc(t).replace(':', '-')}.png"
        src, game = self.item["path"], self.item.get("game")

        def work():
            r = ff.run([ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", src,
                        "-frames:v", "1", str(out)], timeout=60)
            if r.returncode == 0:
                self.ctl.library.add(out, "screenshot", game, seen=True)
                W.ui_call(lambda: self.window().toast("Screenshot saved", out.name))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ keys / nav
    def keyPressEvent(self, e):
        k, shift = e.key(), bool(e.modifiers() & Qt.ShiftModifier)
        step = 1.0 if shift else 5.0
        if k == Qt.Key_Space:
            self.toggle_play()
        elif k == Qt.Key_Left:
            self.seek(self.timeline.pos - step)
        elif k == Qt.Key_Right:
            self.seek(self.timeline.pos + step)
        elif k == Qt.Key_Comma:
            self.player.step(-1)
            QTimer.singleShot(120, self._poll)
        elif k == Qt.Key_Period:
            self.player.step(1)
            QTimer.singleShot(120, self._poll)
        elif k == Qt.Key_I:
            self._set_trim(a=self.timeline.pos)
        elif k == Qt.Key_O:
            self._set_trim(b=self.timeline.pos)
        elif k == Qt.Key_B:
            self.add_bookmark()
        elif k == Qt.Key_A:
            self.add_part()
        elif k == Qt.Key_Escape:
            self._back()
        else:
            super().keyPressEvent(e)

    def leave(self):
        """Called when navigating away: stop playback and release the file."""
        self._tick.stop()
        self._scrub_timer.stop()
        self._detail_timer.stop()
        self.player.unload()
        self._load_token += 1

    def _back(self):
        self.leave()
        self.back.emit()

    def shutdown(self):
        self.leave()
        self.player.shutdown()
