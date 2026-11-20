"""Library grid: clips, long recordings, screenshots — ClipVault's own and linked ones."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize, QRectF, QUrl, QMimeData, QPoint, QItemSelectionModel
from PySide6.QtGui import QPainter, QColor, QPixmap, QFont, QPainterPath, QDesktopServices, QFontMetrics, QPen
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton,
                               QListWidget, QListWidgetItem, QStyledItemDelegate, QStyle, QMenu, QListView,
                               QFileDialog, QInputDialog, QMessageBox, QApplication, QAbstractItemView)

from ..core.library import nice_title
from . import widgets as W
from .widgets import P, icon
from .theme import _mix as mix

CARD_W, CARD_H, THUMB_H = 300, 236, 169
SOURCE_LABEL = {"medal": "MEDAL", "nvidia": "NVIDIA", "obs": "OBS"}


def human_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024
    return "?"


def human_dur(s: float) -> str:
    s = int(s or 0)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def human_when(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    delta = time.time() - ts
    if 0 <= delta < 3600:
        return f"{max(1, int(delta // 60))} min ago"
    if 0 <= delta < 86400 and d.date() == datetime.now().date():
        return d.strftime("Today %I:%M %p").replace(" 0", " ")
    if 0 <= delta < 6 * 86400:
        return d.strftime("%A")
    return d.strftime("%b %d, %Y")


def reveal(path: str):
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


_PIX_CACHE: dict = {}  # decoded thumbnails, keyed by file path


class Delegate(QStyledItemDelegate):
    def __init__(self, view: "Grid"):
        super().__init__(view)
        self.view = view
        self.cache: dict[str, QPixmap] = _PIX_CACHE  # shared: survives a theme rebuild

    def sizeHint(self, opt, idx):
        s = idx.data(Qt.SizeHintRole)
        return s if isinstance(s, QSize) else QSize(CARD_W, CARD_H)

    def paint_header(self, p: QPainter, opt, h: dict):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(8, 8, -8, -2)
        hov = bool(opt.state & QStyle.State_MouseOver)
        # chevron: ▸ folded, ▾ open
        cx, cy = r.left() + 7, r.center().y()
        pen = QPen(QColor(P["text"] if hov else P["muted"]), 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        ch = QPainterPath()
        if h.get("collapsed"):
            ch.moveTo(cx - 2.5, cy - 5)
            ch.lineTo(cx + 2.5, cy)
            ch.lineTo(cx - 2.5, cy + 5)
        else:
            ch.moveTo(cx - 5, cy - 2.5)
            ch.lineTo(cx, cy + 2.5)
            ch.lineTo(cx + 5, cy - 2.5)
        p.drawPath(ch)
        r.setLeft(r.left() + 22)
        f = QFont(opt.font)
        f.setPointSizeF(12.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(P["text"]))
        fm = QFontMetrics(f)
        p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, h["title"])
        x = r.left() + fm.horizontalAdvance(h["title"]) + 12
        if h.get("sub"):
            f2 = QFont(opt.font)
            f2.setPointSizeF(9)
            p.setFont(f2)
            p.setPen(QColor(P["muted"]))
            p.drawText(QRectF(x, r.top(), r.width(), r.height()), Qt.AlignLeft | Qt.AlignVCenter, h["sub"])
            x += QFontMetrics(f2).horizontalAdvance(h["sub"]) + 16
        if h.get("action"):
            f3 = QFont(opt.font)
            f3.setPointSizeF(9)
            f3.setBold(True)
            p.setFont(f3)
            p.setPen(QColor(P["accent"]))
            text = "✓  Mark as seen"
            ar = QRectF(x, r.top(), QFontMetrics(f3).horizontalAdvance(text) + 8, r.height())
            p.drawText(ar, Qt.AlignLeft | Qt.AlignVCenter, text)
            self.view.header_action_rects[h["id"]] = ar.toRect()
        p.restore()

    def strip(self, item_id):
        path = str(self.view.strip_dir / f"{item_id}.strip.jpg") if self.view.strip_dir else None
        if not path or not os.path.exists(path):
            return None
        return self.pix(path, scale=False)

    def pix(self, path, scale=True):
        if not path:
            return None
        pm = self.cache.get(path)
        if pm is None:
            pm = QPixmap(path)
            if pm.isNull():
                return None
            if scale and pm.width() > 480:
                pm = pm.scaledToWidth(480, Qt.SmoothTransformation)
            if len(self.cache) > 300:  # keep memory bounded on big libraries
                self.cache.clear()
            self.cache[path] = pm
        return pm

    def paint(self, p: QPainter, opt, idx):
        key = idx.data(Qt.UserRole)
        h = self.view.headers.get(key)
        if h:
            self.paint_header(p, opt, h)
            return
        it = self.view.items_by_id.get(key)
        if not it:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(6, 6, -6, -6)
        sel = bool(opt.state & QStyle.State_Selected)
        hov = bool(opt.state & QStyle.State_MouseOver)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        # lighter card on the dark page (Medal-style): the card itself is the surface, no outline unless hovered/selected
        p.fillPath(path, QColor(mix(P["panel"], "#ffffff", 0.085 if hov else 0.06)))
        if sel:
            p.setPen(QPen(QColor(P["accent"]), 2))
            p.drawPath(path)
        tr = QRectF(r.left(), r.top(), r.width(), THUMB_H - 6)
        tp = QPainterPath()
        tp.addRoundedRect(tr, 10, 10)
        tp.addRect(QRectF(tr.left(), tr.bottom() - 10, tr.width(), 10))
        p.save()
        p.setClipPath(tp.simplified())
        p.fillRect(tr, QColor(mix(P["panel"], "#ffffff", 0.03)))
        pm = self.pix(it.get("thumb"))
        scrub = self.view.scrub if self.view.scrub and self.view.scrub[0] == it["id"] else None
        sp = self.strip(it["id"]) if scrub else None
        if sp is not None:  # hover-scrub: show the frame under the mouse
            n = max(1, sp.width() // 320)
            k = min(n - 1, int(scrub[1] * n))
            p.drawPixmap(tr.toRect(), sp, QRectF(k * 320, 0, 320, 180).toRect())
            p.fillRect(QRectF(tr.left(), tr.bottom() - 3, tr.width() * scrub[1], 3), QColor(P["accent"]))
        elif pm:
            s = pm.size().scaled(tr.size().toSize(), Qt.KeepAspectRatioByExpanding)
            x = tr.left() + (tr.width() - s.width()) / 2
            y = tr.top() + (tr.height() - s.height()) / 2
            p.drawPixmap(QRectF(x, y, s.width(), s.height()).toRect(), pm)
        else:
            p.setPen(QColor(P["faint"]))
            p.drawText(tr, Qt.AlignCenter, "Processing…")
        if sel:
            tint = QColor(P["accent"])
            tint.setAlpha(38)
            p.fillRect(tr, tint)
        p.restore()
        f = QFont(opt.font)
        f.setPointSizeF(7.5)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)

        def badge(text, x, y, bg, fg="#fff", right=False):
            w = fm.horizontalAdvance(text) + 12
            rr = QRectF(x - w if right else x, y, w, 18)
            pp = QPainterPath()
            pp.addRoundedRect(rr, 5, 5)
            p.fillPath(pp, QColor(bg))
            p.setPen(QColor(fg))
            p.drawText(rr, Qt.AlignCenter, text)
            return rr
        # bottom-right: length · top-right: type (+HDR) · bottom-left: bookmarks — the top-left is the select circle
        if it["kind"] != "screenshot" and it.get("duration"):
            badge(human_dur(it.get("duration", 0)), tr.right() - 8, tr.bottom() - 26, QColor(0, 0, 0, 170), right=True)
        kind_txt = {"clip": "CLIP", "session": "LONG REC", "screenshot": "SHOT", "import": "CLIP"}.get(it["kind"], "")
        kc = {"session": P["rec"], "screenshot": P["accent2"]}.get(it["kind"], P["accent"])
        rr = badge(kind_txt, tr.right() - 8, tr.top() + 8, QColor(kc), "#0b0b10", right=True)
        if it.get("hdr") in ("tonemap", "hdr10"):
            badge("HDR" if it["hdr"] == "hdr10" else "HDR→SDR", rr.left() - 6, tr.top() + 8, QColor(0, 0, 0, 170),
                  right=True)
        if it.get("bookmarks"):
            badge(f"⚑ {len(it['bookmarks'])}", tr.left() + 8, tr.bottom() - 26, QColor(0, 0, 0, 170))
        # select circle: on hover, in select mode, or when selected
        if hov or sel or self.view.check_mode:
            c = self.view.circle_rect(opt.rect)
            if sel:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(P["accent"]))
                p.drawEllipse(c)
                pen = QPen(QColor("#0b0b10"), 2.4)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                ck = QPainterPath()
                ck.moveTo(c.left() + c.width() * 0.28, c.top() + c.height() * 0.52)
                ck.lineTo(c.left() + c.width() * 0.44, c.top() + c.height() * 0.68)
                ck.lineTo(c.left() + c.width() * 0.73, c.top() + c.height() * 0.34)
                p.setBrush(Qt.NoBrush)
                p.drawPath(ck)
            else:
                p.setBrush(QColor(0, 0, 0, 90))
                p.setPen(QPen(QColor(255, 255, 255, 235), 2))
                p.drawEllipse(c)
        # text: title (★ if favorite) + one quiet meta line, no boxes behind the text
        f2 = QFont(opt.font)
        f2.setPointSizeF(10.5)
        f2.setBold(True)
        p.setFont(f2)
        title = nice_title(it)
        tt = QRectF(r.left() + 12, tr.bottom() + 9, r.width() - 24, 22)
        if it.get("seen") is False:  # new since you last looked
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(P["accent"]))
            p.drawEllipse(QRectF(tt.left(), tt.center().y() - 4, 8, 8))
            tt.setLeft(tt.left() + 14)
        if it.get("favorite"):
            p.setPen(QColor("#ffd54a"))
            p.drawText(tt, Qt.AlignLeft | Qt.AlignVCenter, "★")
            tt.setLeft(tt.left() + QFontMetrics(f2).horizontalAdvance("★ "))
        p.setPen(QColor(P["text"]))
        p.drawText(tt, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f2).elidedText(title, Qt.ElideRight, int(tt.width())))
        f3 = QFont(opt.font)
        f3.setPointSizeF(8.5)
        p.setFont(f3)
        p.setPen(QColor(P["muted"]))
        src = {"medal": "Medal", "nvidia": "NVIDIA", "obs": "OBS"}.get(it.get("source") or "", "Linked") \
            if it.get("linked") else ""
        parts = [src, it.get("game", ""), human_size(it.get("size", 0)),
                 human_when(it["created"])]
        meta = "  ·  ".join(x for x in parts if x)
        mr = QRectF(r.left() + 12, tt.bottom() + 1, r.width() - 24, 18)
        p.drawText(mr, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f3).elidedText(meta, Qt.ElideRight, int(mr.width())))
        p.restore()


class Grid(QListWidget):
    """Card grid. Items carry only an id; the dicts live in items_by_id (fast paints, no data copies)."""

    def __init__(self):
        super().__init__()
        self.items_by_id: dict[str, dict] = {}
        self._order: list[str] = []
        self.check_mode = False   # Medal-style: after ticking a circle, clicks tick/untick cards
        self._swallow_release = False
        self.headers: dict[str, dict] = {}            # section rows ("Last session", "Today", …)
        self.header_action_rects: dict[str, object] = {}
        self.on_header_action = None
        self.on_header_toggle = None
        self.scrub = None          # (item id, 0..1) while the mouse moves over a thumbnail
        self.strip_dir = None
        self.on_scrub = None
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(False)
        self.setSpacing(4)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.delegate = Delegate(self)
        self.setItemDelegate(self.delegate)
        self.setStyleSheet("QListWidget::item, QListWidget::item:selected, QListWidget::item:hover { background: transparent; }")

    def _header_size(self) -> QSize:
        return QSize(max(CARD_W, self.viewport().width() - 14), 46)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.headers:
            hs = self._header_size()
            for n in range(self.count()):
                li = self.item(n)
                if li.data(Qt.UserRole) in self.headers:
                    li.setSizeHint(hs)
            self.doItemsLayout()

    def set_items(self, items: list[dict]):
        """Show items (and section headers); when nothing moved just refresh data (keeps scroll + selection)."""
        ids = [i["id"] for i in items]
        self.items_by_id = {i["id"]: i for i in items if not i.get("_hdr")}
        self.headers = {i["id"]: i for i in items if i.get("_hdr")}
        if ids == self._order:
            self.viewport().update()
            return
        sel = {li.data(Qt.UserRole) for li in self.selectedItems()}
        scroll = self.verticalScrollBar().value()
        self.setUpdatesEnabled(False)
        try:
            self.clear()
            hs = self._header_size()
            for it in items:
                li = QListWidgetItem()
                li.setData(Qt.UserRole, it["id"])
                if it.get("_hdr"):
                    li.setFlags(Qt.ItemIsEnabled)  # not selectable / draggable
                    li.setSizeHint(hs)
                else:
                    li.setSizeHint(QSize(CARD_W, CARD_H))
                    li.setToolTip(it["path"])
                self.addItem(li)
                if it["id"] in sel:
                    li.setSelected(True)
            self._order = ids
        finally:
            self.setUpdatesEnabled(True)
        self.doItemsLayout()
        self.verticalScrollBar().setValue(scroll)

    @staticmethod
    def circle_rect(rect) -> QRectF:
        r = QRectF(rect).adjusted(6, 6, -6, -6)
        return QRectF(r.left() + 9, r.top() + 9, 22, 22)

    @staticmethod
    def thumb_rect(rect) -> QRectF:
        r = QRectF(rect).adjusted(6, 6, -6, -6)
        return QRectF(r.left(), r.top(), r.width(), THUMB_H - 6)

    def mouseMoveEvent(self, e):
        # a press we handled ourselves (circle tick) must not reach Qt: it would treat the move as a drag-select
        # from an old press point and highlight every card in between
        if not (self._swallow_release and e.buttons()):
            super().mouseMoveEvent(e)
        pos = e.position()
        li = self.itemAt(pos.toPoint())
        new = None
        if li and li.data(Qt.UserRole) in self.items_by_id and not e.buttons():
            tr = self.thumb_rect(self.visualItemRect(li))
            if tr.contains(pos):
                new = (li.data(Qt.UserRole), max(0.0, min(0.999, (pos.x() - tr.left()) / tr.width())))
        old = self.scrub
        self.scrub = new
        if new and (not old or old[0] != new[0]) and self.on_scrub:
            self.on_scrub(new[0])
        if new != old:
            self.viewport().update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        if self.scrub:
            self.scrub = None
            self.viewport().update()

    def _toggle_at(self, e) -> bool:
        """Click on a card's circle (or anywhere on a card in select mode) ticks/unticks it."""
        if e.button() != Qt.LeftButton or e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
            return False
        pos = e.position()
        li = self.itemAt(pos.toPoint())
        if not li:
            return False
        if li.data(Qt.UserRole) in self.headers:
            hid = li.data(Qt.UserRole)
            ar = self.header_action_rects.get(hid)
            if ar is not None and ar.contains(pos.toPoint()) and self.on_header_action:
                self.on_header_action(hid)
            elif self.on_header_toggle:
                self.on_header_toggle(hid)
            return True
        if not (self.check_mode or self.circle_rect(self.visualItemRect(li)).adjusted(-6, -6, 6, 6).contains(pos)):
            return False
        self.check_mode = True
        li.setSelected(not li.isSelected())
        self.setCurrentItem(li, QItemSelectionModel.NoUpdate)
        if not self.selectedItems():
            self.set_check_mode(False)
        self.viewport().update()
        return True

    def set_check_mode(self, on: bool):
        if self.check_mode != on:
            self.check_mode = on
            self.viewport().update()
            self.itemSelectionChanged.emit()

    def mousePressEvent(self, e):
        if self._toggle_at(e):
            self._swallow_release = True
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self._toggle_at(e):  # second click of a fast double-click: tick/untick again, don't open
            self._swallow_release = True
            return
        super().mouseDoubleClickEvent(e)

    def mouseReleaseEvent(self, e):
        if self._swallow_release:
            self._swallow_release = False
            return
        super().mouseReleaseEvent(e)

    def item_dict(self, li) -> dict | None:
        return self.items_by_id.get(li.data(Qt.UserRole)) if li else None

    def update_item(self, it: dict) -> bool:
        if it and it["id"] in self.items_by_id:
            self.items_by_id[it["id"]] = it
            if it.get("thumb"):
                self.delegate.cache.pop(it["thumb"], None)
            self.viewport().update()
            return True
        return False

    def mimeData(self, items):
        """Drag a card straight into Discord / Explorer."""
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(self.item_dict(i)["path"]) for i in items if self.item_dict(i)])
        return md


class LibraryView(QWidget):
    open_item = Signal(dict)

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.view = "all"
        self.game_filter = None  # game key
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 18, 24, 0)
        v.setSpacing(12)

        head = QHBoxLayout()
        self.title = QLabel("All media")
        self.title.setObjectName("h1")
        head.addWidget(self.title)
        self.count = QLabel()
        self.count.setObjectName("pill")
        head.addWidget(self.count, 0, Qt.AlignVCenter)
        self.scan_pill = QLabel("Linking your recordings…")
        self.scan_pill.setObjectName("pill")
        self.scan_pill.hide()
        head.addWidget(self.scan_pill, 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search clips, games…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(220)
        self.search.addAction(icon("search", P.get("muted")), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self.refresh)
        head.addWidget(self.search)
        self.game = QComboBox()
        self.game.setMinimumWidth(170)
        self.game.currentIndexChanged.connect(self._game_changed)
        head.addWidget(self.game)
        self.source = QComboBox()
        for k, l in (("all", "All sources"), ("clipvault", "ClipVault"), ("linked", "Linked / imported")):
            self.source.addItem(l, k)
        self.source.currentIndexChanged.connect(self.refresh)
        head.addWidget(self.source)
        self.sort = QComboBox()
        for k, l in (("newest", "Newest first"), ("oldest", "Oldest first"), ("largest", "Largest"), ("longest", "Longest")):
            self.sort.addItem(l, k)
        self.sort.currentIndexChanged.connect(self.refresh)
        head.addWidget(self.sort)
        imp = QPushButton(" Import")
        imp.setIcon(icon("add"))
        imp.setToolTip("Add videos or screenshots from anywhere (they stay where they are)")
        imp.clicked.connect(self._import)
        head.addWidget(imp)
        fold = QPushButton()
        fold.setIcon(icon("folder"))
        fold.setToolTip("Open capture folder")
        fold.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ctl.cfg.storage))))
        head.addWidget(fold)
        v.addLayout(head)

        self.trash_bar = QHBoxLayout()
        self.trash_note = QLabel()
        self.trash_note.setObjectName("muted")
        self.trash_bar.addWidget(self.trash_note)
        self.trash_bar.addStretch(1)
        self.empty_btn = QPushButton("Empty trash")
        self.empty_btn.setObjectName("danger")
        self.empty_btn.clicked.connect(self._empty_trash)
        self.trash_bar.addWidget(self.empty_btn)
        v.addLayout(self.trash_bar)

        # selection bar (tick the circles on cards, then act on all of them at once)
        self.selbar = QWidget()
        self.selbar.setObjectName("selbar")
        self.selbar.setStyleSheet(f"QWidget#selbar {{ background: {P['panel2']}; border: 1px solid {P['border']};"
                                  f" border-radius: 10px; }}")
        sb = QHBoxLayout(self.selbar)
        sb.setContentsMargins(14, 6, 8, 6)
        self.sel_lbl = QLabel()
        self.sel_lbl.setStyleSheet("font-weight: 600;")
        sb.addWidget(self.sel_lbl)
        sb.addSpacing(8)
        self.sel_all = QPushButton("Select all")
        self.sel_all.clicked.connect(self._select_all)
        sb.addWidget(self.sel_all)
        sb.addStretch(1)
        self.sel_album = QPushButton(" Add to album")
        self.sel_album.setIcon(icon("folder"))
        self.sel_album.clicked.connect(lambda: self._album_menu(self._selected()).exec(
            self.sel_album.mapToGlobal(self.sel_album.rect().bottomLeft())))
        sb.addWidget(self.sel_album)
        self.sel_unalbum = QPushButton("Remove from album")
        self.sel_unalbum.clicked.connect(lambda: self._bulk("unalbum"))
        sb.addWidget(self.sel_unalbum)
        self.sel_fav = QPushButton(" Favorite")
        self.sel_fav.setIcon(icon("star"))
        self.sel_fav.clicked.connect(lambda: self._bulk("favorite"))
        self.sel_forget = QPushButton("Remove from ClipVault")
        self.sel_forget.setToolTip("Hide from the library, keep the files")
        self.sel_forget.clicked.connect(lambda: self._bulk("forget"))
        self.sel_restore = QPushButton(" Restore")
        self.sel_restore.setIcon(icon("restore"))
        self.sel_restore.clicked.connect(lambda: self._bulk("restore"))
        self.sel_trash = QPushButton(" Move to trash")
        self.sel_trash.setObjectName("danger")
        self.sel_trash.setIcon(icon("trash", "#fff"))
        self.sel_trash.clicked.connect(lambda: self._bulk("trash"))
        self.sel_delete = QPushButton(" Delete forever")
        self.sel_delete.setObjectName("danger")
        self.sel_delete.setIcon(icon("trash", "#fff"))
        self.sel_delete.clicked.connect(lambda: self._bulk("delete"))
        self.sel_done = QPushButton("Done")
        self.sel_done.clicked.connect(self._clear_selection)
        for b in (self.sel_fav, self.sel_forget, self.sel_restore, self.sel_trash, self.sel_delete, self.sel_done):
            sb.addWidget(b)
        self.selbar.hide()
        v.addWidget(self.selbar)

        self.grid = Grid()
        self.grid.on_header_action = self._header_action
        self.grid.on_header_toggle = self._toggle_section
        self.grid.on_scrub = lambda iid: self.ctl.cfg.get("app.hover_preview", True) and self.ctl.library.request_strip(iid)
        self.grid.itemSelectionChanged.connect(self._update_selbar)
        self.grid.itemDoubleClicked.connect(lambda li: self._open(self.grid.item_dict(li)))
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.grid, 1)

        self.empty = QWidget()
        ev = QVBoxLayout(self.empty)
        ev.addStretch(1)
        g = W.glyph_label("film", 44, P.get("faint"))
        g.setAlignment(Qt.AlignCenter)
        ev.addWidget(g)
        self.empty_t = QLabel("Nothing here yet")
        self.empty_t.setObjectName("h2")
        self.empty_t.setAlignment(Qt.AlignCenter)
        self.empty_s = QLabel()
        self.empty_s.setObjectName("muted")
        self.empty_s.setAlignment(Qt.AlignCenter)
        self.empty_s.setWordWrap(True)
        ev.addWidget(self.empty_t)
        ev.addWidget(self.empty_s)
        ev.addStretch(2)
        v.addWidget(self.empty, 1)
        self._fill_games()
        self.set_view("all")

    # -------------------------------------------------------------- data
    def _fill_games(self):
        cur = self.game.currentData()
        self.game.blockSignals(True)
        self.game.clear()
        self.game.addItem("All games", None)
        for disp, n, key in self.ctl.library.games():
            self.game.addItem(f"{disp}  ({n})", key)
        i = self.game.findData(cur)
        self.game.setCurrentIndex(max(0, i))
        self.game.blockSignals(False)

    def _game_changed(self):
        self.game_filter = self.game.currentData()
        self.refresh()

    def set_view(self, view: str, game_key: str | None = None, game_name: str | None = None):
        self.view = view
        if game_key is not None:
            self.game_filter = game_key
            i = self.game.findData(game_key)
            self.game.blockSignals(True)
            self.game.setCurrentIndex(max(0, i))
            self.game.blockSignals(False)
        else:
            self.game_filter = None
            self.game.blockSignals(True)
            self.game.setCurrentIndex(0)
            self.game.blockSignals(False)
        self.title.setText(game_name or (view[6:] if view.startswith("album:") else None) or {
            "all": "All media", "clips": "Clips", "sessions": "Long recordings", "screenshots": "Screenshots",
            "favorites": "Favorites", "bookmarked": "Bookmarked", "linked": "Linked recordings",
            "trash": "Trash"}.get(view, "Library"))
        self.refresh()

    def refresh(self):
        self._fill_games()
        if self.view != getattr(self, "_last_view", None):
            self.grid.clearSelection()
            self.grid.set_check_mode(False)
            self._last_view = self.view
        items = self.ctl.library.query(self.view, self.game_filter, self.search.text(),
                                       self.sort.currentData() or "newest", self.source.currentData() or "all")
        self._n_items = len(items)
        self.grid.strip_dir = self.ctl.library.thumbs
        entries = items
        if (self.sort.currentData() or "newest") == "newest" and not self.search.text() and self.view != "trash" and items:
            entries = self._grouped(items)
        self.grid.set_items(entries)
        self.count.setText(f"{len(items)} item{'s' if len(items) != 1 else ''}")
        is_trash = self.view == "trash"
        self.trash_note.setVisible(is_trash)
        self.empty_btn.setVisible(is_trash and bool(items))
        self.trash_note.setText(f"Items in trash are deleted for good after {int(self.ctl.cfg.get('storage.trash_days', 30))} days.")
        self.grid.setVisible(bool(items))
        self.empty.setVisible(not items)
        self.scan_pill.setVisible(self.ctl.library.scanning)
        hk = self.ctl.cfg.get("hotkeys")
        filtered = bool(self.search.text() or self.game_filter or (self.source.currentData() or "all") != "all")
        self.empty_t.setText("No matches" if filtered else "Nothing here yet")
        self.empty_s.setText("Try clearing the search or filters." if filtered else {
            "trash": "Deleted clips land here first, so nothing is lost by accident.",
            "favorites": "Star a clip to keep it here (favorites are never auto-deleted).",
            "screenshots": f"Press {hk['screenshot']} in-game to grab a screenshot.",
            "sessions": f"Press {hk['long_recording']} to start a long recording — or play a game with full-session recording on.",
            "bookmarked": f"Press {hk['bookmark']} during a long recording to drop a bookmark.",
            "linked": "Videos already in your capture folder show up here automatically.\n"
                      "Add more folders in Settings → Storage → Linked recordings.",
        }.get(self.view, f"Press {hk['clip']} in-game to save the last {self.ctl.cfg.get('recording.clip_seconds')} seconds.\n"
                         "Videos already in your capture folder are linked automatically."))

        if self.view.startswith("album:") and not filtered:
            self.empty_t.setText("This album is empty")
            self.empty_s.setText("Right-click clips (or tick several) → Add to album.")

    def _grouped(self, items: list[dict]) -> list[dict]:
        """Medal-style sections: your last session first, then Today / This week / Earlier."""
        out = []
        closed = set(self.ctl.cfg.get("app.collapsed_sections", []) or [])
        ls = self.ctl.library.last_session() if self.view in ("all", "clips", "sessions", "screenshots") else None
        ids = set(ls[2]) if ls else set()
        top = [i for i in items if i["id"] in ids]
        if top:
            unseen = [i["id"] for i in top if i.get("seen") is False]
            sub = f"{ls[0]} · {human_when(ls[1])} · {len(top)} item{'s' if len(top) != 1 else ''}" + \
                (f" · {len(unseen)} new" if unseen else "")
            out.append({"_hdr": True, "id": "hdr:last", "title": "Last session", "sub": sub, "collapsed": "Last session" in closed,
                        "action": "seen" if unseen else None, "ids": [i["id"] for i in top]})
            if "Last session" not in closed:
                out += top
        today = datetime.now().date()

        def bucket(ts):
            d = datetime.fromtimestamp(ts).date()
            if d == today:
                return "Today"
            if (today - d).days < 7:
                return "This week"
            return "Earlier"  # (few big sections — one per month left lots of half-empty rows)
        groups: dict[str, list] = {}
        for it in items:
            if it["id"] not in ids:
                groups.setdefault(bucket(it["created"]), []).append(it)
        for b, its in groups.items():  # (dicts keep insertion order: newest section first)
            out.append({"_hdr": True, "id": f"hdr:{b}", "title": b, "sub": f"{len(its)} item{'s' if len(its) != 1 else ''}", "collapsed": b in closed})
            if b not in closed:
                out += its
        return out

    def _toggle_section(self, hid: str):
        """Click a section header to fold it away (remembered)."""
        h = self.grid.headers.get(hid)
        if not h:
            return
        closed = list(self.ctl.cfg.get("app.collapsed_sections", []) or [])
        if h["title"] in closed:
            closed.remove(h["title"])
        else:
            closed.append(h["title"])
        self.ctl.cfg.set("app.collapsed_sections", closed)
        self.refresh()

    def _header_action(self, hid: str):
        h = self.grid.headers.get(hid)
        if h and h.get("action") == "seen":
            self.ctl.library.mark_seen(h.get("ids", []))
            self.refresh()

    def _album_menu(self, items) -> QMenu:
        lib = self.ctl.library
        m = QMenu(self)
        for name, n in lib.albums():
            m.addAction(icon("folder"), f"{name}  ({n})",
                        lambda name=name: (lib.add_to_album([i["id"] for i in items], name),
                                           self.window().toast("Added to album", f"{len(items)} → {name}")))
        if lib.album_names:
            m.addSeparator()

        def new():
            name, ok = QInputDialog.getText(self, "New album", "Album name:")
            if ok and name.strip():
                lib.add_to_album([i["id"] for i in items], name)
                self.window().toast("Album created", f"{name.strip()} · {len(items)} item(s)")
        m.addAction(icon("add"), "New album…", new)
        return m

    def update_item(self, item_id: str | None):
        """Fast path for a single changed item (thumbnail finished, favorite toggled…)."""
        it = self.ctl.library.get(item_id) if item_id else None
        if it is not None and self.grid.update_item(it):
            return True
        return False

    def repaint_grid(self):
        self.grid.doItemsLayout()
        self.grid.viewport().update()

    def set_scanning(self, on: bool):
        self.scan_pill.setVisible(on)

    # -------------------------------------------------------------- selection
    def _update_selbar(self):
        n = len(self.grid.selectedItems())
        show = n > 1 or (n >= 1 and self.grid.check_mode)
        self.selbar.setVisible(show)
        if not show:
            return
        is_trash = self.view == "trash"
        self.sel_lbl.setText(f"{n} selected")
        self.sel_all.setVisible(n < getattr(self, "_n_items", self.grid.count()))
        self.sel_album.setVisible(not is_trash)
        self.sel_unalbum.setVisible(self.view.startswith("album:"))
        self.sel_fav.setVisible(not is_trash)
        self.sel_forget.setVisible(not is_trash)
        self.sel_trash.setVisible(not is_trash)
        self.sel_restore.setVisible(is_trash)
        self.sel_delete.setVisible(is_trash)

    def _select_all(self):
        self.grid.check_mode = True
        self.grid.selectAll()

    def _clear_selection(self):
        self.grid.clearSelection()
        self.grid.set_check_mode(False)

    def _bulk(self, action):
        items = self._selected()
        if not items:
            return
        lib = self.ctl.library
        ids = [i["id"] for i in items]
        if action == "favorite":
            fav = not all(i.get("favorite") for i in items)
            for i in ids:
                lib.update(i, favorite=fav)
            return
        if action == "unalbum":
            lib.remove_from_album(ids, self.view[6:])
        elif action == "forget":
            self._forget(items)
        elif action == "restore":
            lib.restore(ids)
        elif action == "delete":
            self._delete_forever(items)
        elif action == "trash":
            lib.trash(ids)
            self.window().toast(f"Moved {len(ids)} to trash", "Restore them from Trash, or empty it to free the space")
        self._clear_selection()
        self.refresh()

    # -------------------------------------------------------------- actions
    def _selected(self) -> list[dict]:
        return [d for d in (self.grid.item_dict(li) for li in self.grid.selectedItems()) if d]

    def _open(self, it):
        if not it:
            return
        if it["kind"] == "screenshot":
            QDesktopServices.openUrl(QUrl.fromLocalFile(it["path"]))
        else:
            self.open_item.emit(it)

    def _menu(self, pos: QPoint):
        li = self.grid.itemAt(pos)
        if li and not li.isSelected():
            self.grid.clearSelection()
            li.setSelected(True)
        items = self._selected()
        if not items:
            return
        lib = self.ctl.library
        m = QMenu(self)
        one = items[0]
        if len(items) == 1:
            m.addAction(icon("play"), "Open" if one["kind"] == "screenshot" else "Open in editor", lambda: self._open(one))
            m.addAction(icon("folder"), "Show in folder", lambda: reveal(one["path"]))
            m.addAction(icon("copy"), "Copy file (paste into Discord)", lambda: self._copy_files(items))
            m.addAction(icon("rename"), "Rename…", lambda: self._rename(one))
        else:
            m.addAction(icon("copy"), f"Copy {len(items)} files", lambda: self._copy_files(items))
        fav = not all(i.get("favorite") for i in items)
        m.addAction(icon("star_on" if fav else "star"), "Add to favorites" if fav else "Remove from favorites",
                    lambda: [lib.update(i["id"], favorite=fav) for i in items])
        if self.view != "trash":
            am = self._album_menu(items)
            am.setTitle("Add to album")
            am.setIcon(icon("folder"))
            m.addMenu(am)
            if self.view.startswith("album:"):
                m.addAction("Remove from this album", lambda: lib.remove_from_album([i["id"] for i in items], self.view[6:]))
        m.addSeparator()
        if self.view == "trash":
            m.addAction(icon("restore"), "Restore", lambda: lib.restore([i["id"] for i in items]))
            m.addAction(icon("trash", P["danger"]), "Delete forever", lambda: self._delete_forever(items))
        else:
            m.addAction(icon("close"), "Remove from ClipVault (keep the file)", lambda: self._forget(items))
            m.addAction(icon("trash", P["danger"]), "Move to trash", lambda: lib.trash([i["id"] for i in items]))
        m.exec(self.grid.mapToGlobal(pos))

    def keyPressEvent(self, e):
        sel = self._selected()
        if e.key() == Qt.Key_Escape and (sel or self.grid.check_mode):
            self._clear_selection()
        elif e.key() == Qt.Key_Delete and sel:
            if self.view == "trash":
                self._delete_forever(sel)
            else:
                self.ctl.library.trash([i["id"] for i in sel])
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter) and sel:
            self._open(sel[0])
        elif e.key() == Qt.Key_C and e.modifiers() & Qt.ControlModifier and sel:
            self._copy_files(sel)
        else:
            super().keyPressEvent(e)

    def _copy_files(self, items):
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(i["path"]) for i in items])
        QApplication.clipboard().setMimeData(md)
        self.window().toast("Copied", "Paste into Discord, a folder, anywhere")

    def _rename(self, it):
        name, ok = QInputDialog.getText(self, "Rename", "Title:", text=nice_title(it))
        if ok:
            self.ctl.library.update(it["id"], title=name.strip())

    def _forget(self, items):
        n = len(items)
        if QMessageBox.question(self, "Remove from ClipVault",
                                f"Remove {n} item(s) from ClipVault?\n\nThe files stay on your drive and won't be "
                                "linked again (re-add them with Import).") == QMessageBox.Yes:
            self.ctl.library.forget([i["id"] for i in items])

    def _delete_forever(self, items):
        linked = sum(1 for i in items if i.get("linked"))
        extra = f"\n\n{linked} of these are linked recordings (Medal/NVIDIA/OBS) — their files are deleted too." if linked else ""
        if QMessageBox.question(self, "Delete forever", f"Permanently delete {len(items)} file(s) from disk?{extra}") \
                == QMessageBox.Yes:
            failed = self.ctl.library.delete_forever([i["id"] for i in items])
            if failed:
                self.window().toast("Some files couldn't be deleted",
                                    f"In use by another app: {', '.join(failed[:3])}{'…' if len(failed) > 3 else ''}",
                                    error=True)

    def _empty_trash(self):
        items = self.ctl.library.query("trash")
        if items:
            self._delete_forever(items)

    def _import(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Import videos or screenshots", str(self.ctl.cfg.storage),
                                                "Media (*.mp4 *.mkv *.mov *.webm *.avi *.m4v *.png *.jpg *.jpeg *.webp)")
        if files:
            n = self.ctl.library.import_files(files)
            self.window().toast("Imported", f"{n} file(s) added — they stay where they are")
