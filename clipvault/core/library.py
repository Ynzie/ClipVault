"""Local media library: ClipVault's clips, long recordings and screenshots, plus *linked*
recordings — existing videos found in the capture folder (old Medal / NVIDIA / OBS captures)
and any extra folders you add. Linked files stay where they are; ClipVault only indexes them.

Index lives in <storage>/library.json; thumbnails in <storage>/.thumbs.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import ffmpeg as ff

log = logging.getLogger("clipvault.library")

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".wmv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# folders that hold other apps' caches/thumbnails/temp files, never recordings
SKIP_DIRS = {"thumbnails", "temp", "tmp", "databases", "video-editor", "reels", "cache", "caches", "logs",
             "system volume information", "proxies", "previews", "node_modules"}
# path parts that say nothing about the game (Medal "Clips/<Game>", "Videos/Medal/...")
GENERIC_PARTS = {"clips", "sessions", "screenshots", "videos", "recordings", "captures", "highlights", "replays",
                 "medal", "obs", "nvidia", "shadowplay", "geforce", "radeon", "radeon relive", "amd",
                 "xbox game bar", "game bar", "clipvault"}
LONG_SECONDS = 600
THUMB_V = 2  # bump to redo thumbnails (v2: skip black loading screens)


def _dark(jpg: Path) -> bool:
    """Mostly-black frame (loading screen, fade-in)?"""
    try:
        from PIL import Image, ImageStat
        with Image.open(jpg) as im:
            return ImageStat.Stat(im.convert("L").resize((64, 36))).mean[0] < 16
    except Exception:
        return False


def thumb_points(dur: float, kind: str) -> list[float]:
    """Where to grab a thumbnail, best guess first. Long recordings start on loading screens."""
    if not dur:
        return [0.5]
    if kind == "session" or dur >= LONG_SECONDS:
        return [min(dur * 0.15, 600), dur * 0.4, dur * 0.65, dur * 0.85]
    return [min(dur * 0.3, 8.0), dur * 0.5, dur * 0.75, dur * 0.15]  # linked videos this long count as "long recordings"


# muxer boilerplate, not real track names: "SoundHandler", "OBS Audio Handler", "IsoMedia File Produced by Google…"
_JUNK_TRACK_NAME = re.compile(r"handle|isomedia|produced by|mainconcept|^#", re.I)


def track_title(stream: dict, i: int) -> str:
    tags = stream.get("tags") or {}
    name = (tags.get("title") or tags.get("handler_name") or "").strip()
    return name if name and not _JUNK_TRACK_NAME.search(name) else f"Track {i + 1}"


def game_key(name: str | None) -> str:
    """'Call of Duty  Black Ops II' == 'Call Of Duty Black Ops Ii' == 'Call of Duty: Black Ops II'."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold()) or "desktop"


def display_game(folder: str) -> str:
    # NVIDIA strips ':' from folder names leaving a double space ("Call of Duty  Modern Warfare 2019")
    return re.sub(r"\s{2,}", ": ", folder.strip()) or "Unsorted"


def norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def guess_source(name: str) -> str | None:
    if name.startswith("MedalTV"):
        return "medal"
    if re.search(r"\d{4}\.\d{2}\.\d{2} - \d{2}\.\d{2}\.\d{2}\.\d{2}", name):
        return "nvidia"
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}", name):
        return "obs"
    return None


def _valid(y, mo, d, h, mi, s):
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    return dt.timestamp() if 2010 <= dt.year <= datetime.now().year + 1 else None


def parse_created(name: str) -> float | None:
    """Recording time from common file-name patterns (more reliable than mtime after copies)."""
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2}) - (\d{2})\.(\d{2})\.(\d{2})", name)  # NVIDIA
    if m:
        return _valid(*m.groups())
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})", name)  # OBS / ClipVault
    if m:
        return _valid(*m.groups())
    for run in re.findall(r"\d{14,}", name):  # Medal: MedalTV<Game>YYYYMMDDhhmmss[mmm]
        for off in range(0, len(run) - 13):
            c = run[off:off + 14]
            ts = _valid(c[0:4], c[4:6], c[6:8], c[8:10], c[10:12], c[12:14])
            if ts:
                return ts
    return None


def nice_title(it: dict) -> str:
    """Display title: your own title/file name, or 'Game · date' for auto-named Medal/NVIDIA/OBS files."""
    if it.get("title"):
        return it["title"]
    name = Path(it["path"]).name
    stem = Path(name).stem
    src = it.get("source") or guess_source(name)
    if src in ("medal", "nvidia", "obs") or re.fullmatch(r".*\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}", stem):
        when = datetime.fromtimestamp(it.get("created") or 0)
        return f"{it.get('game') or 'Recording'} · {when:%b %d, %Y} {when:%I:%M %p}".replace(" 0", " ")
    return stem


def infer_game(path: Path, root: Path | None) -> str:
    try:
        parts = list(path.relative_to(root).parts[:-1]) if root else [path.parent.name]
    except ValueError:
        parts = [path.parent.name]
    for part in parts:
        if part.casefold() not in GENERIC_PARTS:
            return display_game(part)
    return "Unsorted"


def skip_dir(name: str) -> bool:
    return name.startswith((".", "$")) or name.casefold() in SKIP_DIRS


class Library:
    def __init__(self, cfg, events=None):
        self.cfg = cfg
        self.events = events
        self._lock = threading.RLock()
        self.items: dict[str, dict] = {}
        self.excluded: set[str] = set()  # normalized paths the user removed but kept on disk
        self.album_names: list[str] = []  # albums (also the empty ones)
        self._strip_busy = False
        self._strip_next: str | None = None
        self._save_timer = None
        self.scanning = False
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        for n in range(2):
            threading.Thread(target=self._worker, daemon=True, name=f"LibraryWorker{n}").start()
        self.reload()

    # ---------------------------------------------------------------- paths
    @property
    def root(self) -> Path:
        return self.cfg.storage

    @property
    def db_path(self) -> Path:
        return self.root / "library.json"

    @property
    def thumbs(self) -> Path:
        p = self.root / ".thumbs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def folder(self, kind: str, game: str | None) -> Path:
        sub = {"clip": "Clips", "session": "Sessions", "screenshot": "Screenshots"}.get(kind, "Clips")
        safe = "".join(c for c in (game or "Desktop") if c not in '<>:"/\\|?*').strip() or "Desktop"
        p = self.root / sub / safe
        p.mkdir(parents=True, exist_ok=True)
        return p

    def scan_roots(self) -> list[Path]:
        roots = []
        if self.cfg.get("storage.scan_storage", True):
            roots.append(self.root)
        for f in self.cfg.get("storage.linked_folders", []) or []:
            p = Path(f)
            if all(norm(p) != norm(r) for r in roots):
                roots.append(p)
        return roots

    # ---------------------------------------------------------------- persistence
    def reload(self):
        with self._lock:
            self.items, self.excluded, self.album_names = {}, set(), []
            if self.db_path.exists():
                try:
                    data = json.loads(self.db_path.read_text("utf-8"))
                    self.items = {i["id"]: i for i in data.get("items", [])}
                    self.excluded = set(data.get("excluded", []))
                    self.album_names = list(data.get("albums", []))
                except Exception:
                    self.items = {}
            # drop entries whose files were deleted (but not when their drive/folder is offline)
            for k, v in list(self.items.items()):
                p = Path(v["path"])
                if not p.exists() and p.parent.parent.exists():
                    self.items.pop(k)
            # (a file that timed out 3 times isn't retried at every start)
            missing = [v["id"] for v in self.items.values()
                       if (not v.get("thumb") or not Path(v["thumb"]).exists()) and v.get("probe_fail", 0) < 3]
            # one-time: re-check older video thumbnails (black loading-screen frames get replaced)
            redo = [v["id"] for v in self.items.values() if v["id"] not in missing and v.get("kind") != "screenshot"
                    and v.get("thumb_v", 0) < THUMB_V and not v.get("broken")]
        for i in missing:  # thumbnails/probe that never finished (app closed mid-way)
            self._enqueue(i, 5)
        for i in redo:
            self._enqueue(i, 7)

    def save(self, now: bool = False):
        if not now:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.8, self.save, kwargs={"now": True})
            self._save_timer.daemon = True
            self._save_timer.start()
            return
        with self._lock:
            text = json.dumps({"version": 2, "items": [dict(v) for v in self.items.values()],
                               "excluded": sorted(self.excluded), "albums": list(self.album_names)}, indent=1)
            try:
                tmp = self.db_path.with_suffix(".tmp")
                tmp.write_text(text, "utf-8")
                os.replace(tmp, self.db_path)
            except OSError:
                pass  # drive busy/offline — next save retries

    def _emit(self, name, payload=None):
        if self.events:
            self.events.emit(name, payload)

    # ---------------------------------------------------------------- add / update
    def _new_item(self, path: Path, kind: str, game: str | None, meta: dict) -> dict:
        item = {
            "id": uuid.uuid4().hex[:12], "path": str(path), "kind": kind, "game": game or "Desktop",
            "created": meta.pop("created", time.time()), "title": meta.pop("title", ""),
            "favorite": False, "trashed_at": None, "bookmarks": meta.pop("bookmarks", []),
            "session_id": meta.pop("session_id", None), "duration": 0.0,
            "size": path.stat().st_size if path.exists() else 0, "thumb": None, "hdr": meta.pop("hdr", None),
        }
        item.update(meta)
        return item

    def add(self, path: Path, kind: str, game: str | None, **meta) -> dict:
        meta.setdefault("seen", bool(meta.get("linked")))  # fresh captures show as new until you look at them
        item = self._new_item(Path(path), kind, game, meta)
        with self._lock:
            self.items[item["id"]] = item
        self._enqueue(item["id"], 0)  # new captures jump the queue
        self.save()
        self._emit("library_changed", item["id"])
        return item

    def _enqueue(self, item_id: str, priority: int):
        self._jobs.put((priority, next(self._seq), item_id))

    def _worker(self):
        while True:
            _, _, item_id = self._jobs.get()
            try:
                self._finish_item(item_id)
            except Exception:
                log.exception("library: couldn't read %s", (self.items.get(item_id) or {}).get("path"))

    def _finish_item(self, item_id: str):
        it = self.items.get(item_id)
        if not it:
            return
        p = it["path"]
        if not Path(p).exists():
            return
        thumb = self.thumbs / f"{item_id}.jpg"
        upd = {}
        if (it["kind"] != "screenshot" and it.get("duration") and it.get("thumb") and thumb.exists()
                and it.get("thumb_v", 0) < THUMB_V):
            # already probed: only redo the thumbnail if it's a black frame
            if _dark(thumb):
                self._make_thumb(p, thumb, it["duration"], it["kind"])
            with self._lock:
                it["thumb_v"] = THUMB_V
            self.save()
            self._emit("library_changed", item_id)
            return
        if it["kind"] == "screenshot":
            try:
                from PIL import Image  # optional
                with Image.open(p) as im:
                    im.thumbnail((480, 270))
                    im.convert("RGB").save(thumb, quality=85)
            except Exception:
                ff.run(ff.thumbnail_command(p, thumb, 0), timeout=30, low_priority=True)
        else:
            info, status = ff.probe_checked(p, timeout=180, low_priority=True)
            if status == "timeout":
                # huge or unfinished (fragmented) recording on a slow drive: not broken, try again next start
                with self._lock:
                    it["probe_fail"] = it.get("probe_fail", 0) + 1
                self.save()
                return
            try:
                dur = float(info.get("format", {}).get("duration") or 0)
            except ValueError:
                dur = 0.0
            upd["duration"] = dur
            upd["probe_fail"] = 0
            kind = ("session" if dur >= LONG_SECONDS else "clip") if it.get("linked") else it["kind"]
            self._make_thumb(p, thumb, dur, kind)
            upd["thumb_v"] = THUMB_V
            upd["audio_tracks"] = [track_title(st, i) for i, st in
                                   enumerate(x for x in info.get("streams", []) if x.get("codec_type") == "audio")]
            v = next((x for x in info.get("streams", []) if x.get("codec_type") == "video"), {})
            upd.update(width=v.get("width"), height=v.get("height"), codec=v.get("codec_name"))
            if it.get("linked") and it["kind"] in ("clip", "session"):
                upd["kind"] = "session" if dur >= LONG_SECONDS else "clip"
            upd["broken"] = not v  # no video stream = not playable (e.g. a recording cut off by a crash)
        if thumb.exists():
            upd["thumb"] = str(thumb)
        try:
            upd["size"] = Path(p).stat().st_size
        except OSError:
            pass
        with self._lock:
            it.update(upd)
        self.save()
        self._emit("library_changed", item_id)

    def _make_thumb(self, src: str, thumb: Path, dur: float, kind: str):
        """Grab a frame; if it's black (loading screen / fade), try further in. Keeps the best one found."""
        for n, at in enumerate(thumb_points(dur, kind)):
            try:
                tmp = thumb.with_name(thumb.stem + ".try.jpg")
                ff.run(ff.thumbnail_command(src, tmp, at), timeout=120, low_priority=True)
                if not tmp.exists():
                    continue
                if n == 0 or not thumb.exists() or not _dark(tmp):
                    os.replace(tmp, thumb)
                else:
                    tmp.unlink(missing_ok=True)
                if not _dark(thumb):
                    return
            except Exception:
                pass  # no thumbnail, but keep what the probe found

    def update(self, item_id: str, **fields):
        with self._lock:
            if item_id in self.items:
                self.items[item_id].update(fields)
        self.save()
        self._emit("library_changed", item_id)

    def get(self, item_id: str) -> dict | None:
        return self.items.get(item_id)

    def snapshot(self) -> list[dict]:
        """Thread-safe list of items (other threads add/update while the UI reads)."""
        with self._lock:
            return list(self.items.values())

    # ---------------------------------------------------------------- linking existing recordings
    def scan(self, busy: set[str] | None = None) -> dict:
        """Index videos/screenshots in the capture folder (+ linked folders) that ClipVault doesn't know yet,
        and forget linked files that were deleted outside ClipVault. Safe to call repeatedly."""
        if self.scanning:
            return {"added": 0, "removed": 0, "busy": True}
        self.scanning = True
        self._emit("scan_state", True)
        try:
            return self._scan(busy or set())
        finally:
            self.scanning = False
            self._emit("scan_state", False)

    def _scan(self, busy: set[str]) -> dict:
        roots = [r for r in self.scan_roots() if r.exists()]
        busy_n = {norm(b) for b in busy}
        with self._lock:
            known = {norm(i["path"]) for i in self.items.values()}
            excluded = set(self.excluded)
        now = time.time()
        found = []
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not skip_dir(d)]
                for f in filenames:
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in VIDEO_EXT and ext not in IMAGE_EXT:
                        continue
                    p = os.path.join(dirpath, f)
                    n = norm(p)
                    if n in known or n in excluded or n in busy_n:
                        continue
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if now - st.st_mtime < 30:  # still being written (OBS, an export in progress…)
                        continue
                    if ext in VIDEO_EXT and st.st_size < 200_000:
                        continue
                    if ext in IMAGE_EXT and st.st_size < 30_000:  # icons / tiny thumbnails
                        continue
                    known.add(n)
                    found.append((Path(p), root, st))
        new_items = []
        for p, root, st in found:
            kind = "screenshot" if p.suffix.lower() in IMAGE_EXT else "clip"
            meta = {"created": parse_created(p.name) or st.st_mtime, "title": "", "linked": True,
                    "source": guess_source(p.name)}
            new_items.append(self._new_item(p, kind, infer_game(p, root), meta))
        # forget linked entries whose files are gone (only when their folder is still reachable)
        removed = []
        with self._lock:
            for k, v in list(self.items.items()):
                if v.get("linked") and not Path(v["path"]).exists() and Path(v["path"]).parent.exists():
                    removed.append(k)
                    self.items.pop(k)
            for it in new_items:
                self.items[it["id"]] = it
        for it in sorted(new_items, key=lambda i: -i["created"]):  # newest get thumbnails first
            self._enqueue(it["id"], 5)
        if new_items or removed:
            self.save()
            self._emit("library_changed", None)
        return {"added": len(new_items), "removed": len(removed),
                "videos": sum(1 for i in new_items if i["kind"] != "screenshot"),
                "screenshots": sum(1 for i in new_items if i["kind"] == "screenshot")}

    def forget(self, ids: list[str]):
        """Remove from ClipVault but keep the files on disk (won't be re-linked)."""
        with self._lock:
            for i in ids:
                it = self.items.pop(i, None)
                if it:
                    self.excluded.add(norm(it["path"]))
                    if it.get("thumb"):
                        try:
                            Path(it["thumb"]).unlink(missing_ok=True)
                        except OSError:
                            pass
        self.save()
        self._emit("library_changed", None)

    def unlink_folder(self, folder: str):
        """A linked folder was removed in settings: drop its linked entries (files untouched)."""
        f = norm(folder).rstrip("\\/") + os.sep
        storage = norm(self.root).rstrip("\\/") + os.sep
        with self._lock:
            for k, v in list(self.items.items()):
                n = norm(v["path"])
                if v.get("linked") and n.startswith(f) and not n.startswith(storage):
                    self.items.pop(k)
        self.save()
        self._emit("library_changed", None)

    # ---------------------------------------------------------------- queries
    def query(self, view: str = "all", game: str | None = None, search: str = "", sort: str = "newest",
              source: str = "all") -> list[dict]:
        items = [i for i in self.snapshot() if not i.get("broken")]
        if view == "trash":
            items = [i for i in items if i.get("trashed_at")]
        else:
            items = [i for i in items if not i.get("trashed_at")]
            if view == "clips":
                items = [i for i in items if i["kind"] in ("clip", "import")]
            elif view == "sessions":
                items = [i for i in items if i["kind"] == "session"]
            elif view == "screenshots":
                items = [i for i in items if i["kind"] == "screenshot"]
            elif view == "favorites":
                items = [i for i in items if i.get("favorite")]
            elif view == "bookmarked":
                items = [i for i in items if i.get("bookmarks")]
            elif view == "linked":
                items = [i for i in items if i.get("linked")]
            elif view.startswith("album:"):
                name = view[6:]
                items = [i for i in items if name in (i.get("albums") or [])]
        if source == "clipvault":
            items = [i for i in items if not i.get("linked")]
        elif source == "linked":
            items = [i for i in items if i.get("linked")]
        if game:
            items = [i for i in items if game_key(i.get("game")) == game]
        if search:
            s = search.lower()
            items = [i for i in items if s in (i.get("title") or "").lower() or s in (i.get("game") or "").lower()
                     or s in Path(i["path"]).name.lower() or any(s in a.lower() for a in i.get("albums") or [])]
        key = {"newest": (lambda i: -i["created"]), "oldest": (lambda i: i["created"]),
               "largest": (lambda i: -i.get("size", 0)), "longest": (lambda i: -(i.get("duration") or 0))}.get(sort)
        items.sort(key=key or (lambda i: -i["created"]))
        return items

    # ---------------------------------------------------------------- new / last session
    def mark_seen(self, ids):
        changed = False
        with self._lock:
            for i in ids:
                it = self.items.get(i)
                if it and it.get("seen") is False:
                    it["seen"] = True
                    changed = True
        if changed:
            self.save()
            self._emit("library_changed", None)

    def last_session(self) -> tuple[str, float, list[str]] | None:
        """Your most recent play session: (game, time, item ids) — ClipVault's own captures from that game
        (same long recording, or taken less than 45 min apart)."""
        own = sorted((i for i in self.snapshot() if not i.get("linked") and not i.get("trashed_at")
                      and not i.get("broken")), key=lambda i: -i["created"])
        if not own:
            return None
        first = own[0]
        game, sid = first.get("game"), first.get("session_id")
        ids, prev = [], first["created"]
        for it in own:
            same = (sid and it.get("session_id") == sid) or (it.get("game") == game and prev - it["created"] < 2700)
            if not same:
                if it["created"] < prev - 2700:
                    break
                continue
            ids.append(it["id"])
            prev = it["created"]
        return game, first["created"], ids

    # ---------------------------------------------------------------- albums
    def albums(self) -> list[tuple[str, int]]:
        counts = Counter(a for i in self.snapshot() if not i.get("trashed_at") for a in i.get("albums") or [])
        return [(n, counts.get(n, 0)) for n in self.album_names]

    def create_album(self, name: str) -> str:
        name = " ".join(name.split())[:60]
        if name and name not in self.album_names:
            with self._lock:
                self.album_names.append(name)
            self.save()
            self._emit("library_changed", None)
        return name

    def add_to_album(self, ids, name: str):
        name = self.create_album(name)
        if not name:
            return
        with self._lock:
            for i in ids:
                it = self.items.get(i)
                if it is not None and name not in (it.get("albums") or []):
                    it["albums"] = list(it.get("albums") or []) + [name]
        self.save()
        self._emit("library_changed", None)

    def remove_from_album(self, ids, name: str):
        with self._lock:
            for i in ids:
                it = self.items.get(i)
                if it is not None and name in (it.get("albums") or []):
                    it["albums"] = [a for a in it["albums"] if a != name]
        self.save()
        self._emit("library_changed", None)

    def rename_album(self, old: str, new: str) -> bool:
        new = " ".join(new.split())[:60]
        if not new or new in self.album_names or old not in self.album_names:
            return False
        with self._lock:
            self.album_names[self.album_names.index(old)] = new
            for it in self.items.values():
                if old in (it.get("albums") or []):
                    it["albums"] = [new if a == old else a for a in it["albums"]]
        self.save()
        self._emit("library_changed", None)
        return True

    def delete_album(self, name: str):
        """The album goes; its clips stay in the library."""
        with self._lock:
            if name in self.album_names:
                self.album_names.remove(name)
            for it in self.items.values():
                if name in (it.get("albums") or []):
                    it["albums"] = [a for a in it["albums"] if a != name]
        self.save()
        self._emit("library_changed", None)

    # ---------------------------------------------------------------- hover scrub previews
    STRIP_FRAMES = 12

    def strip_path(self, item_id: str) -> Path:
        return self.thumbs / f"{item_id}.strip.jpg"

    def request_strip(self, item_id: str):
        """Make a strip of frames for hover-scrubbing (one at a time, newest request wins)."""
        it = self.items.get(item_id)
        if not it or it["kind"] == "screenshot" or not it.get("duration") or self.strip_path(item_id).exists():
            return
        with self._lock:
            if self._strip_busy:
                self._strip_next = item_id
                return
            self._strip_busy = True
        threading.Thread(target=self._strip_worker, args=(item_id,), daemon=True, name="Strip").start()

    def _strip_worker(self, item_id: str):
        while item_id:
            try:
                self._make_strip(item_id)
            except Exception:
                log.exception("strip for %s failed", item_id)
            with self._lock:
                item_id, self._strip_next = self._strip_next, None
                if item_id and self.strip_path(item_id).exists():
                    item_id = None
                if not item_id:
                    self._strip_busy = False

    def _make_strip(self, item_id: str):
        it = self.items.get(item_id)
        if not it:
            return
        from PIL import Image
        dur = float(it.get("duration") or 0)
        n = self.STRIP_FRAMES
        tmp = self.thumbs / f".{item_id}.strip"
        tmp.mkdir(exist_ok=True)
        frames = []
        try:
            for k in range(n):
                f = tmp / f"{k:02d}.jpg"
                ff.run([ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-ss", f"{dur * (k + 0.5) / n:.2f}",
                        "-i", it["path"], "-frames:v", "1", "-vf", "scale=320:180:force_original_aspect_ratio=increase,"
                        "crop=320:180", "-q:v", "5", str(f)], timeout=60, low_priority=True)
                if f.exists():
                    frames.append(f)
            if len(frames) < 2:
                return
            sheet = Image.new("RGB", (320 * len(frames), 180))
            for k, f in enumerate(frames):
                with Image.open(f) as im:
                    sheet.paste(im.convert("RGB"), (320 * k, 0))
            out = self.strip_path(item_id)
            sheet.save(out.with_suffix(".tmp.jpg"), quality=80)
            os.replace(out.with_suffix(".tmp.jpg"), out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._emit("library_changed", item_id)

    def games(self) -> list[tuple[str, int, str]]:
        """[(display name, count, key)] — spelling variants of the same game are merged."""
        groups: dict[str, Counter] = {}
        for i in self.snapshot():
            if not i.get("trashed_at") and not i.get("broken"):
                g = i.get("game") or "Desktop"
                groups.setdefault(game_key(g), Counter())[g] += 1
        out = []
        for k, c in groups.items():
            # prefer the variant with proper punctuation/casing, then the most common one
            disp = max(c, key=lambda g: (":" in g, sum(ch.isupper() for ch in g) < len(g) * 0.6, c[g]))
            out.append((disp, sum(c.values()), k))
        return sorted(out, key=lambda t: (-t[1], t[0].casefold()))

    def counts(self) -> dict[str, int]:
        allv = [i for i in self.snapshot() if not i.get("broken")]
        live = [i for i in allv if not i.get("trashed_at")]
        return {
            "all": len(live),
            "clips": sum(1 for i in live if i["kind"] in ("clip", "import")),
            "sessions": sum(1 for i in live if i["kind"] == "session"),
            "screenshots": sum(1 for i in live if i["kind"] == "screenshot"),
            "favorites": sum(1 for i in live if i.get("favorite")),
            "bookmarked": sum(1 for i in live if i.get("bookmarks")),
            "linked": sum(1 for i in live if i.get("linked")),
            "trash": sum(1 for i in allv if i.get("trashed_at")),
        }

    # ---------------------------------------------------------------- trash / delete
    def trash(self, ids: list[str]):
        with self._lock:
            for i in ids:
                if i in self.items:
                    self.items[i]["trashed_at"] = time.time()
        self.save()
        self._emit("library_changed", None)

    def restore(self, ids: list[str]):
        with self._lock:
            for i in ids:
                if i in self.items:
                    self.items[i]["trashed_at"] = None
        self.save()
        self._emit("library_changed", None)

    def delete_forever(self, ids: list[str]) -> list[str]:
        """Delete files from disk. Returns names that couldn't be deleted (e.g. open in another app)."""
        failed = []
        for i in ids:
            it = self.items.get(i)
            if not it:
                continue
            try:
                Path(it["path"]).unlink(missing_ok=True)
            except OSError:
                failed.append(Path(it["path"]).name)
                continue  # keep it in the library so nothing silently disappears
            with self._lock:
                self.items.pop(i, None)
            for extra in (it.get("thumb"), str(self.thumbs / f"{i}.strip.jpg")):
                if extra:
                    try:
                        Path(extra).unlink(missing_ok=True)
                    except OSError:
                        pass
        self.save()
        self._emit("library_changed", None)
        return failed

    def purge_old_trash(self):
        days = float(self.cfg.get("storage.trash_days", 30))
        cutoff = time.time() - days * 86400
        old = [v["id"] for v in self.snapshot() if v.get("trashed_at") and v["trashed_at"] < cutoff]
        if old:
            self.delete_forever(old)

    # ---------------------------------------------------------------- storage
    def usage(self) -> dict:
        totals = {"clip": 0, "session": 0, "screenshot": 0, "import": 0, "linked": 0}
        for i in self.snapshot():
            k = "linked" if i.get("linked") else i["kind"]
            totals[k] = totals.get(k, 0) + int(i.get("size") or 0)
        buf = 0
        bdir = self.root / ".buffer"
        if bdir.exists():
            for p in bdir.rglob("*.ts"):
                try:
                    buf += p.stat().st_size
                except OSError:
                    pass
        try:
            du = shutil.disk_usage(self.root)
            disk = {"total": du.total, "free": du.free, "used": du.used}
        except OSError:
            disk = {"total": 0, "free": 0, "used": 0}
        own = sum(v for k, v in totals.items() if k != "linked")
        return {"by_kind": totals, "buffer": buf, "disk": disk, "library": own, "linked": totals["linked"]}

    def enforce_limit(self):
        """Storage limit only ever deletes ClipVault's own recordings — never linked (Medal/NVIDIA/OBS) files."""
        st = self.cfg.get("storage")
        limit = float(st.get("limit_gb", 0)) * 1024 ** 3
        if not limit or not st.get("auto_delete"):
            return []
        used = self.usage()["library"]
        if used <= limit:
            return []
        cands = [i for i in self.snapshot() if not i.get("favorite") and not i.get("linked")]
        if st.get("only_delete_sessions", True):
            cands = [i for i in cands if i["kind"] == "session"]
        cands.sort(key=lambda i: i["created"])
        removed = []
        for it in cands:
            if used <= limit:
                break
            used -= int(it.get("size") or 0)
            removed.append(it["id"])
        self.delete_forever(removed)
        return removed

    def import_files(self, paths: list[str]) -> int:
        """Import button: index files from anywhere (they stay where they are)."""
        added = []
        with self._lock:
            known = {norm(i["path"]) for i in self.items.values()}
        for p in paths:
            pp = Path(p)
            if norm(pp) in known or not pp.exists():
                continue
            ext = pp.suffix.lower()
            if ext not in VIDEO_EXT and ext not in IMAGE_EXT:
                continue
            kind = "screenshot" if ext in IMAGE_EXT else "clip"
            meta = {"created": parse_created(pp.name) or pp.stat().st_mtime, "title": "", "linked": True,
                    "source": guess_source(pp.name)}
            g = display_game(pp.parent.name) if pp.parent.name.casefold() not in GENERIC_PARTS else "Imported"
            it = self._new_item(pp, kind, g, meta)
            with self._lock:
                self.items[it["id"]] = it
                self.excluded.discard(norm(pp))
            added.append(it)
            known.add(norm(pp))
        for it in added:
            self._enqueue(it["id"], 1)
        if added:
            self.save()
            self._emit("library_changed", None)
        return len(added)

    def rescan(self, busy: set[str] | None = None) -> int:
        return self.scan(busy).get("added", 0)
