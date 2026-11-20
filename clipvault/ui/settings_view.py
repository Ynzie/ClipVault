"""Settings: recording, hotkeys, quality, HDR (with live preview), audio (mic sensitivity!), storage, alerts, look."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices, QColor, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QScrollArea,
                               QListWidget, QListWidgetItem, QLineEdit, QFileDialog, QSpinBox, QPlainTextEdit,
                               QStackedWidget, QFrame, QProgressBar, QApplication, QInputDialog, QSizePolicy,
                               QGridLayout, QDialog)

from ..core import audio as audiomod
from ..core import ffmpeg as ff
from ..core import hdr as hdrmod
from ..core.config import QUALITY_PRESETS, app_data_dir, APP_VERSION
from . import widgets as W
from .theme import THEMES
from .widgets import P, Card, Toggle, Segmented, SliderValue, HotkeyEdit, icon
from .library_view import human_size

PAGES = [
    ("recording", "Recording", "rec"), ("hotkeys", "Hotkeys", "keyboard"), ("quality", "Quality", "film"),
    ("hdr", "HDR", "sun"), ("audio", "Audio & Mic", "mic"), ("storage", "Storage", "storage"),
    ("alerts", "Sounds & alerts", "bell"), ("look", "Appearance", "color"), ("advanced", "Advanced", "settings"),
]


class CardColumns(QWidget):
    """Settings cards: one column on a normal window, two side by side on a wide one. Pages just call
    addWidget / addLayout like on a normal layout. In two columns the cards are split so both columns end
    up about the same height (each column keeps the page's order), and a card marked grow=True (e.g. the
    games lists) stretches to use the rest of the window's height instead of leaving it empty."""
    ONE_MAX, TWO_MIN, TWO_MAX = 900, 1280, 2300

    def __init__(self):
        super().__init__()
        self._items: list[QWidget] = []
        self._ncols = 0
        self._avail = 0
        self._split = None
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 6, 0, 0)
        h.setSpacing(18)
        self._cols = []
        for _ in range(2):
            c = QVBoxLayout()
            c.setSpacing(16)
            h.addLayout(c, 1)
            self._cols.append(c)
        self.setMaximumWidth(self.ONE_MAX)
        self._relayout = QTimer(self, singleShot=True, interval=0, timeout=self._place)

    def addWidget(self, w, stretch=0, *_):
        self._items.append(w)
        self._relayout.start()

    def addLayout(self, lay, stretch=0):
        box = QWidget()
        box.setLayout(lay)
        self.addWidget(box)

    def set_available(self, width: int):
        n = 2 if width >= self.TWO_MIN else 1
        self.setMaximumWidth(self.TWO_MAX if n == 2 else self.ONE_MAX)
        if n != self._ncols or (n == 2 and abs(width - self._avail) > 40):
            self._ncols, self._avail = n, width
            self._relayout.start()

    def showEvent(self, e):
        super().showEvent(e)
        self._relayout.start()

    def _heights(self, colw: int) -> list[int]:
        out = []
        for w in self._items:
            hh = w.heightForWidth(colw) if w.hasHeightForWidth() else -1
            out.append(max(hh, w.minimumSizeHint().height(), 1) if hh > 0 else w.sizeHint().height())
        return out

    def _choose_split(self, hs: list[int]) -> list[int]:
        """Column for each card: smallest tallest-column (then most even), first card always on the left."""
        n = len(hs)
        if n <= 1:
            return [0] * n
        if n > 12:  # (not on any page today) greedy fallback
            cols, tot = [], [0, 0]
            for x in hs:
                i = 0 if tot[0] <= tot[1] else 1
                cols.append(i)
                tot[i] += x
            return cols
        best, best_key = None, None
        for mask in range(1 << (n - 1)):
            cols = [0] + [(mask >> k) & 1 for k in range(n - 1)]
            t0 = sum(h for h, c in zip(hs, cols) if c == 0)
            t1 = sum(h for h, c in zip(hs, cols) if c == 1)
            if t1 == 0:
                continue
            key = (max(t0, t1), abs(t0 - t1), [-c for c in cols])
            if best_key is None or key < best_key:
                best, best_key = cols, key
        return best or [0] * n

    def _place(self):
        n = max(1, self._ncols or 1)
        if n == 2:
            colw = max(200, (min(self._avail, self.TWO_MAX) - 18) // 2)
            split = self._choose_split(self._heights(colw))
        else:
            split = [0] * len(self._items)
        if split == self._split and n == getattr(self, "_ncols_placed", None):
            return
        self._split, self._ncols_placed = split, n
        for c in self._cols:
            while c.count():
                c.takeAt(0)
        grow = [False, False]
        for w, i in zip(self._items, split):
            g = bool(w.property("grow"))
            self._cols[i].addWidget(w, 1 if g else 0)
            grow[i] |= g
        for i, c in enumerate(self._cols):
            if not grow[i]:
                c.addStretch(1)
        self.layout().setStretch(1, 1 if n == 2 else 0)


class _PageScroll(QScrollArea):
    def __init__(self, cols):
        super().__init__()
        self._cols = cols

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._cols.set_available(self.viewport().width() - 68)


def page_scaffold(title: str, sub: str, glyph: str):
    holder = CardColumns()
    scroll = _PageScroll(holder)
    scroll.setWidgetResizable(True)
    inner = QWidget()
    v = QVBoxLayout(inner)
    v.setContentsMargins(34, 26, 34, 34)
    v.setSpacing(16)
    head = QHBoxLayout()
    g = W.glyph_label(glyph, 26, P["accent"])
    head.addWidget(g)
    col = QVBoxLayout()
    col.setSpacing(0)
    t = QLabel(title)
    t.setObjectName("h1")
    s = QLabel(sub)
    s.setObjectName("muted")
    s.setWordWrap(True)
    col.addWidget(t)
    col.addWidget(s)
    head.addLayout(col, 1)
    v.addLayout(head)
    v.addWidget(holder, 1)  # cards fill down to the bottom (a "grow" card stretches; others stay at the top)
    scroll.setWidget(inner)
    return scroll, holder


class AppPicker(QDialog):
    """Pick from apps that are open right now (no need to know the exact .exe name)."""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 520)
        self.result_exe = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)
        tip = QLabel("Open the game first, then pick it here. Showing apps with a window open right now.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        v.addWidget(tip)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by window title or exe…")
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._accept_sel)
        top.addWidget(self.search, 1)
        ref = QPushButton("Refresh")
        ref.clicked.connect(self._load)
        top.addWidget(ref)
        v.addLayout(top)
        self.list = QListWidget()
        self.list.setStyleSheet(f"QListWidget {{ background: {P['panel2']}; border: 1px solid {P['border']}; border-radius: 8px; }}")
        self.list.itemDoubleClicked.connect(lambda _: self._accept_sel())
        v.addWidget(self.list, 1)
        row = QHBoxLayout()
        browse = QPushButton("Browse for .exe…")
        browse.clicked.connect(self._browse)
        typed = QPushButton("Type name…")
        typed.clicked.connect(self._typed)
        row.addWidget(browse)
        row.addWidget(typed)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        add = QPushButton("Add")
        add.setObjectName("primary")
        add.clicked.connect(self._accept_sel)
        row.addWidget(cancel)
        row.addWidget(add)
        v.addLayout(row)
        self._load()

    def _load(self):
        from ..core.games import list_open_apps
        self.list.clear()
        for app in list_open_apps():
            it = QListWidgetItem(f"{app['title']}\n{app['exe']}")
            it.setData(Qt.UserRole, app["exe"])
            it.setToolTip(app["path"])
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            it = QListWidgetItem("No open apps found — use Browse or Type name")
            it.setFlags(Qt.NoItemFlags)
            self.list.addItem(it)
        self._filter(self.search.text())

    def _filter(self, text):
        t = text.lower().strip()
        first = None
        for i in range(self.list.count()):
            it = self.list.item(i)
            hide = bool(t) and t not in it.text().lower()
            it.setHidden(hide)
            if not hide and first is None and it.data(Qt.UserRole):
                first = it
        cur = self.list.currentItem()
        if first is not None and (cur is None or cur.isHidden()):
            self.list.setCurrentItem(first)

    def _accept_sel(self):
        it = self.list.currentItem()
        if it and it.data(Qt.UserRole) and not it.isHidden():
            self.result_exe = it.data(Qt.UserRole)
            self.accept()

    def _browse(self):
        f, _ = QFileDialog.getOpenFileName(self, "Pick the game's .exe", "C:\\", "Programs (*.exe)")
        if f:
            self.result_exe = Path(f).name
            self.accept()

    def _typed(self):
        name, ok = QInputDialog.getText(self, "Add executable", "Process name (e.g. iw4x.exe):")
        if ok and name.strip():
            n = name.strip()
            self.result_exe = n if n.lower().endswith(".exe") else n + ".exe"
            self.accept()

    @staticmethod
    def pick(parent, title) -> str:
        d = AppPicker(parent, title)
        return d.result_exe if d.exec() == QDialog.Accepted else ""


def combo(items: list[tuple], value, on_change, width=220) -> QComboBox:
    c = QComboBox()
    c.setMinimumWidth(width)
    for data, label in items:
        c.addItem(label, data)
    i = c.findData(value)
    c.setCurrentIndex(max(0, i))
    c.currentIndexChanged.connect(lambda _: on_change(c.currentData()))
    return c


def toggle(cfg, key) -> Toggle:
    t = Toggle(bool(cfg.get(key)))
    t.toggled.connect(lambda v: cfg.set(key, v))
    return t


class SettingsView(QWidget):
    theme_changed = Signal(str)

    def __init__(self, ctl):
        super().__init__()
        self.ctl, self.cfg = ctl, ctl.cfg
        W.HotkeyEdit.suspend_hook = ctl.hotkeys.suspend
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        nav = QWidget()
        nav.setFixedWidth(220)
        nav.setStyleSheet(f"background: {P['bg']}; border-right: 1px solid {P['border']};")
        nv = QVBoxLayout(nav)
        nv.setContentsMargins(12, 20, 12, 12)
        lab = QLabel("SETTINGS")
        lab.setObjectName("section")
        nv.addWidget(lab)
        self.nav = QListWidget()
        self.nav.setStyleSheet("QListWidget { border: none; }")
        for key, label, g in PAGES:
            it = QListWidgetItem(icon(g, P["muted"]), "  " + label)
            it.setData(Qt.UserRole, key)
            self.nav.addItem(it)
        nv.addWidget(self.nav, 1)
        ver = QLabel(f"ClipVault {APP_VERSION}\nLocal-only · no ads · no uploads")
        ver.setObjectName("faint")
        nv.addWidget(ver)
        h.addWidget(nav)
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.pages = {}
        builders = {"recording": self._recording, "hotkeys": self._hotkeys, "quality": self._quality, "hdr": self._hdr,
                    "audio": self._audio, "storage": self._storage, "alerts": self._alerts, "look": self._look,
                    "advanced": self._advanced}
        for key, label, g in PAGES:
            w = builders[key]()
            self.pages[key] = w
            self.stack.addWidget(w)
        self._meter_timer = QTimer(self, interval=33, timeout=self._tick_meters)
        self._audio_held = False
        self.nav.currentRowChanged.connect(self._page_changed)
        self.nav.setCurrentRow(0)

    def show_page(self, key: str):
        for i in range(self.nav.count()):
            if self.nav.item(i).data(Qt.UserRole) == key:
                self.nav.setCurrentRow(i)

    def _page_changed(self, row):
        self.stack.setCurrentIndex(row)
        key = PAGES[row][0]
        if key == "audio":
            self._hold_audio(True)
        else:
            self._hold_audio(False)
        if key == "storage":
            self._refresh_storage()
        if key == "advanced":
            self._refresh_diag()
        if key == "hdr":
            self._refresh_hdr_status()

    def hideEvent(self, e):
        self._hold_audio(False)
        super().hideEvent(e)

    def showEvent(self, e):
        if PAGES[self.nav.currentRow()][0] == "audio":
            self._hold_audio(True)
        super().showEvent(e)

    def _hold_audio(self, on):
        if on and not self._audio_held:
            self.ctl.audio.acquire()
            self._audio_held = True
            self._meter_timer.start()
        elif not on and self._audio_held:
            self.ctl.audio.stop_monitor()
            if hasattr(self, "listen_btn"):
                self.listen_btn.blockSignals(True)
                self.listen_btn.setChecked(False)
                self.listen_btn.blockSignals(False)
            self._meter_timer.stop()
            self.ctl.audio.release()
            self._audio_held = False

    # ================================================================== Recording
    def _recording(self):
        page, v = page_scaffold("Recording", "When ClipVault records and how much it keeps ready for a clip.", "rec")
        c = Card("Recording mode")
        seg = Segmented([("manual", "Manual"), ("auto", "Auto (when a game is focused)"), ("always", "Always on")],
                        self.cfg.get("recording.mode"))
        seg.changed.connect(lambda k: self.cfg.set("recording.mode", k))
        c.add(seg)
        note = QLabel("Auto starts the replay buffer when a game is detected and stops 30 s after you leave it. "
                      "Always on records your desktop too (good for VR mirror windows).")
        note.setObjectName("muted")
        note.setWordWrap(True)
        c.lay.addSpacing(8)
        c.add(note)
        c.lay.addSpacing(6)
        c.add_row("Automatically record entire game session", "Starts a long recording when a game launches and saves it "
                  "when the game closes — nothing is ever missed.", toggle(self.cfg, "recording.full_session"))
        c.add_row("Review after the game closes", "Pops up your session and clips so you can keep, delete or clip them right away.",
                  toggle(self.cfg, "recording.review_after_game"))
        c.add_row("Clip key during a session", "What your clip hotkey does while a long recording is running.",
                  combo([("bookmark", "Add a bookmark"), ("clip", "Save a clip"), ("both", "Bookmark + save a clip")],
                        self.cfg.get("recording.session_key"), lambda d: self.cfg.set("recording.session_key", d), 220))
        c.add_row("Hold clip key to split session", "Hold ~1 s to save the current session and start a new one (one file per match).",
                  toggle(self.cfg, "recording.long_press_split"))
        v.addWidget(c)

        c = Card("Clip buffer")
        lens = Segmented([(str(s), l) for s, l in ((15, "15 s"), (30, "30 s"), (60, "1 min"), (120, "2 min"), (300, "5 min"))],
                         str(self.cfg.get("recording.clip_seconds")))
        lens.changed.connect(lambda k: self.cfg.set("recording.clip_seconds", int(k)))
        c.add_row("Clip length", "How much the clip hotkey saves.", lens)
        c.add_row("Keep in buffer", "Extra history on disk so you can grab longer moments from the editor later.",
                  combo([(120, "2 minutes"), (300, "5 minutes"), (600, "10 minutes"), (1200, "20 minutes")],
                        self.cfg.get("recording.buffer_seconds"), lambda d: self.cfg.set("recording.buffer_seconds", d)))
        v.addWidget(c)

        c = Card("Capture")
        self.mon_combo = QComboBox()
        self.mon_combo.setMinimumWidth(220)
        self.mon_combo.addItem(f"Display {int(self.cfg.get('recording.monitor', 0)) + 1}", self.cfg.get("recording.monitor", 0))
        self.mon_combo.currentIndexChanged.connect(lambda _: self.cfg.set("recording.monitor", self.mon_combo.currentData()))
        detect = QPushButton("Detect")
        detect.clicked.connect(self._detect_monitors)
        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(self.mon_combo)
        rh.addWidget(detect)
        c.add_row("Monitor", "Which display to record (captures exactly what's on screen — games, VR mirror, desktop).", row)
        c.add_row("Show mouse cursor", "", toggle(self.cfg, "recording.show_cursor"))
        c.add_row("Frame rate mode", "VFR saves space and only stores changed frames. CFR is friendlier for video editors.",
                  combo([("vfr", "Variable (VFR)"), ("cfr", "Constant (CFR)")], self.cfg.get("recording.framerate_mode"),
                        lambda d: self.cfg.set("recording.framerate_mode", d), 180))
        v.addWidget(c)

        c = Card("Games", "Executables that count as games. Anything fullscreen counts too (unless ignored).")
        c.add_row("Detect fullscreen apps as games", "", toggle(self.cfg, "recording.detect_fullscreen"))
        lists = QHBoxLayout()
        for key, label in (("games", "Always a game"), ("ignored", "Never a game")):
            col = QVBoxLayout()
            l = QLabel(label)
            l.setStyleSheet("font-weight: 600;")
            col.addWidget(l)
            lw = QListWidget()
            lw.setMinimumHeight(170)
            lw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)  # taller lists on a tall window
            lw.setStyleSheet(f"QListWidget {{ background: {P['panel2']}; border: 1px solid {P['border']}; border-radius: 8px; }}")
            for exe in self.cfg.get(f"recording.{key}"):
                lw.addItem(exe)
            col.addWidget(lw)
            btns = QHBoxLayout()
            add = QPushButton("Add…")
            rem = QPushButton("Remove")

            def do_add(_=False, key=key, lw=lw):
                name = AppPicker.pick(self, "Add a game" if key == "games" else "Never treat as a game")
                ok = bool(name)
                if ok and name.strip():
                    lst = list(self.cfg.get(f"recording.{key}"))
                    if name.strip() not in lst:
                        lst.append(name.strip())
                        self.cfg.set(f"recording.{key}", lst)
                        lw.addItem(name.strip())
                        # an exe can only be on one list
                        other = "ignored" if key == "games" else "games"
                        rest = [x for x in self.cfg.get(f"recording.{other}") if x.lower() != name.strip().lower()]
                        if len(rest) != len(self.cfg.get(f"recording.{other}")):
                            self.cfg.set(f"recording.{other}", rest)
                            for lw2 in self.findChildren(QListWidget):
                                for it in lw2.findItems(name.strip(), Qt.MatchFixedString):
                                    if lw2 is not lw:
                                        lw2.takeItem(lw2.row(it))

            def do_rem(_=False, key=key, lw=lw):
                for it in lw.selectedItems():
                    lst = [x for x in self.cfg.get(f"recording.{key}") if x != it.text()]
                    self.cfg.set(f"recording.{key}", lst)
                    lw.takeItem(lw.row(it))
            add.clicked.connect(do_add)
            rem.clicked.connect(do_rem)
            btns.addWidget(add)
            btns.addWidget(rem)
            btns.addStretch(1)
            col.addLayout(btns)
            lists.addLayout(col)
        c.lay.addSpacing(8)
        c.lay.addLayout(lists, 1)
        c.setProperty("grow", True)
        v.addWidget(c)
        return page

    def _detect_monitors(self):
        self.mon_combo.setEnabled(False)
        threading.Thread(target=lambda: (lambda m: W.ui_call(lambda: self._fill_monitors(m)))(ff.probe_monitors()),
                         daemon=True).start()

    def _fill_monitors(self, mons):
        self.mon_combo.setEnabled(True)
        self.mon_combo.blockSignals(True)
        self.mon_combo.clear()
        for m in mons:
            self.mon_combo.addItem(f"Display {m['idx'] + 1}  ·  {m['w']}×{m['h']}", m["idx"])
        self.mon_combo.setCurrentIndex(max(0, self.mon_combo.findData(self.cfg.get("recording.monitor"))))
        self.mon_combo.blockSignals(False)
        self.ctl.recorder._mons = mons

    # ================================================================== Hotkeys
    def _hotkeys(self):
        page, v = page_scaffold("Hotkeys", "Global shortcuts — they work while you're in a fullscreen game.", "keyboard")
        c = Card()
        rows = [("clip", "Save clip", "Save the last moments of gameplay (bookmark during a session; hold to split)."),
                ("clip_alt", "Save long clip", "A second clip key with its own length."),
                ("long_recording", "Start / stop long recording", "Records the whole session; clips you take are linked to it."),
                ("bookmark", "Bookmark", "Mark a moment during a long recording (makes a clip if none is running)."),
                ("screenshot", "Screenshot", "Instant screenshot of your game (HDR-fixed).")]
        for key, title, desc in rows:
            e = HotkeyEdit(self.cfg.get(f"hotkeys.{key}"))
            e.changed.connect(lambda combo, key=key: self.cfg.set(f"hotkeys.{key}", combo))
            c.add_row(title, desc, e)
        c.add_row("Long clip length", "", combo([(60, "1 minute"), (120, "2 minutes"), (300, "5 minutes"), (600, "10 minutes")],
                  self.cfg.get("recording.clip_alt_seconds"), lambda d: (self.cfg.set("recording.clip_alt_seconds", d),
                  self.cfg.set("recording.buffer_seconds", max(d, self.cfg.get("recording.buffer_seconds")))), 160))
        v.addWidget(c)
        tip = QFrame()
        tip.setObjectName("hint")
        th = QHBoxLayout(tip)
        th.setContentsMargins(14, 10, 14, 10)
        tl = QLabel("Click a hotkey, then press the new combo. Esc cancels, Backspace clears. Side mouse buttons work too.")
        tl.setWordWrap(True)
        th.addWidget(tl)
        v.addWidget(tip)
        self.hk_warn = QLabel()
        self.hk_warn.setStyleSheet(f"color: {P['warn']};")
        v.addWidget(self.hk_warn)
        QTimer.singleShot(1500, self._hk_check)

        # ---- controller
        c = Card("Controller", "Clip from an Xbox controller (or any controller Steam Input / DS4Windows shows as "
                               "one). Press all the buttons of a combo together. VR controllers aren't supported.")
        c.add_row("Controller shortcuts", "", toggle(self.cfg, "controller.enabled"))
        self.pad_lbls = {}
        for key, title in (("clip", "Save clip"), ("bookmark", "Bookmark"), ("screenshot", "Screenshot")):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel()
            lbl.setMinimumWidth(130)
            self.pad_lbls[key] = lbl
            setb = QPushButton("Set…")
            setb.clicked.connect(lambda _=False, k=key: self._pad_record(k))
            clr = QPushButton("Clear")
            clr.clicked.connect(lambda _=False, k=key: (self.cfg.set(f"controller.{k}", ""), self._pad_show()))
            h.addWidget(lbl)
            h.addWidget(setb)
            h.addWidget(clr)
            c.add_row(title, "", row)
        self.pad_status = QLabel()
        self.pad_status.setObjectName("faint")
        c.add(self.pad_status)
        v.addWidget(c)
        self._pad_show()
        self._pad_timer = QTimer(self, interval=1500, timeout=self._pad_show)
        self._pad_timer.start()
        return page

    def _pad_show(self):
        for k, lbl in getattr(self, "pad_lbls", {}).items():
            if getattr(self, "_pad_recording", None) == k:
                continue
            combo_ = self.cfg.get(f"controller.{k}", "")
            lbl.setText(combo_.replace("+", " + ") if combo_ else "Not set")
        pad = getattr(self.ctl, "pad", None)
        n = len(pad.connected) if pad else 0
        self.pad_status.setText(f"{n} controller{'s' if n != 1 else ''} connected" if n else
                                "No controller detected right now")

    def _pad_record(self, key):
        pad = getattr(self.ctl, "pad", None)
        if not pad or not pad.running:
            self.pad_lbls[key].setText("Controller support isn't available")
            return
        self._pad_recording = key
        self.pad_lbls[key].setText("Press the buttons…")

        def done(combo_):
            def apply():
                self._pad_recording = None
                if combo_:
                    self.cfg.set(f"controller.{key}", combo_)
                self._pad_show()
            W.ui_call(apply)
        pad.capture(done)

    def _hk_check(self):
        f = getattr(self.ctl.hotkeys, "failed", [])
        self.hk_warn.setText(f"⚠ Couldn't register: {', '.join(f)} — another app (Medal, Discord, GeForce/AMD overlay) "
                             "is probably using that key." if f else "")

    # ================================================================== Quality
    def _quality(self):
        page, v = page_scaffold("Quality", "Resolution, frame rate, bitrate and encoder. Recording is done on your GPU.", "film")
        grid = QGridLayout()
        grid.setSpacing(10)
        self.preset_btns = {}
        presets = list(QUALITY_PRESETS.items()) + [("custom", {"label": "Custom", "desc": "Your settings"})]
        for i, (k, p) in enumerate(presets):
            b = QPushButton()
            b.setCheckable(True)
            b.setMinimumHeight(70)
            extra = f" · {p['bitrate']} Mbps" if "bitrate" in p else ""
            b.setText(f"{p['label']}\n{p['desc']}{extra}")
            b.setStyleSheet(f"""QPushButton {{ text-align: left; padding: 10px 14px; border-radius: 12px; background: {P['panel']};
                color: {P['muted']}; font-weight: 500; }}
                QPushButton:checked {{ border: 2px solid {P['accent']}; color: {P['text']}; background: {P['accent_soft']}; }}""")
            b.clicked.connect(lambda _=False, k=k: self._apply_preset(k))
            grid.addWidget(b, 0, i)
            self.preset_btns[k] = b
        v.addLayout(grid)

        c = Card("Video")
        self.q_res = combo([(0, "Native (monitor resolution)"), (1440, "1440p"), (1080, "1080p"), (900, "900p"), (720, "720p")],
                           self.cfg.get("quality.height"), lambda d: self._qset("height", d))
        c.add_row("Resolution", "", self.q_res)
        self.q_fps = combo([(f, f"{f} FPS") for f in (30, 60, 90, 120, 144, 165, 240)], self.cfg.get("quality.fps"),
                           lambda d: self._qset("fps", d), 160)
        c.add_row("Frame rate", "", self.q_fps)
        self.q_codec = Segmented([("h264", "H.264"), ("hevc", "HEVC / H.265"), ("av1", "AV1")], self.cfg.get("quality.codec"))
        self.q_codec.changed.connect(lambda k: self._qset("codec", k))
        c.add_row("Codec", "H.264 plays everywhere (Discord embeds). HEVC = same quality at ~half the size. AV1 = smallest (RX 9000 / RTX 40+).",
                  self.q_codec)
        self.q_rc = Segmented([("vbr", "Variable bitrate"), ("cbr", "Constant bitrate"), ("cqp", "Constant quality")],
                              self.cfg.get("quality.rate_control"))
        self.q_rc.changed.connect(lambda k: (self._qset("rate_control", k), self._q_sync()))
        c.add_row("Rate control", "", self.q_rc)
        self.q_br = SliderValue(2, 150, self.cfg.get("quality.bitrate"), 1, "{:.0f} Mbps", 240)
        self.q_br.valueChanged.connect(lambda x: self._qset("bitrate", int(x)))
        self.q_br_row = c.add_row("Bitrate", "", self.q_br)
        self.q_cq = SliderValue(10, 35, self.cfg.get("quality.cq"), 1, "QP {:.0f}", 240)
        self.q_cq.valueChanged.connect(lambda x: self._qset("cq", int(x)))
        self.q_cq_row = c.add_row("Quality level", "Lower = better quality, bigger files. 18–22 looks great.", self.q_cq)
        self.q_size = QLabel()
        self.q_size.setObjectName("muted")
        c.add(self.q_size)
        v.addWidget(c)

        c = Card("Encoder")
        vendors = [("auto", "Automatic")] + [(k, ff.VENDOR_LABEL[k]) for k in ("amf", "nvenc", "qsv", "cpu")]
        self.q_enc = combo(vendors, self.cfg.get("quality.encoder"), lambda d: self._qset("encoder", d))
        c.add_row("Video encoder", "Automatic picks your GPU (AMD AMF on the RX 9070 XT).", self.q_enc)
        self.q_speed = Segmented([("speed", "Speed"), ("balanced", "Balanced"), ("quality", "Quality")], self.cfg.get("quality.speed"))
        self.q_speed.changed.connect(lambda k: self._qset("speed", k))
        c.add_row("Encoder preset", "Speed has the lowest in-game impact.", self.q_speed)
        c.add_row("GPU zero-copy", "Frames go straight from the screen to the encoder without touching the CPU (SDR, native resolution).",
                  toggle(self.cfg, "quality.zero_copy"))
        self.enc_status = QLabel("Checking encoders…")
        self.enc_status.setObjectName("muted")
        c.add(self.enc_status)
        v.addWidget(c)
        threading.Thread(target=self._probe_encoders, daemon=True).start()
        self._q_sync()
        return page

    def _probe_encoders(self):
        found = []
        for vend in ("amf", "nvenc", "qsv"):
            ok = [c for c in ("h264", "hevc", "av1") if ff.encoder_works(ff.CODEC_ENCODERS[vend][c])]
            if ok:
                found.append(f"{ff.VENDOR_LABEL[vend]}: {', '.join(x.upper() for x in ok)}")
        txt = "Detected: " + (" · ".join(found) if found else "no GPU encoder found — CPU encoding will be used")
        W.ui_call(lambda: self.enc_status.setText(txt))

    def _apply_preset(self, k):
        if k == "custom":
            self.cfg.set("quality.preset", "custom")
        else:
            self.cfg.apply_preset(k)
        self._q_sync(reload=True)

    def _qset(self, key, val):
        self.cfg.set(f"quality.{key}", val)
        if key in ("height", "fps", "bitrate", "codec") and self.cfg.get("quality.preset") != "custom":
            p = QUALITY_PRESETS.get(self.cfg.get("quality.preset"), {})
            if p.get(key) != val:
                self.cfg.set("quality.preset", "custom")
        self._q_sync()

    def _q_sync(self, reload=False):
        q = self.cfg.get("quality")
        for k, b in self.preset_btns.items():
            b.setChecked(k == q.get("preset"))
        if reload:
            for w, val in ((self.q_res, q["height"]), (self.q_fps, q["fps"])):
                w.blockSignals(True)
                w.setCurrentIndex(max(0, w.findData(val)))
                w.blockSignals(False)
            self.q_codec.set_value(q["codec"])
            self.q_br.set_value(q["bitrate"])
        cq = q.get("rate_control") == "cqp"
        self.q_br_row.setVisible(not cq)
        self.q_cq_row.setVisible(cq)
        if cq:
            self.q_size.setText("Size depends on the scene with constant quality (typically 15–60 MB per minute at 1440p60).")
        else:
            mb = q["bitrate"] * 60 / 8
            self.q_size.setText(f"≈ {mb:.0f} MB per minute of video · a 1-minute clip ≈ {mb:.0f} MB · 1 hour ≈ {mb * 60 / 1024:.1f} GB")

    # ================================================================== HDR
    def _hdr(self):
        page, v = page_scaffold("HDR", "Fixes washed-out / dark clips when Windows HDR is on — automatically, matched to your "
                                       "Windows SDR brightness.", "sun")
        self.hdr_status = QFrame()
        self.hdr_status.setObjectName("hint")
        sh = QHBoxLayout(self.hdr_status)
        sh.setContentsMargins(16, 12, 16, 12)
        self.hdr_status_lbl = QLabel("Checking display…")
        self.hdr_status_lbl.setWordWrap(True)
        sh.addWidget(self.hdr_status_lbl, 1)
        rb = QPushButton("Re-check")
        rb.clicked.connect(self._refresh_hdr_status)
        sh.addWidget(rb)
        v.addWidget(self.hdr_status)

        c = Card("When the display is in HDR")
        mode = Segmented([("tonemap", "Auto-fix to SDR (recommended)"), ("hdr10", "Keep HDR10"), ("windows", "Windows default")],
                         self.cfg.get("hdr.mode"))
        mode.changed.connect(lambda k: (self.cfg.set("hdr.mode", k), self._hdr_preview_soon()))
        c.add(mode)
        c.lay.addSpacing(6)
        m = QLabel("Auto-fix: clips look like your screen and play right everywhere (Discord, phones). "
                   "Keep HDR10: true HDR files for HDR TVs/YouTube (look washed out on SDR screens). "
                   "Windows default: what most recorders do — fastest, but dull or blown-out highlights.")
        m.setObjectName("muted")
        m.setWordWrap(True)
        c.add(m)
        c.lay.addSpacing(6)
        c.add_row("HDR detection", "", combo([("auto", "Automatic"), ("on", "Always treat as HDR"), ("off", "Never (SDR)")],
                                             self.cfg.get("hdr.detect"), lambda d: self.cfg.set("hdr.detect", d), 200))
        eng = Segmented([("gpu", "GPU (libplacebo)"), ("cpu", "CPU (exact)")], self.cfg.get("hdr.engine"))
        eng.changed.connect(lambda k: (self.cfg.set("hdr.engine", k), self._hdr_preview_soon()))
        c.add_row("Tone-mapping engine", "GPU: smart per-scene brightness, near-zero CPU. CPU: exact match to your Windows "
                  "SDR white (uses a few CPU cores at 1440p60).", eng)
        v.addWidget(c)

        c = Card("Brightness")
        aw = Toggle(self.cfg.get("hdr.auto_white"))
        aw.toggled.connect(lambda x: (self.cfg.set("hdr.auto_white", x), self.white_slider.setEnabled(not x), self._hdr_preview_soon()))
        c.add_row("Match Windows SDR brightness", "Reads the 'SDR content brightness' slider from Windows HDR settings so "
                  "UI and paper-white land exactly at 100% white.", aw)
        self.white_slider = SliderValue(80, 480, self.cfg.get("hdr.white_nits"), 1, "{:.0f} nits", 220)
        self.white_slider.setEnabled(not self.cfg.get("hdr.auto_white"))
        self.white_slider.valueChanged.connect(lambda x: (self.cfg.set("hdr.white_nits", int(x)), self._hdr_preview_soon()))
        c.add_row("Reference white (manual)", "Raise if clips look too bright, lower if too dark.", self.white_slider)
        c.add_row("Highlight curve", "", combo([("bt.2390", "Natural (BT.2390)"), ("spline", "Soft"), ("hable", "Filmic (punchy)"),
                                                ("reinhard", "Reinhard"), ("clip", "Hard clip")],
                                               self.cfg.get("hdr.curve"), lambda d: (self.cfg.set("hdr.curve", d), self._hdr_preview_soon()), 200))
        pd = Toggle(self.cfg.get("hdr.peak_detect"))
        pd.toggled.connect(lambda x: (self.cfg.set("hdr.peak_detect", x), self._hdr_preview_soon()))
        c.add_row("Per-scene auto brightness", "Adapts to each scene's brightest point (GPU engine).", pd)
        self.trims = {}
        for key, label, lo, hi, step, fmt in (("brightness", "Brightness", -0.3, 0.3, 0.01, "{:+.2f}"),
                                               ("contrast", "Contrast", 0.5, 1.5, 0.01, "{:.2f}×"),
                                               ("saturation", "Saturation", 0.5, 1.6, 0.01, "{:.2f}×"),
                                               ("gamma", "Gamma", 0.6, 1.6, 0.01, "{:.2f}")):
            s = SliderValue(lo, hi, self.cfg.get(f"hdr.{key}"), step, fmt, 220)
            s.valueChanged.connect(lambda x, key=key: (self.cfg.set(f"hdr.{key}", round(x, 3)), self._hdr_preview_soon()))
            self.trims[key] = s
            c.add_row(label, "", s)
        reset = QPushButton("Reset trims")
        reset.clicked.connect(self._hdr_reset)
        c.add(reset)
        v.addWidget(c)

        c = Card("Live preview", "Grab a frame of your screen (put a game up in HDR first), then tune until the right side looks "
                                 "like your monitor.")
        row = QHBoxLayout()
        grab = QPushButton(" Grab frame in 3 s")
        grab.setObjectName("primary")
        grab.setIcon(icon("camera", "#0b0b10"))
        grab.clicked.connect(self._hdr_grab)
        row.addWidget(grab)
        load = QPushButton("Use HDR screenshot file…")
        load.clicked.connect(self._hdr_load)
        row.addWidget(load)
        row.addStretch(1)
        self.prev_status = QLabel()
        self.prev_status.setObjectName("muted")
        row.addWidget(self.prev_status)
        c.lay.addLayout(row)
        c.lay.addSpacing(10)
        imgs = QHBoxLayout()
        self.prev_left, self.prev_right = QLabel(), QLabel()
        for lab, cap in ((self.prev_left, "Without fix (Windows default)"), (self.prev_right, "ClipVault auto-fix")):
            col = QVBoxLayout()
            t = QLabel(cap)
            t.setObjectName("muted")
            col.addWidget(t)
            lab.setMinimumSize(380, 214)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet(f"background: #000; border-radius: 8px; color: {P['faint']};")
            lab.setText("No frame yet")
            col.addWidget(lab)
            imgs.addLayout(col)
        c.lay.addLayout(imgs)
        v.addWidget(c)
        self._prev_dir = Path(tempfile.mkdtemp(prefix="clipvault_hdr_"))
        self._raw_png = self._prev_dir / "raw.png"
        self._hdr_timer = QTimer(self, singleShot=True, interval=250, timeout=self._hdr_render)
        self._hdr_busy = False
        return page

    def _refresh_hdr_status(self):
        def work():
            mons = getattr(self.ctl.recorder, "_mons", None) or ff.probe_monitors()
            idx = int(self.cfg.get("recording.monitor", 0))
            mon = next((m for m in mons if m["idx"] == idx), mons[0])
            st = hdrmod.display_state(ff.gdi_name_for_monitor(mon))
            gpu = ff.gpu_tonemap_ok()
            if st is None:
                txt = "Couldn't read display HDR state (not on Windows?). Detection set to automatic falls back to SDR."
            elif st.hdr_enabled:
                txt = (f"<b>HDR is ON</b> for {st.gdi_name} · Windows SDR content brightness ≈ <b>{st.sdr_white_nits:.0f} nits</b> · "
                       f"{st.bits_per_channel}-bit. Auto-fix will tone-map your clips to match.")
            else:
                txt = (f"HDR is off for {st.gdi_name}{' (supported)' if st.hdr_supported else ''}. Clips record as normal SDR — "
                       "nothing to fix.")
            if not gpu:
                txt += "<br>⚠ GPU tone-mapping (libplacebo/Vulkan) isn't available here — the CPU engine will be used."
            self._hdr_state = st
            W.ui_call(lambda: self.hdr_status_lbl.setText(txt))
        threading.Thread(target=work, daemon=True).start()

    def _hdr_reset(self):
        for k, d in (("brightness", 0.0), ("contrast", 1.0), ("saturation", 1.0), ("gamma", 1.0)):
            self.cfg.set(f"hdr.{k}", d)
            self.trims[k].set_value(d)
        self._hdr_preview_soon()

    def _hdr_grab(self):
        self.prev_status.setText("Switch to your game — grabbing in 3 s…")
        QTimer.singleShot(3000, self._hdr_do_grab)

    def _hdr_do_grab(self):
        self.prev_status.setText("Grabbing…")

        def work():
            mons = getattr(self.ctl.recorder, "_mons", None) or ff.probe_monitors()
            mon = next((m for m in mons if m["idx"] == int(self.cfg.get("recording.monitor", 0))), mons[0])
            r = ff.run(ff.grab_raw_hdr_command(mon, self._raw_png), timeout=20)
            ok = r.returncode == 0 and self._raw_png.exists()
            # what "Windows default" capture (8-bit DDA) looks like at the same moment
            left = self._prev_dir / "left.png"
            ff.run([ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"ddagrab=output_idx={mon['idx']}:framerate=5:output_fmt=8bit", "-vf",
                    "hwdownload,format=bgra,scale=960:-2,format=rgb24", "-frames:v", "1", str(left)], timeout=20)
            msg = "" if ok else ("Couldn't grab a 10-bit frame — is HDR on for this display? " + r.stderr[-200:])
            W.ui_call(lambda: self._after_grab(ok, msg, left))
        threading.Thread(target=work, daemon=True).start()

    def _after_grab(self, ok, msg, left: Path):
        self.prev_status.setText(msg or "Frame captured — adjust the sliders above.")
        if left.exists():
            self._set_prev(self.prev_left, left)
        if ok:
            self._hdr_render()

    def _hdr_load(self):
        f, _ = QFileDialog.getOpenFileName(self, "16-bit PQ PNG", str(Path.home()), "PNG (*.png)")
        if f:
            import shutil
            shutil.copy(f, self._raw_png)
            naive = self._prev_dir / "naive.png"
            ff.run(ff.preview_naive_command(self._raw_png, naive))
            self._set_prev(self.prev_left, naive)
            self._hdr_render()

    def _hdr_preview_soon(self):
        if self._raw_png.exists():
            self._hdr_timer.start()

    def _hdr_render(self):
        if self._hdr_busy or not self._raw_png.exists():
            if self._raw_png.exists():
                self._hdr_timer.start()
            return
        self._hdr_busy = True
        h = dict(self.cfg.get("hdr"))
        st = getattr(self, "_hdr_state", None)
        out = self._prev_dir / "fixed.png"
        mode = h.get("mode")

        def work():
            if mode == "tonemap":
                r = ff.run(ff.preview_tonemap_command(h, st, self._raw_png, out), timeout=30)
            else:
                r = ff.run(ff.preview_naive_command(self._raw_png, out), timeout=30)
            def done():
                self._hdr_busy = False
                if r.returncode == 0:
                    self._set_prev(self.prev_right, out)
                else:
                    self.prev_status.setText("Preview failed: " + r.stderr[-160:])
            W.ui_call(done)
        threading.Thread(target=work, daemon=True).start()

    def _set_prev(self, lab: QLabel, path: Path):
        pm = QPixmap(str(path))
        if not pm.isNull():
            lab.setPixmap(pm.scaled(lab.width(), lab.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ================================================================== Audio
    def _audio(self):
        page, v = page_scaffold("Audio & Mic", "Levels, input sensitivity, noise suppression and tracks.", "mic")
        devs = audiomod.list_devices()
        a = self.cfg.get("audio")

        # PC audio
        c = Card("Game / PC audio")
        c.add_row("Record PC audio", "Everything you hear (WASAPI loopback).", toggle(self.cfg, "audio.system_enabled"))
        c.add_row("Output device", "", combo([("", "Default output")] + [(d.name, d.name) for d in devs["loopback"]],
                                             a["system_device"], lambda d: self.cfg.set("audio.system_device", d), 280))
        sv = SliderValue(0, 200, a["system_volume"], 1, "{:.0f}%", 220)
        sv.valueChanged.connect(lambda x: self.cfg.set("audio.system_volume", int(x)))
        c.add_row("Volume", "", sv)
        self.sys_meter = W.LevelMeter()
        c.add_row("Level", "", self.sys_meter, stretch_control=True)
        v.addWidget(c)

        # Mic
        c = Card("Microphone")
        c.add_row("Record microphone", "", toggle(self.cfg, "audio.mic_enabled"))
        c.add_row("Input device", "", combo([("", "Default input")] + [(d.name, d.name) for d in devs["mic"]],
                                            a["mic_device"], lambda d: self.cfg.set("audio.mic_device", d), 280))
        mv = SliderValue(0, 300, a["mic_volume"], 1, "{:.0f}%", 220)
        mv.valueChanged.connect(lambda x: self.cfg.set("audio.mic_volume", int(x)))
        c.add_row("Mic volume", "Boost a quiet mic (applied before the sensitivity gate).", mv)
        c.add_row("Mono mic fix", "Use if your mic only comes through one ear.", toggle(self.cfg, "audio.mono_mic"))
        v.addWidget(c)

        # Input sensitivity
        c = Card("Input sensitivity",
                 "Only sounds louder than the line get recorded — so the person next to you, keyboard and fan noise stay out. "
                 "Drag the line, or let Calibrate find it.")
        c.add_row("Enable input sensitivity", "", toggle(self.cfg, "audio.gate_enabled"))
        autot = Toggle(a.get("gate_auto"))
        autot.toggled.connect(lambda x: (self.cfg.set("audio.gate_auto", x), setattr(self.sens, "auto", x), self.sens.update()))
        c.add_row("Automatically determine sensitivity", "Tracks your room's noise floor and sits the line just above it. "
                  "Turn off if the person next to you still gets through.", autot)
        c.lay.addSpacing(6)
        self.sens = W.SensitivityMeter(a.get("gate_threshold_db", -45))
        self.sens.auto = bool(a.get("gate_auto"))
        self.sens.thresholdChanged.connect(lambda x: (setattr(self.ctl.audio.gate_settings, "threshold_db", x)))
        self.sens.thresholdCommitted.connect(lambda x: self.cfg.set("audio.gate_threshold_db", round(x, 1)))
        c.add(self.sens)
        self.gate_state = QLabel()
        self.gate_state.setObjectName("muted")
        c.add(self.gate_state)
        lrow = QHBoxLayout()
        self.listen_btn = QPushButton(" Listen to my mic")
        self.listen_btn.setCheckable(True)
        self.listen_btn.setIcon(icon("volume"))
        self.listen_btn.setToolTip("Hear your processed mic in your headphones while tuning. Turns off when you leave this page "
                                   "(or after 2 minutes).")
        self.listen_btn.toggled.connect(lambda on: self.ctl.audio.start_monitor() if on else self.ctl.audio.stop_monitor())
        lrow.addWidget(self.listen_btn)
        lnote = QLabel("Temporary — use headphones, turns itself off")
        lnote.setObjectName("faint")
        lrow.addWidget(lnote)
        lrow.addStretch(1)
        c.lay.addSpacing(6)
        c.lay.addLayout(lrow)
        c.lay.addSpacing(10)
        # calibration
        cal = QFrame()
        cal.setObjectName("hint")
        cv = QVBoxLayout(cal)
        cv.setContentsMargins(14, 12, 14, 12)
        top = QHBoxLayout()
        cl = QVBoxLayout()
        ct = QLabel("Calibrate for a shared room")
        ct.setStyleSheet("font-weight: 700;")
        self.cal_text = QLabel("Step 1: you talk for 5 s. Step 2: you stay quiet while your wife talks (or the TV plays) for 5 s. "
                               "ClipVault puts the line between the two.")
        self.cal_text.setWordWrap(True)
        self.cal_text.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)
        self.cal_text.setMinimumHeight(40)
        cl.addWidget(ct)
        cl.addWidget(self.cal_text)
        top.addLayout(cl, 1)
        cv.addLayout(top)
        self.cal_btn = QPushButton("Calibrate")
        self.cal_btn.setObjectName("primary")
        self.cal_btn.setFixedWidth(160)
        self.cal_btn.clicked.connect(self._cal_start)
        cv.addSpacing(6)
        cv.addWidget(self.cal_btn)
        self.cal_prog = QProgressBar()
        self.cal_prog.setRange(0, 100)
        self.cal_prog.hide()
        cv.addWidget(self.cal_prog)
        c.add(cal)
        c.lay.addSpacing(6)
        hold = SliderValue(50, 1000, a.get("gate_hold_ms", 250), 10, "{:.0f} ms", 200)
        hold.valueChanged.connect(lambda x: self.cfg.set("audio.gate_hold_ms", int(x)))
        c.add_row("Hold", "How long the mic stays open after you stop talking (avoids chopping word endings).", hold)
        rel = SliderValue(20, 600, a.get("gate_release_ms", 120), 10, "{:.0f} ms", 200)
        rel.valueChanged.connect(lambda x: self.cfg.set("audio.gate_release_ms", int(x)))
        c.add_row("Release", "Fade-out speed when closing.", rel)
        v.addWidget(c)

        # Noise suppression & PTT
        c = Card("Cleanup")
        c.add_row("Noise suppression", "Removes steady background noise (fans, hum, PC whine) from the mic track.",
                  toggle(self.cfg, "audio.noise_suppression"))
        ns = SliderValue(-80, -20, a["noise_floor_db"], 1, "{:.0f} dB", 220)
        ns.valueChanged.connect(lambda x: self.cfg.set("audio.noise_floor_db", int(x)))
        c.add_row("Suppression strength", "Move right to remove more (too far can make your voice sound underwater).", ns)
        c.add_row("Push to talk", "Mic is only recorded while the key is held.", toggle(self.cfg, "audio.push_to_talk"))
        ptt = HotkeyEdit(a.get("ptt_key", ""))
        ptt.changed.connect(lambda k: self.cfg.set("audio.ptt_key", k))
        c.add_row("Push-to-talk key", "", ptt)
        v.addWidget(c)

        # Mic test
        c = Card("Mic test", "Records 5 seconds through everything above (volume, sensitivity, suppression), then plays it back.")
        row = QHBoxLayout()
        self.test_btn = QPushButton(" Record 5 s test")
        self.test_btn.setIcon(icon("mic"))
        self.test_btn.clicked.connect(self._mic_test)
        row.addWidget(self.test_btn)
        self.test_prog = QProgressBar()
        self.test_prog.setRange(0, 100)
        self.test_prog.setValue(0)
        row.addWidget(self.test_prog, 1)
        self.play_test = QPushButton(" Play back")
        self.play_test.setIcon(icon("play"))
        self.play_test.setEnabled(False)
        self.play_test.clicked.connect(self._play_test)
        row.addWidget(self.play_test)
        c.lay.addLayout(row)
        self.test_player = QMediaPlayer(self)
        self.test_out = W.audio_output(self)
        if hasattr(self.test_out, "deviceChanged"):
            self.test_player.setAudioOutput(self.test_out)
        v.addWidget(c)

        # Virtual mic
        c = Card("Use this mic everywhere (virtual mic)",
                 "Sends your processed mic (sensitivity + volume) into a virtual audio cable. Pick that cable as your mic in "
                 "Discord, games or OBS and they all get the same clean mic — like NVIDIA Broadcast. "
                 "This never plays to your speakers/headphones.")
        virt = [d.name for d in devs["output"] if audiomod.is_virtual_output(d.name)]
        self.pt_toggle = Toggle(bool(a.get("passthrough_enabled")) and bool(virt))
        self.pt_toggle.setEnabled(bool(virt))
        self.pt_toggle.toggled.connect(lambda x: self.cfg.set("audio.passthrough_enabled", x))
        c.add_row("Enable virtual mic output", "", self.pt_toggle)
        cur = a.get("passthrough_device") if a.get("passthrough_device") in virt else (next((n for n in virt if "cable input" in n.lower()), virt[0]) if virt else "")
        if cur != a.get("passthrough_device"):
            self.cfg.set("audio.passthrough_device", cur)
            if not virt:
                self.cfg.set("audio.passthrough_enabled", False)
        if virt:
            c.add_row("Send to", "Use 'CABLE Input'. Then in Discord set Input Device to 'CABLE Output' and turn Discord's own "
                      "input sensitivity / noise suppression off.",
                      combo([(n, n) for n in virt], cur, lambda d: self.cfg.set("audio.passthrough_device", d), 300))
        else:
            warn = QLabel("No virtual audio cable found. Install VB-Cable (free), restart ClipVault, and it will show up here.")
            warn.setObjectName("muted")
            warn.setWordWrap(True)
            c.add(warn)
        self.pt_status = QLabel()
        self.pt_status.setObjectName("muted")
        c.add(self.pt_status)
        get = QPushButton("Get VB-Cable (free)")
        get.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://vb-audio.com/Cable/")))
        c.add(get)
        v.addWidget(c)

        # tracks
        c = Card("Tracks & sync")
        c.add_row("Separate audio tracks", "Clips get Mixed + Game + Mic tracks so you can mute or rebalance your mic later.",
                  toggle(self.cfg, "audio.separate_tracks"))
        c.add_row("Audio bitrate", "", combo([(128, "128 kbps"), (192, "192 kbps"), (256, "256 kbps"), (320, "320 kbps")],
                                             a["bitrate_k"], lambda d: self.cfg.set("audio.bitrate_k", d), 160))
        so = SliderValue(-300, 300, a.get("sync_offset_ms", 0), 10, "{:+.0f} ms", 220)
        so.valueChanged.connect(lambda x: self.cfg.set("audio.sync_offset_ms", int(x)))
        c.add_row("Audio sync offset", "Only if audio is early/late vs video (positive = delay audio).", so)
        v.addWidget(c)
        self._cal = None
        return page

    def _tick_meters(self):
        au = self.ctl.audio
        if au.system:
            self.sys_meter.set_level(au.system.level_db)
        else:
            self.sys_meter.set_level(-90)
        if au.mic and au.mic.gate is not None:
            g = au.mic.gate
            self.sens.set_level(g.last_level, g.is_open)
            if self.sens.auto:
                self.sens.set_threshold(g.effective_threshold())
            if au.mic.error:
                self.gate_state.setText(f"⚠ Mic error: {au.mic.error}")
            else:
                self.gate_state.setText("● Transmitting" if g.is_open else "○ Muted (below the line)")
                self.gate_state.setStyleSheet(f"color: {P['ok'] if g.is_open else P['muted']};")
        else:
            self.sens.set_level(-90, False)
            self.gate_state.setText("Mic is off or unavailable")
        if self._cal:
            self._cal_tick()
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        if self._tick_n % 10 == 0:
            st = self.ctl.audio.passthrough_status
            self.pt_status.setText(f"Status: {st}")
            want = bool(self.cfg.get("audio.passthrough_enabled"))
            if self.pt_toggle.isChecked() != want:
                self.pt_toggle.blockSignals(True)
                self.pt_toggle.setChecked(want)
                self.pt_toggle.blockSignals(False)
            mon = self.ctl.audio.monitoring
            if self.listen_btn.isChecked() != mon:
                self.listen_btn.blockSignals(True)
                self.listen_btn.setChecked(mon)
                self.listen_btn.blockSignals(False)

    # -- calibration (sampled on the UI timer, no threads needed)
    def _cal_start(self):
        if not self.ctl.audio.mic:
            self.cal_text.setText("Turn on the microphone first.")
            return
        self._cal = {"step": 0, "t": 0, "voice": [], "room": []}
        self.cal_btn.setEnabled(False)
        self.cal_prog.show()
        self.sens.markers = {}
        self.cal_text.setText("<b>Get ready…</b> In 2 seconds, talk normally for 5 seconds (count to 20, read something).")

    def _cal_tick(self):
        cal = self._cal
        cal["t"] += 1
        g = self.ctl.audio.mic.gate if self.ctl.audio.mic else None
        lvl = g.last_level if g else -90
        frames = 5 * 30
        if cal["step"] == 0:  # 2 s get ready
            if cal["t"] >= 60:
                cal.update(step=1, t=0)
                self.cal_text.setText("<b>Step 1 · Talk now</b> — normal voice, normal distance from the mic.")
        elif cal["step"] == 1:
            cal["voice"].append(lvl)
            self.cal_prog.setValue(int(cal["t"] / frames * 100))
            if cal["t"] >= frames:
                cal.update(step=2, t=0)
                self.cal_text.setText("<b>Get ready…</b> Now stay quiet. Have your wife talk at her normal volume "
                                      "(or let the room be as loud as usual).")
        elif cal["step"] == 2:
            if cal["t"] >= 75:
                cal.update(step=3, t=0)
                self.cal_text.setText("<b>Step 2 · Stay quiet</b> — let the room / the other person talk.")
        elif cal["step"] == 3:
            cal["room"].append(lvl)
            self.cal_prog.setValue(int(cal["t"] / frames * 100))
            if cal["t"] >= frames:
                self._cal_finish()

    def _cal_finish(self):
        cal, self._cal = self._cal, None
        self.cal_btn.setEnabled(True)
        self.cal_prog.hide()
        res = audiomod.suggest_threshold(cal["voice"], cal["room"])
        if not res["ok"]:
            self.cal_text.setText("⚠ " + res["reason"])
            return
        self.sens.markers = {"You": res["you"], "Room": res["room"]}
        self.sens.set_threshold(res["threshold"])
        self.ctl.audio.gate_settings.threshold_db = res["threshold"]
        self.cfg.set("audio.gate_auto", False)
        self.cfg.set("audio.gate_threshold_db", res["threshold"])
        self.sens.auto = False
        msg = (f"<b>Done.</b> Your voice ≈ {res['you']:.0f} dB · room ≈ {res['room']:.0f} dB · "
               f"line set to <b>{res['threshold']:.0f} dB</b> ({res['gap']:.0f} dB gap).")
        if res.get("warn"):
            msg += f"<br>⚠ {res['warn']}"
        self.cal_text.setText(msg)

    def _mic_test(self):
        mic = self.ctl.audio.mic
        if not mic:
            return
        out = str(app_data_dir() / "mictest.wav")
        self.test_btn.setEnabled(False)
        self.play_test.setEnabled(False)
        prog = {"v": 0.0}

        def work():
            audiomod.MicTest(mic, 5.0, out).run(progress=lambda f: prog.__setitem__("v", f))
            # run the same noise suppression the recorder uses
            a = self.cfg.get("audio")
            final = out
            if a.get("noise_suppression"):
                final = str(app_data_dir() / "mictest_ns.wav")
                ff.run([ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-i", out, "-af",
                        f"afftdn=nf={int(a.get('noise_floor_db', -50))}:nr=18:tn=1", final], timeout=30)
            prog["v"] = 2
            prog["file"] = final

        threading.Thread(target=work, daemon=True).start()

        def poll():
            if prog["v"] >= 2:
                self.test_prog.setValue(100)
                self.test_btn.setEnabled(True)
                self.play_test.setEnabled(True)
                self._test_file = prog["file"]
                self._play_test()
                return
            self.test_prog.setValue(int(min(1.0, prog["v"]) * 100))
            QTimer.singleShot(50, poll)
        poll()

    def _play_test(self):
        if getattr(self, "_test_file", None):
            self.test_player.setSource(QUrl.fromLocalFile(self._test_file))
            self.test_player.play()

    # ================================================================== Storage
    def _storage(self):
        page, v = page_scaffold("Storage", "Where clips live and how much space they may use.", "storage")
        c = Card("Capture folder")
        row = QHBoxLayout()
        self.folder = QLineEdit(self.cfg.get("storage.folder"))
        self.folder.setReadOnly(True)
        row.addWidget(self.folder, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self._pick_folder)
        row.addWidget(b)
        o = QPushButton("Open")
        o.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.cfg.get("storage.folder"))))
        row.addWidget(o)
        c.lay.addLayout(row)
        c.lay.addSpacing(12)
        self.usage_bar = QWidget()
        self.usage_bar.setFixedHeight(12)
        self.usage_bar.paintEvent = self._paint_usage
        c.add(self.usage_bar)
        self.usage_lbl = QLabel()
        self.usage_lbl.setObjectName("muted")
        c.lay.addSpacing(6)
        c.add(self.usage_lbl)
        v.addWidget(c)

        c = Card("Space management")
        lim = QSpinBox()
        lim.setRange(0, 20000)
        lim.setSuffix(" GB")
        lim.setSpecialValueText("No limit")
        lim.setValue(int(self.cfg.get("storage.limit_gb")))
        lim.valueChanged.connect(lambda x: self.cfg.set("storage.limit_gb", x))
        c.add_row("Storage limit", "Counts ClipVault's own recordings only.", lim)
        c.add_row("Automatically delete oldest", "When over the limit, delete the oldest ClipVault recordings. Favorites "
                  "and linked recordings (Medal/NVIDIA/OBS) are never auto-deleted.",
                  toggle(self.cfg, "storage.auto_delete"))
        c.add_row("Only delete long recordings", "Never auto-delete clips.", toggle(self.cfg, "storage.only_delete_sessions"))
        td = QSpinBox()
        td.setRange(1, 365)
        td.setSuffix(" days")
        td.setValue(int(self.cfg.get("storage.trash_days")))
        td.valueChanged.connect(lambda x: self.cfg.set("storage.trash_days", x))
        c.add_row("Empty trash after", "", td)
        v.addWidget(c)

        # ---- linked recordings
        c = Card("Linked recordings",
                 "Your existing recordings — old Medal, NVIDIA, OBS captures — show up in the library so you can cut "
                 "clips from them. Files stay exactly where they are; nothing is moved or copied.")
        c.add_row("Include other videos in the capture folder",
                  "Everything already in your capture folder (e.g. your old Medal library) is linked automatically.",
                  toggle(self.cfg, "storage.scan_storage"))
        lbl = QLabel("Extra folders")
        lbl.setStyleSheet("font-weight: 600;")
        c.lay.addSpacing(8)
        c.add(lbl)
        self.linked_list = QListWidget()
        self.linked_list.setMinimumHeight(90)
        self.linked_list.setStyleSheet(f"QListWidget {{ background: {P['panel2']}; border: 1px solid {P['border']}; "
                                       f"border-radius: 8px; }}")
        for f in self.cfg.get("storage.linked_folders", []) or []:
            self.linked_list.addItem(f)
        c.lay.addSpacing(4)
        c.add(self.linked_list)
        row = QHBoxLayout()
        addf = QPushButton("Add folder…")
        addf.clicked.connect(lambda: self._add_linked(None))
        remf = QPushButton("Remove")
        remf.clicked.connect(self._remove_linked)
        scan = QPushButton(" Scan now")
        scan.setIcon(icon("refresh"))
        scan.clicked.connect(lambda: (self.ctl.scan_library(manual=True), self.scan_status.setText("Scanning…")))
        row.addWidget(addf)
        row.addWidget(remf)
        row.addStretch(1)
        row.addWidget(scan)
        c.lay.addSpacing(6)
        c.lay.addLayout(row)
        self.suggest_box = QHBoxLayout()
        c.lay.addSpacing(6)
        c.lay.addLayout(self.suggest_box)
        self._fill_suggestions()
        self.scan_status = QLabel()
        self.scan_status.setObjectName("muted")
        c.lay.addSpacing(6)
        c.add(self.scan_status)
        v.addWidget(c)
        return page

    def _suggested_folders(self) -> list[tuple[str, str]]:
        home = Path.home()
        cands = [("Medal", home / "Videos" / "Medal"), ("OBS", home / "Videos"),
                 ("NVIDIA", home / "Videos" / "NVIDIA"), ("AMD ReLive", home / "Videos" / "Radeon ReLive"),
                 ("Xbox Game Bar", home / "Videos" / "Captures")]
        have = {str(Path(f)).casefold() for f in self.cfg.get("storage.linked_folders", []) or []}
        have.add(str(self.ctl.cfg.storage).casefold())
        return [(n, str(p)) for n, p in cands if p.exists() and str(p).casefold() not in have]

    def _fill_suggestions(self):
        while self.suggest_box.count():
            w = self.suggest_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        for name, path in self._suggested_folders():
            b = QPushButton(f"+ {name}")
            b.setObjectName("chip")
            b.setToolTip(f"Link {path}")
            b.clicked.connect(lambda _=False, p=path: self._add_linked(p))
            self.suggest_box.addWidget(b)
        self.suggest_box.addStretch(1)

    def _add_linked(self, path: str | None):
        if path is None:
            path = QFileDialog.getExistingDirectory(self, "Link a folder of recordings", str(Path.home()))
        if not path:
            return
        lst = list(self.cfg.get("storage.linked_folders", []) or [])
        if any(Path(path) == Path(x) for x in lst) or Path(path) == self.ctl.cfg.storage:
            return
        lst.append(str(Path(path)))
        self.linked_list.addItem(str(Path(path)))
        self.cfg.set("storage.linked_folders", lst)  # triggers a scan
        self.scan_status.setText("Scanning…")
        self._fill_suggestions()

    def _remove_linked(self):
        for it in self.linked_list.selectedItems():
            path = it.text()
            self.cfg.set("storage.linked_folders", [x for x in self.cfg.get("storage.linked_folders", []) if x != path])
            self.ctl.library.unlink_folder(path)
            self.linked_list.takeItem(self.linked_list.row(it))
        self._fill_suggestions()

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Capture folder", self.cfg.get("storage.folder"))
        if d:
            self.cfg.set("storage.folder", d)
            self.folder.setText(d)
            self._refresh_storage()

    def _refresh_storage(self):
        def work():
            u = self.ctl.library.usage()
            W.ui_call(lambda: self._show_usage(u))
        threading.Thread(target=work, daemon=True).start()

    def _show_usage(self, u):
        self._usage = u
        if hasattr(self, "scan_status"):
            c = self.ctl.library.counts()
            self.scan_status.setText(f"{c.get('linked', 0)} linked recording(s) in the library · "
                                     f"{human_size(u.get('linked', 0))}" +
                                     (" · scanning…" if self.ctl.library.scanning else ""))
        k = u["by_kind"]
        self.usage_lbl.setText(
            f"Clips {human_size(k.get('clip', 0) + k.get('import', 0))} · Long recordings {human_size(k.get('session', 0))} · "
            f"Screenshots {human_size(k.get('screenshot', 0))} · Linked {human_size(k.get('linked', 0))} · "
            f"Replay buffer {human_size(u['buffer'])} · Free {human_size(u['disk']['free'])} of {human_size(u['disk']['total'])}")
        self.usage_bar.update()

    def _paint_usage(self, e):
        u = getattr(self, "_usage", None)
        p = QPainter(self.usage_bar)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.usage_bar.rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(P["panel3"]))
        p.drawRoundedRect(r, 6, 6)
        if not u or not u["disk"]["total"]:
            return
        tot = u["disk"]["total"]
        x = 0.0
        k = u["by_kind"]
        for val, col in ((k.get("clip", 0) + k.get("import", 0), P["accent"]), (k.get("session", 0), P["rec"]),
                         (k.get("screenshot", 0), P["accent2"]), (k.get("linked", 0), P["ok"]), (u["buffer"], P["warn"]),
                         (u["disk"]["used"] - u["library"] - u.get("linked", 0) - u["buffer"], P["faint"])):
            w = max(0.0, val / tot * r.width())
            p.setBrush(QColor(col))
            p.drawRect(int(x), 0, max(1, int(w)) if val > 0 else 0, r.height())
            x += w

    # ================================================================== Alerts
    def _alerts(self):
        page, v = page_scaffold("Sounds & alerts", "Audio cues so you know a clip saved without alt-tabbing.", "bell")
        c = Card("Play a sound when…")
        for key, label in (("clip", "A clip is saved"), ("bookmark", "A bookmark is added"), ("screenshot", "A screenshot is taken"),
                           ("session", "A long recording starts / stops")):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            t = QPushButton()
            t.setIcon(icon("play"))
            t.setToolTip("Test")
            snd = {"session": "start"}.get(key, key)
            t.clicked.connect(lambda _=False, s=snd: threading.Thread(target=self.ctl.alerts.play, args=(s, True), daemon=True).start())
            h.addWidget(t)
            h.addWidget(toggle(self.cfg, f"alerts.{key}"))
            c.add_row(label, "", row)
        v.addWidget(c)
        c = Card("Sound")
        st = Segmented([("chime", "Chime"), ("pop", "Pop"), ("click", "Click"), ("custom", "Custom file")], self.cfg.get("alerts.sound"))
        st.changed.connect(lambda k: (self.cfg.set("alerts.sound", k),
                                      threading.Thread(target=self.ctl.alerts.play, args=("clip", True), daemon=True).start()))
        self.snd_style = st
        c.add_row("Style", "", st)
        vol = SliderValue(0, 100, self.cfg.get("alerts.volume"), 1, "{:.0f}%", 220)
        self._vol_preview = QTimer(self, singleShot=True, interval=350,
                                   timeout=lambda: threading.Thread(target=self.ctl.alerts.play, args=("clip", True), daemon=True).start())
        vol.valueChanged.connect(lambda x: (self.cfg.set("alerts.volume", int(x)), self._vol_preview.start()))
        c.add_row("Volume", "", vol)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self.custom_snd = QLineEdit(self.cfg.get("alerts.custom_path"))
        self.custom_snd.setReadOnly(True)
        self.custom_snd.setMinimumWidth(240)
        bb = QPushButton("Browse…")

        def pick():
            f, _ = QFileDialog.getOpenFileName(self, "Clip sound", str(Path.home()),
                                               "Audio (*.wav *.mp3 *.ogg *.m4a *.flac *.opus *.aac *.wma *.webm);;All files (*)")
            if f:
                self.cfg.set("alerts.custom_path", f)
                self.custom_snd.setText(f)
                self.cfg.set("alerts.sound", "custom")
                self.snd_style.set_value("custom")
                threading.Thread(target=self.ctl.alerts.play, args=("clip", True), daemon=True).start()
        bb.clicked.connect(pick)
        h.addWidget(self.custom_snd)
        h.addWidget(bb)
        c.add_row("Custom clip/bookmark sound", "Any audio file (mp3, ogg, wav, m4a…). First 6 s are used. Volume above applies.", row)
        c.add_row("On-screen toast in ClipVault", "", toggle(self.cfg, "alerts.toast"))
        v.addWidget(c)
        c = Card("Pop-up over the game", "A small notice in the corner of the game when you clip, bookmark or take a "
                                         "screenshot. Never takes focus; clicks pass straight through it. Games in "
                                         "true exclusive fullscreen draw over it — you'll still hear the sound.")
        c.add_row("Show in-game pop-up", "", toggle(self.cfg, "app.overlay"))
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(combo([("top-right", "Top right"), ("top-left", "Top left"), ("bottom-right", "Bottom right"),
                           ("bottom-left", "Bottom left")], self.cfg.get("app.overlay_pos", "top-right"),
                          lambda k: self.cfg.set("app.overlay_pos", k), 170))
        tb = QPushButton("Test")
        tb.clicked.connect(lambda: self.window().show_overlay(
            {"kind": "clip", "title": "Clip saved", "sub": "Last 60 s · test", "screen": None}, force=True))
        h.addWidget(tb)
        c.add_row("Position", "", row)
        v.addWidget(c)
        return page

    # ================================================================== Appearance
    def _look(self):
        page, v = page_scaffold("Appearance", "Pick a color theme.", "color")
        c = Card("Theme")
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, (k, (label, a1, a2, bg, panel)) in enumerate(THEMES.items()):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(self.cfg.get("theme") == k)
            b.setMinimumHeight(64)
            b.setStyleSheet(f"""QPushButton {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {panel}, stop:0.55 {panel},
                stop:0.56 {a1}, stop:0.78 {a1}, stop:0.79 {a2}, stop:1 {a2}); color: #fff; border: 2px solid {P['border']};
                border-radius: 12px; text-align: left; padding-left: 14px; font-weight: 700; }}
                QPushButton:checked {{ border: 2px solid #fff; }}""")
            b.clicked.connect(lambda _=False, k=k: (self.cfg.set("theme", k), self.theme_changed.emit(k)))
            grid.addWidget(b, i // 3, i % 3)
        c.lay.addLayout(grid)
        v.addWidget(c)
        c = Card("Window")
        c.add_row("Close button minimizes to tray", "Keeps recording in the background.", toggle(self.cfg, "app.close_to_tray"))
        c.add_row("Start minimized", "", toggle(self.cfg, "app.start_minimized"))
        st = Toggle(self.cfg.get("app.launch_on_startup"))
        st.toggled.connect(self._set_startup)
        c.add_row("Launch when Windows starts", "", st)
        c.add_row("Hover to preview", "Move the mouse across a card to scrub through the video.", toggle(self.cfg, "app.hover_preview"))
        v.addWidget(c)
        return page

    def _set_startup(self, on):
        self.cfg.set("app.launch_on_startup", on)  # Controller writes the registry Run key

    # ================================================================== Advanced
    def _advanced(self):
        page, v = page_scaffold("Advanced", "Diagnostics and fine tuning.", "settings")
        c = Card("Capture internals")
        c.add_row("Segment length", "Buffer chunk size. Smaller = clips start closer to the moment; 2 s is a good balance.",
                  combo([(1, "1 s"), (2, "2 s"), (4, "4 s")], self.cfg.get("recording.segment_seconds"),
                        lambda d: self.cfg.set("recording.segment_seconds", d), 120))
        c.add_row("Keyframe interval", "Lower = more precise fast cuts in the editor, slightly bigger files.",
                  combo([(0.5, "0.5 s"), (1, "1 s"), (2, "2 s")], self.cfg.get("quality.keyframe_sec"),
                        lambda d: self.cfg.set("quality.keyframe_sec", d), 120))
        v.addWidget(c)
        c = Card("Diagnostics", "Share this if something isn't recording right.")
        self.diag = QPlainTextEdit()
        self.diag.setReadOnly(True)
        self.diag.setMinimumHeight(320)
        c.add(self.diag)
        row = QHBoxLayout()
        r = QPushButton("Refresh")
        r.clicked.connect(self._refresh_diag)
        cp = QPushButton("Copy")
        cp.clicked.connect(lambda: QApplication.clipboard().setText(self.diag.toPlainText()))
        lg = QPushButton("Open log")
        lg.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_data_dir() / "clipvault.log"))))
        row.addWidget(lg)
        od = QPushButton("Open settings folder")
        od.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_data_dir()))))
        row.addWidget(r)
        row.addWidget(cp)
        row.addWidget(od)
        row.addStretch(1)
        c.lay.addSpacing(8)
        c.lay.addLayout(row)
        v.addWidget(c)
        return page

    def _refresh_diag(self):
        enc = ", ".join(sorted(e for e in ff.available_encoders() if any(x in e for x in ("amf", "nvenc", "qsv", "264", "265", "av1"))))
        txt = (f"ffmpeg: {ff.ffmpeg_path()}\nEncoders: {enc}\nGPU tonemap (libplacebo+Vulkan): {ff.gpu_tonemap_ok()} · zscale: {ff.has_filter('zscale')}\n"
               f"Audio devices: {sum(len(x) for x in audiomod.list_devices().values())}\n\n" + self.ctl.recorder.diagnostics())
        self.diag.setPlainText(txt)
