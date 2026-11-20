"""Main window: sidebar, status, hotkey bar, pages, tray."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, QSize, QRectF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction, QLinearGradient, QPainterPath
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QStackedWidget,
                               QSystemTrayIcon, QMenu, QApplication, QFrame, QProgressBar, QButtonGroup, QScrollArea,
                               QMessageBox)

from ..core.config import APP_NAME
from . import widgets as W
from .theme import palette, stylesheet
from .widgets import icon, kbd
from .library_view import LibraryView, human_size, human_dur
from .editor_view import EditorView
from .settings_view import SettingsView
from .review_view import ReviewView


def app_icon(p: dict, size=256) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    g = QPainter(pm)
    g.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor(p["accent"]))
    grad.setColorAt(1, QColor(p["accent2"]))
    path = QPainterPath()
    path.addRoundedRect(QRectF(size * .06, size * .06, size * .88, size * .88), size * .22, size * .22)
    g.fillPath(path, grad)
    # vault ring + play triangle
    g.setPen(Qt.NoPen)
    g.setBrush(QColor("#0b0b10"))
    g.drawEllipse(QRectF(size * .22, size * .22, size * .56, size * .56))
    tri = QPainterPath()
    tri.moveTo(size * .44, size * .37)
    tri.lineTo(size * .64, size * .5)
    tri.lineTo(size * .44, size * .63)
    tri.closeSubpath()
    g.fillPath(tri, QColor(p["accent"]))
    g.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, ctl, start_hidden=False):
        super().__init__()
        self.ctl = ctl
        self._overlay = None
        W.init_invoker()
        # native title bars (this window and every dialog) painted in the theme's colors: no seam at the top
        W.style_new_windows(QApplication.instance())
        self.setWindowTitle(APP_NAME)
        self.resize(1560, 940)
        self.setMinimumSize(1100, 680)
        self._quitting = False
        self._tray_hint_shown = False
        self._build()
        W.style_title_bar(self)
        self.tray = QSystemTrayIcon(app_icon(W.P), self)
        self._build_tray()
        self.tray.show()
        ctl.bridge.event.connect(self._on_event)
        self._hk_timer = QTimer(self, singleShot=True, interval=200, timeout=self._rebuild_topbar)
        ctl.cfg.on_change(lambda k, v: (k.startswith("hotkeys") or k == "recording.clip_alt_seconds") and W.ui_call(self._hk_timer.start))
        self._tick = QTimer(self, interval=1000, timeout=self._update_status)
        self._tick.start()
        self._lib_refresh = QTimer(self, singleShot=True, interval=600, timeout=self._refresh_library)
        self._update_status()

    # ------------------------------------------------------------------ build
    def _build(self, page: str = "library"):
        # Theme change = rebuild. Throw the old screen away *first* (nothing of it is on the call stack: the
        # theme click is deferred to here), so restyling the app only touches a near-empty window instead of
        # two full copies of the UI; and keep painting off until the new screen is ready (no flash/glitch).
        self.setUpdatesEnabled(False)
        old = self.takeCentralWidget()
        if old is not None:
            import shiboken6
            old.hide()
            shiboken6.delete(old)
        W.P.clear()
        W.P.update(palette(self.ctl.cfg.get("theme")))
        QApplication.instance().setStyleSheet(stylesheet(W.P))
        self.setWindowIcon(app_icon(W.P))
        root = QWidget()
        root.setObjectName("root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._sidebar())
        right = W.GlowPane()  # pages sit on a soft glow that's low in the window; the top bar stays plain
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        self._right_layout = rv
        self.topbar = self._topbar()
        rv.addWidget(self.topbar)
        self.stack = QStackedWidget()
        self.library = LibraryView(self.ctl)
        self.editor = EditorView(self.ctl)
        self.settings = SettingsView(self.ctl)
        self.review = ReviewView(self.ctl)
        for w in (self.library, self.editor, self.settings, self.review):
            self.stack.addWidget(w)
        self.library.open_item.connect(self.open_editor)
        self.editor.back.connect(lambda: self.show_page("library"))
        self.editor.open_item.connect(self.open_editor)
        self.review.open_item.connect(self.open_editor)
        self.review.done.connect(lambda: self.show_page("library"))
        self.settings.theme_changed.connect(self._retheme)
        rv.addWidget(self.stack, 1)
        h.addWidget(right, 1)
        self.setCentralWidget(root)
        self.toast_w = W.Toast(root)
        W.glow_primary_buttons(root)  # RGB Studio-style glow under the accent buttons
        self.show_page(page)
        self._refresh_counts()
        self.setUpdatesEnabled(True)

    def _retheme(self, key):
        # runs from the theme button's click: do the rebuild once that click has fully returned
        QTimer.singleShot(0, self._do_retheme)

    def _do_retheme(self):
        self.editor.shutdown()  # detach the video engine before its window is destroyed
        self.settings._hold_audio(False)
        self._build("settings")
        W.style_title_bar(self)
        self.settings.show_page("look")
        self.tray.setIcon(app_icon(W.P))

    def _sidebar(self) -> QWidget:
        P = W.P
        sb = QWidget()
        sb.setObjectName("sidebar")
        sb.setFixedWidth(262)
        v = QVBoxLayout(sb)
        v.setContentsMargins(14, 16, 14, 14)
        v.setSpacing(4)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon(P).pixmap(30, 30))
        brand.addWidget(logo)
        name = QLabel("ClipVault")
        name.setObjectName("brand")
        brand.addWidget(name)
        brand.addStretch(1)
        v.addLayout(brand)
        v.addSpacing(10)

        # status card
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 12, 14, 12)
        cv.setSpacing(6)
        row = QHBoxLayout()
        self.dot = QLabel("●")
        self.state_lbl = QLabel("Waiting for game")
        self.state_lbl.setStyleSheet("font-weight: 700;")
        row.addWidget(self.dot)
        row.addWidget(self.state_lbl, 1)
        cv.addLayout(row)
        grow = QHBoxLayout()
        self.game_lbl = QLabel()
        self.game_lbl.setObjectName("muted")
        grow.addWidget(self.game_lbl, 1)
        self.notgame_btn = QPushButton("Not a game")
        self.notgame_btn.setObjectName("ghost")
        self.notgame_btn.setToolTip("ClipVault mistook this app for a game — stop recording it and never detect it again")
        self.notgame_btn.clicked.connect(self._confirm_not_a_game)
        grow.addWidget(self.notgame_btn)
        cv.addLayout(grow)
        self.pipe_lbl = QLabel()
        self.pipe_lbl.setObjectName("faint")
        self.pipe_lbl.setWordWrap(True)
        cv.addWidget(self.pipe_lbl)
        btns = QHBoxLayout()
        self.buf_btn = QPushButton()
        self.buf_btn.clicked.connect(self.ctl.toggle_buffer)
        self.long_btn = QPushButton()
        self.long_btn.clicked.connect(self.ctl.recorder.toggle_long_recording)
        btns.addWidget(self.buf_btn, 1)
        btns.addWidget(self.long_btn, 1)
        cv.addLayout(btns)
        v.addWidget(card)
        v.addSpacing(6)

        sec = QLabel("LIBRARY")
        sec.setObjectName("section")
        v.addWidget(sec)
        self.nav_group = QButtonGroup(self)
        self.nav_btns = {}
        for key, label, g in (("all", "All media", "library"), ("clips", "Clips", "film"), ("sessions", "Long recordings", "rec"),
                              ("screenshots", "Screenshots", "image"), ("favorites", "Favorites", "star"),
                              ("bookmarked", "Bookmarked", "flag"), ("linked", "Linked", "folder"),
                              ("trash", "Trash", "trash")):
            b = QPushButton(f"  {label}")
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setIcon(icon(g, P["muted"]))
            b.setIconSize(QSize(16, 16))
            b.clicked.connect(lambda _=False, k=key: self._nav(k))
            self.nav_group.addButton(b)
            self.nav_btns[key] = b
            v.addWidget(b)
        # games + albums scroll if there are many
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        sw = QWidget()
        sv = QVBoxLayout(sw)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(2)
        sec = QLabel("GAMES")
        sec.setObjectName("section")
        sv.addWidget(sec)
        self.games_box = QVBoxLayout()
        self.games_box.setSpacing(2)
        sv.addLayout(self.games_box)
        sec2 = QLabel("ALBUMS")
        sec2.setObjectName("section")
        sv.addSpacing(6)
        sv.addWidget(sec2)
        self.albums_box = QVBoxLayout()
        self.albums_box.setSpacing(2)
        sv.addLayout(self.albums_box)
        new_album = QPushButton("  New album")
        new_album.setObjectName("nav")
        new_album.setIcon(icon("add", P["muted"]))
        new_album.clicked.connect(self._new_album)
        sv.addWidget(new_album)
        sv.addStretch(1)
        side_scroll.setWidget(sw)
        v.addWidget(side_scroll, 1)
        sb_set = QPushButton("  Settings")
        sb_set.setObjectName("nav")
        sb_set.setCheckable(True)
        sb_set.setIcon(icon("settings", P["muted"]))
        sb_set.clicked.connect(lambda: self.show_page("settings"))
        self.nav_group.addButton(sb_set)
        self.nav_btns["settings"] = sb_set
        v.addWidget(sb_set)
        v.addSpacing(6)
        st = QLabel("PC STORAGE")
        st.setObjectName("section")
        v.addWidget(st)
        self.storage_bar = QProgressBar()
        self.storage_bar.setFixedHeight(6)
        self.storage_bar.setRange(0, 1000)
        v.addWidget(self.storage_bar)
        self.storage_lbl = QLabel()
        self.storage_lbl.setObjectName("faint")
        v.addWidget(self.storage_lbl)
        return sb

    def _topbar(self) -> QWidget:
        tb = QWidget()
        tb.setObjectName("topbar")
        tb.setFixedHeight(54)
        h = QHBoxLayout(tb)
        h.setContentsMargins(22, 0, 22, 0)
        h.setSpacing(18)
        hk = self.ctl.cfg.get("hotkeys")
        rec = self.ctl.recorder
        alt_s = int(self.ctl.cfg.get("recording.clip_alt_seconds", 300))
        alt_lbl = f"{alt_s // 60} min clip" if alt_s >= 60 else f"{alt_s} s clip"
        for combo, label, fn in ((hk["clip"], "Clip", rec.clip),
                                 (hk.get("clip_alt", ""), alt_lbl, lambda: rec.clip(alt_s)),
                                 (hk["long_recording"], "Long recording", rec.toggle_long_recording),
                                 (hk["bookmark"], "Bookmark", rec.bookmark), (hk["screenshot"], "Screenshot", rec.screenshot)):
            if not combo:
                continue
            b = QPushButton()
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"{label} (click to trigger now)")
            bl = QHBoxLayout(b)
            bl.setContentsMargins(6, 0, 6, 0)
            bl.setSpacing(6)
            for part in combo.split("+"):
                bl.addWidget(kbd(part))
            t = QLabel(label)
            t.setObjectName("muted")
            bl.addWidget(t)
            b.setMinimumWidth(b.sizeHint().width() + 14 + len(label) * 7 + len(combo) * 7)
            b.setFixedHeight(34)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        self.mic_ind = QLabel()
        self.mic_ind.setObjectName("muted")
        h.addWidget(self.mic_ind)
        return tb

    def _rebuild_topbar(self):
        old = self.topbar
        self.topbar = self._topbar()
        self._right_layout.replaceWidget(old, self.topbar)
        old.deleteLater()

    def _build_tray(self):
        m = QMenu()
        m.addAction("Open ClipVault", self.bring_up)
        m.addSeparator()
        self.t_buf = m.addAction("Start buffer", self.ctl.toggle_buffer)
        self.t_long = m.addAction("Start long recording", self.ctl.recorder.toggle_long_recording)
        m.addAction("Save clip now", self.ctl.recorder.clip)
        m.addSeparator()
        m.addAction("Quit", self.quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self.bring_up() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)

    def _confirm_not_a_game(self):
        name = self.ctl.games.current
        if not name:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Not a game?")
        box.setText(f"Stop treating {name} as a game?")
        box.setInformativeText("ClipVault stops recording it now, skips the post-game review, and won't detect it "
                               "again. You can undo this in Settings → Recording → Games, under \"Never a game\".")
        yes = box.addButton("Not a game", QMessageBox.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)  # Enter = Cancel, so it can't happen by accident
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is yes and self.ctl.games.current == name:
            self.ctl.not_a_game()

    # ------------------------------------------------------------------ navigation
    def _nav(self, key):
        self.library.set_view(key)
        self.show_page("library")
        self.nav_btns[key].setChecked(True)

    def show_page(self, page):
        w = {"library": self.library, "editor": self.editor, "settings": self.settings, "review": self.review}[page]
        if page != "editor" and self.stack.currentWidget() is self.editor:
            self.editor.leave()  # stop + release the file (so it can be moved/deleted)
        self.stack.setCurrentWidget(w)
        if page == "settings":
            self.nav_btns["settings"].setChecked(True)
        elif page == "library":
            self.nav_btns.get(self.library.view, self.nav_btns["all"]).setChecked(True)
            self.library.refresh()
            QTimer.singleShot(0, self.library.repaint_grid)  # after the video surface is gone

    def _new_album(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New album", "Album name:")
        if ok and name.strip():
            name = self.ctl.library.create_album(name)
            self.library.set_view("album:" + name)
            self.show_page("library")
            self.toast("Album created", "Right-click clips (or tick several) → Add to album")

    def _album_ctx(self, btn, pos, name):
        from PySide6.QtWidgets import QInputDialog
        m = QMenu(self)

        def rename():
            new, ok = QInputDialog.getText(self, "Rename album", "Album name:", text=name)
            if ok and new.strip() and self.ctl.library.rename_album(name, new):
                if self.library.view == "album:" + name:
                    self.library.set_view("album:" + " ".join(new.split())[:60])

        def delete():
            if QMessageBox.question(self, "Delete album", f"Delete the album “{name}”?\n\nThe clips in it stay in "
                                    "your library — only the album goes.") == QMessageBox.Yes:
                self.ctl.library.delete_album(name)
                if self.library.view == "album:" + name:
                    self._nav("all")
        m.addAction("Rename…", rename)
        m.addAction("Delete album", delete)
        m.exec(btn.mapToGlobal(pos))

    def open_editor(self, item):
        self.ctl.library.mark_seen([item["id"]])
        if item["kind"] == "screenshot":
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(item["path"]))
            return
        self.show_page("editor")
        self.editor.load(item)

    def bring_up(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def toast(self, title, sub="", error=False):
        if self.ctl.cfg.get("alerts.toast", True) or error:
            self.toast_w.show_msg(title, sub, error)

    # ------------------------------------------------------------------ status
    def _update_status(self):
        P = W.P
        r = self.ctl.recorder
        if r.long:
            el = time.time() - r.long.start
            self.state_lbl.setText(f"Long recording  {human_dur(el)}")
            self.dot.setStyleSheet(f"color: {P['rec']};" if int(el) % 2 == 0 else f"color: {P['faint']};")
        elif r.state == "buffering":
            self.state_lbl.setText(f"Buffering · {int(min(r.buffered_seconds(), self.ctl.cfg.get('recording.buffer_seconds')))} s ready")
            self.dot.setStyleSheet(f"color: {P['ok']};")
        elif r.state == "starting":
            self.state_lbl.setText("Starting capture…")
            self.dot.setStyleSheet(f"color: {P['warn']};")
        elif r.state == "error":
            self.state_lbl.setText("Capture error")
            self.dot.setStyleSheet(f"color: {P['danger']};")
        else:
            mode = self.ctl.cfg.get("recording.mode")
            self.state_lbl.setText("Waiting for game" if mode == "auto" else "Not recording")
            self.dot.setStyleSheet(f"color: {P['faint']};")
        self.game_lbl.setText(f"🎮 {r.game}" if r.game else ("Desktop" if r.state == "buffering" else "Open a game to start"))
        self.pipe_lbl.setText(r.status_line if r.state == "buffering" else "")
        self.notgame_btn.setVisible(bool(self.ctl.games.current))
        buffering = r.state in ("buffering", "starting")
        self.buf_btn.setText("Stop" if buffering else "Start")
        self.buf_btn.setIcon(icon("stop" if buffering else "play"))
        self.long_btn.setText("Stop rec" if r.long else "Long rec")
        self.long_btn.setObjectName("rec" if r.long else "")
        self.long_btn.style().unpolish(self.long_btn)
        self.long_btn.style().polish(self.long_btn)
        self.t_buf.setText("Stop buffer" if buffering else "Start buffer")
        self.t_long.setText("Stop long recording" if r.long else "Start long recording")
        tip = f"ClipVault — {self.state_lbl.text()}" + (f" · {r.game}" if r.game else "")
        self.tray.setToolTip(tip)
        mic = self.ctl.audio.mic
        if mic and mic.gate is not None and r.state == "buffering":
            on = mic.gate.is_open
            self.mic_ind.setText("🎙 Mic: transmitting" if on else "🎙 Mic: gated")
            self.mic_ind.setStyleSheet(f"color: {P['ok'] if on else P['faint']};")
        else:
            self.mic_ind.setText("")

    def _refresh_counts(self):
        c = self.ctl.library.counts()
        labels = {"all": "All media", "clips": "Clips", "sessions": "Long recordings", "screenshots": "Screenshots",
                  "favorites": "Favorites", "bookmarked": "Bookmarked", "linked": "Linked", "trash": "Trash"}
        for k, l in labels.items():
            n = c.get(k, 0)
            self.nav_btns[k].setText(f"  {l}" + (f"   ·  {n}" if n else ""))
        while self.games_box.count():
            w = self.games_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        for g, n, key in self.ctl.library.games()[:8]:
            b = QPushButton()
            b.setObjectName("nav")
            # long names ("Call of Duty: Modern Warfare 2019") get an ellipsis so the count stays visible
            b.setText(f"  {b.fontMetrics().elidedText(g, Qt.ElideRight, 118)}   ·  {n}")
            b.setIcon(icon("game", W.P["muted"]))
            b.setToolTip(g)
            b.clicked.connect(lambda _=False, g=g, key=key: (self.library.set_view("all", key, g), self.show_page("library")))
            self.games_box.addWidget(b)
        while self.albums_box.count():
            w = self.albums_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        for name, n in self.ctl.library.albums():
            b = QPushButton()
            b.setObjectName("nav")
            b.setText(f"  {b.fontMetrics().elidedText(name, Qt.ElideRight, 118)}" + (f"   ·  {n}" if n else ""))
            b.setIcon(icon("folder", W.P["muted"]))
            b.setToolTip(name)
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, b=b, name=name: self._album_ctx(b, pos, name))
            b.clicked.connect(lambda _=False, name=name: (self.library.set_view("album:" + name), self.show_page("library")))
            self.albums_box.addWidget(b)
        u = self.ctl.library.usage()
        d = u["disk"]
        if d["total"]:
            self.storage_bar.setValue(int(d["used"] / d["total"] * 1000))
        linked = f" · {human_size(u['linked'])} linked" if u.get("linked") else ""
        self.storage_lbl.setText(f"{human_size(u['library'])} in ClipVault{linked} · {human_size(d['free'])} free")

    def _refresh_library(self):
        self._refresh_counts()
        if self.stack.currentWidget() is self.library:
            self.library.refresh()

    # ------------------------------------------------------------------ events
    def _on_event(self, name, payload):
        if name == "library_changed":
            if payload and self.stack.currentWidget() is self.library:
                self.library.update_item(payload)
            self._lib_refresh.start()
        elif name == "scan_state":
            self.library.set_scanning(bool(payload))
            if not payload and self.stack.currentWidget() is self.settings:
                self.settings._refresh_storage()
        elif name == "toast":
            self.toast(*payload)
        elif name == "overlay":
            self.show_overlay(payload)
        elif name == "error":
            self.toast("Something went wrong", str(payload)[:160], error=True)
            if not self.isVisible():
                self.tray.showMessage("ClipVault", str(payload)[:200], QSystemTrayIcon.Warning, 5000)
        elif name in ("state", "status", "game", "long_recording"):
            self._update_status()
        elif name == "review":
            self.review.load(payload)
            self.show_page("review")
            if payload.get("defer"):  # you're already in another game — don't steal focus
                self.tray.showMessage("ClipVault", f"{payload['game']} session ready to review",
                                      QSystemTrayIcon.Information, 4000)
            else:
                self.bring_up()

    # ------------------------------------------------------------------ window lifecycle
    def closeEvent(self, e):
        if not self._quitting and self.ctl.cfg.get("app.close_to_tray", True):
            e.ignore()
            self.editor.player.pause()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage("ClipVault is still running", "Recording continues in the tray. Right-click the icon to quit.",
                                      QSystemTrayIcon.Information, 3500)
            return
        self.quit()
        e.accept()

    def show_overlay(self, msg: dict, force: bool = False):
        """In-game pop-up — skipped while you're looking at ClipVault itself (the toast covers that)."""
        cfg = self.ctl.cfg
        if not force and (not cfg.get("app.overlay", True) or (self.isVisible() and self.isActiveWindow())):
            return
        if self._overlay is None:
            from .overlay import GameOverlay
            self._overlay = GameOverlay()
        self._overlay.show_msg(msg.get("kind", "clip"), msg.get("title", ""), msg.get("sub", ""), msg.get("screen"),
                               cfg.get("app.overlay_pos", "top-right"))

    def quit(self):
        """Hide right away, finish saving any session/clip in the background, then exit."""
        if self._quitting:
            return
        self._quitting = True
        if self._overlay is not None:
            self._overlay.close()
        busy = self.ctl.recorder.long or self.ctl.recorder.busy()
        if busy:
            self.tray.showMessage("ClipVault", "Saving your recording before closing…", QSystemTrayIcon.Information, 4000)
        self.editor.shutdown()
        self.hide()
        import threading
        done = threading.Event()
        threading.Thread(target=lambda: (self.ctl.shutdown(), done.set()), daemon=True).start()

        def poll():
            if done.is_set():
                self.tray.hide()
                QApplication.instance().quit()
            else:
                QTimer.singleShot(200, poll)
        poll()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast_w.isVisible():
            self.toast_w.move(self.centralWidget().width() - self.toast_w.width() - 24,
                              self.centralWidget().height() - self.toast_w.height() - 24)
