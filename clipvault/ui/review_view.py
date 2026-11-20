"""Post-game review: shown right after a game closes — keep, delete or clip what you just recorded."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidgetItem, QFrame,
                               QMessageBox)

from . import widgets as W
from .widgets import P, icon
from .library_view import Grid, CARD_W, CARD_H, human_dur, human_size


class ReviewView(QWidget):
    open_item = Signal(dict)
    done = Signal()

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.data: dict | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 22, 28, 18)
        v.setSpacing(14)

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = QLabel("GG")
        self.title.setObjectName("h1")
        self.sub = QLabel()
        self.sub.setObjectName("muted")
        col.addWidget(self.title)
        col.addWidget(self.sub)
        head.addLayout(col, 1)
        done = QPushButton("Done")
        done.clicked.connect(self.done.emit)
        head.addWidget(done, 0, Qt.AlignTop)
        v.addLayout(head)

        # session card
        self.sess = QFrame()
        self.sess.setObjectName("card")
        sh = QHBoxLayout(self.sess)
        sh.setContentsMargins(14, 14, 18, 14)
        sh.setSpacing(18)
        self.thumb = QLabel()
        self.thumb.setFixedSize(320, 180)
        self.thumb.setStyleSheet("background: #000; border-radius: 10px;")
        self.thumb.setAlignment(Qt.AlignCenter)
        sh.addWidget(self.thumb)
        info = QVBoxLayout()
        info.setSpacing(6)
        k = QLabel("FULL SESSION")
        k.setStyleSheet(f"color: {P['rec']}; font-weight: 800; font-size: 8.5pt; letter-spacing: 1px;")
        info.addWidget(k)
        self.s_title = QLabel()
        self.s_title.setObjectName("h2")
        info.addWidget(self.s_title)
        self.s_meta = QLabel()
        self.s_meta.setObjectName("muted")
        self.s_meta.setWordWrap(True)
        info.addWidget(self.s_meta)
        info.addStretch(1)
        btns = QHBoxLayout()
        self.b_clip = QPushButton(" Open && make clips")
        self.b_clip.setObjectName("primary")
        self.b_clip.setIcon(icon("cut", "#0b0b10"))
        self.b_clip.clicked.connect(self._open_session)
        self.b_keep = QPushButton(" Keep")
        self.b_keep.setIcon(icon("check"))
        self.b_keep.clicked.connect(self._keep_session)
        self.b_fav = QPushButton(" Favorite")
        self.b_fav.setIcon(icon("star"))
        self.b_fav.clicked.connect(self._fav_session)
        self.b_del = QPushButton(" Delete session")
        self.b_del.setObjectName("danger")
        self.b_del.setIcon(icon("trash", P["danger"]))
        self.b_del.clicked.connect(self._delete_session)
        for b in (self.b_clip, self.b_keep, self.b_fav, self.b_del):
            btns.addWidget(b)
        btns.addStretch(1)
        info.addLayout(btns)
        sh.addLayout(info, 1)
        v.addWidget(self.sess)

        # clips
        ch = QHBoxLayout()
        self.c_title = QLabel("Clips & screenshots from this game")
        self.c_title.setObjectName("h2")
        ch.addWidget(self.c_title)
        ch.addStretch(1)
        self.b_del_sel = QPushButton(" Delete selected")
        self.b_del_sel.setIcon(icon("trash", P["danger"]))
        self.b_del_sel.clicked.connect(self._delete_selected)
        ch.addWidget(self.b_del_sel)
        self.b_del_all = QPushButton("Delete everything")
        self.b_del_all.setObjectName("danger")
        self.b_del_all.clicked.connect(self._delete_all)
        ch.addWidget(self.b_del_all)
        v.addLayout(ch)
        self.grid = Grid()
        self.grid.itemDoubleClicked.connect(lambda li: self.grid.item_dict(li) and self.open_item.emit(self.grid.item_dict(li)))
        v.addWidget(self.grid, 1)
        self.none = QLabel("No clips this time — the full session above has everything.")
        self.none.setObjectName("muted")
        v.addWidget(self.none)
        tip = QLabel("Deleted items go to Trash first. Double-click a clip to open it.")
        tip.setObjectName("faint")
        v.addWidget(tip)

    def load(self, data: dict):
        self.data = data
        lib = self.ctl.library
        dur = (data.get("end") or data["start"]) - data["start"]
        self.title.setText(f"GG — {data['game']}")
        self.sub.setText(f"{datetime.fromtimestamp(data['start']):%A %I:%M %p} · played {human_dur(dur)} · "
                         "decide what to keep while it's fresh")
        sess = lib.get(data.get("session")) if data.get("session") else None
        self.session = sess
        self.sess.setVisible(bool(sess))
        if sess:
            from ..core.library import nice_title
            self.s_title.setText(nice_title(sess))
            self.s_meta.setText(f"{human_dur(sess.get('duration', 0))} · {human_size(sess.get('size', 0))} · "
                                f"{len(sess.get('bookmarks') or [])} bookmark(s)\n{sess['path']}")
            pm = QPixmap(sess.get("thumb") or "")
            if not pm.isNull():
                self.thumb.setPixmap(pm.scaled(320, 180, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            else:
                self.thumb.setText("Processing…")
            self._sync_session_buttons()
        clips = [lib.get(i) for i in data["items"] if i != data.get("session")]
        clips = [c for c in clips if c and not c.get("trashed_at")]
        self.grid.set_items(clips)
        self.grid.setVisible(bool(clips))
        self.none.setVisible(not clips)
        self.b_del_sel.setVisible(bool(clips))
        self.b_del_all.setVisible(bool(clips) or bool(sess))

    def _sync_session_buttons(self):
        s = self.ctl.library.get(self.session["id"]) if self.session else None
        if not s:
            return
        self.b_fav.setText(" Favorited" if s.get("favorite") else " Favorite")
        self.b_fav.setIcon(icon("star_on" if s.get("favorite") else "star", "#ffd54a" if s.get("favorite") else None))

    def _open_session(self):
        if self.session:
            self.open_item.emit(self.ctl.library.get(self.session["id"]) or self.session)

    def _keep_session(self):
        self.window().toast("Kept", "Session is in your library under Long recordings")
        self.b_keep.setEnabled(False)

    def _fav_session(self):
        s = self.ctl.library.get(self.session["id"])
        if s:
            self.ctl.library.update(s["id"], favorite=not s.get("favorite"))
            self._sync_session_buttons()

    def _delete_session(self):
        if not self.session:
            return
        self.ctl.library.trash([self.session["id"]])
        self.sess.hide()
        self.window().toast("Session moved to trash", "Your clips were kept")

    def _delete_selected(self):
        ids = [li.data(Qt.UserRole) for li in self.grid.selectedItems()]
        if ids:
            self.ctl.library.trash(ids)
            rest = [self.grid.items_by_id[i] for i in self.grid._order if i not in ids and i in self.grid.items_by_id]
            self.grid.set_items(rest)
            self.grid.setVisible(bool(rest))
            self.none.setVisible(not rest)

    def _delete_all(self):
        ids = [self.grid.item(i).data(Qt.UserRole) for i in range(self.grid.count())]
        if self.session and self.sess.isVisible():
            ids.append(self.session["id"])
        if not ids:
            return
        if QMessageBox.question(self, "Delete everything", f"Move {len(ids)} item(s) from this game to trash?") == QMessageBox.Yes:
            self.ctl.library.trash(ids)
            self.done.emit()
