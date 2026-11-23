"""The recording engine.

One ffmpeg process per "run" captures the display (ddagrab) + audio pipes and writes
2-second MPEG-TS segments into <storage>/.buffer/<run>/.  Everything is built on that
rolling buffer:

  * Clip (F8):          concat the segments covering [now - N s, now] -> mp4 (stream copy, instant)
  * Long recording:     capture switches to writing ONE crash-safe MP4 directly (no pieces, nothing to
                        join afterwards - it's finished the moment capture stops). Clips pressed during a
                        long recording are cut out of that growing file. A session_*.json marker on disk
                        lets the next start finish the file if ClipVault or the PC went down.
  * Bookmarks:          timestamps attached to the active long recording
  * Screenshot:         separate one-shot ddagrab grab (HDR-fixed too)

Segment times come from ffmpeg's segment list and are wall-clock based (every input
uses wall-clock timestamps offset by the run's t0), so a hotkey press maps exactly to
a point in the buffer even across ffmpeg restarts.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import logging

from . import ffmpeg as ff
from . import hdr as hdrmod
from .audio import AudioEngine, PipeFeeder


log = logging.getLogger("clipvault.recorder")

EXPORT_STALL_S = 300      # an export whose output hasn't grown for this long is stuck (no limit on total time)
CAPTURE_STALL_S = 45      # ffmpeg alive but nothing written for this long -> restart capture
CAPTURE_STALL_SILENT_S = 180  # same, when there's no audio track (a still screen writes nothing at all)


class Events:
    def __init__(self):
        self._subs = []

    def subscribe(self, fn):
        self._subs.append(fn)

    def emit(self, name, payload=None):
        for fn in list(self._subs):
            try:
                fn(name, payload)
            except Exception:
                pass


@dataclass
class Segment:
    path: Path
    start: float      # relative (seconds since run.t0) — what ffmpeg/concat use
    end: float
    run: "Run"

    @property
    def abs_start(self):
        return self.run.t0 + self.start

    @property
    def abs_end(self):
        return self.run.t0 + self.end


@dataclass
class Run:
    dir: Path
    t0: float
    proc: object = None
    plan: object = None
    feeders: list = field(default_factory=list)
    csv_rows: int = 0
    started: float = 0.0
    log: deque = field(default_factory=lambda: deque(maxlen=200))
    listed: set = field(default_factory=set)       # segment files ffmpeg has finished (from segments.csv)
    grow_key: tuple = ()                           # (newest file, size) at the last progress check
    grow_at: float = 0.0
    grow_check: float = 0.0
    ended: float = 0.0
    clean: bool = False                            # ffmpeg closed its output properly
    tidy: bool = False                             # written as a plain fragmented MP4: remux when done
    # long-recording runs (one growing MP4 instead of pieces):
    session_part: Path | None = None               # the file while it's being written
    session_file: Path | None = None               # where it ended up
    frags: list = field(default_factory=list)      # (byte offset, media time in s) of each finished chunk
    scan_off: int = 0
    data_end: int = 0
    vtrack: int = 0
    timescale: int = 0
    pending: tuple | None = None
    frag_seen: float = 0.0
    abs0: float | None = None                      # wall-clock time of media time 0 (estimated)

    @property
    def zero(self) -> float:
        return self.abs0 if self.abs0 is not None else self.started + 1.0

    @property
    def media_end(self) -> float:
        if not self.frags:
            return 0.0
        last = self.frags[-1][1]
        return last + (last - self.frags[-2][1] if len(self.frags) > 1 else 1.0)


def _boxes(buf: bytes, start: int, end: int):
    """Yield (type, body start, box end) for the MP4 boxes in buf[start:end]."""
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, off)
        hdr = 8
        if size == 1:
            if off + 16 > end:
                return
            size, hdr = struct.unpack_from(">Q", buf, off + 8)[0], 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            return
        yield typ, off + hdr, off + size
        off += size


def _video_track(moov: bytes) -> tuple[int, int] | None:
    """(track id, timescale) of the video track."""
    for typ, a, b in _boxes(moov, 0, len(moov)):
        if typ != b"trak":
            continue
        tid = scale = 0
        video = False
        for t2, a2, b2 in _boxes(moov, a, b):
            if t2 == b"tkhd":
                tid = struct.unpack_from(">I", moov, a2 + (20 if moov[a2] == 1 else 12))[0]
            elif t2 == b"mdia":
                for t3, a3, _ in _boxes(moov, a2, b2):
                    if t3 == b"mdhd":
                        scale = struct.unpack_from(">I", moov, a3 + (20 if moov[a3] == 1 else 12))[0]
                    elif t3 == b"hdlr":
                        video = moov[a3 + 8:a3 + 12] == b"vide"
        if video and tid and scale:
            return tid, scale
    return None


def _frag_time(moof: bytes, tid: int) -> int | None:
    """Decode time (in track units) at which this chunk's video starts."""
    for typ, a, b in _boxes(moof, 0, len(moof)):
        if typ != b"traf":
            continue
        this, t = None, None
        for t2, a2, _ in _boxes(moof, a, b):
            if t2 == b"tfhd":
                this = struct.unpack_from(">I", moof, a2 + 4)[0]
            elif t2 == b"tfdt":
                t = struct.unpack_from(">Q" if moof[a2] == 1 else ">I", moof, a2 + 4)[0]
        if this == tid and t is not None:
            return t
    return None


def _move(src: Path, dst: Path, tries: int = 120) -> bool:
    """Rename, waiting out a reader that still has the file open (a clip being cut from it)."""
    for _ in range(tries):
        try:
            os.replace(src, dst)
            return True
        except OSError:
            time.sleep(0.5)
    return False


def _sig(run: "Run") -> str:
    return json.dumps(getattr(run.plan, "signature", None), default=list)


def _probe_duration(path: Path) -> float:
    try:
        r = ff.run([ff.ffprobe_path(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                    str(path)], timeout=30)
        return max(0.0, float(r.stdout.strip().split(",")[0]))
    except Exception:
        return 0.0


def _write_json(path: Path, data: dict):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, default=list), "utf-8")
    os.replace(tmp, path)


@dataclass
class LongRecording:
    id: str
    start: float
    game: str
    bookmarks: list = field(default_factory=list)
    clip_ids: list = field(default_factory=list)
    out: Path | None = None                        # final file name
    parts: list = field(default_factory=list)      # capture runs that wrote it (more than one after a restart)


class Recorder:
    def __init__(self, cfg, library, audio: AudioEngine, alerts, events: Events):
        self.cfg, self.library, self.audio, self.alerts, self.events = cfg, library, audio, alerts, events
        self.segments: list[Segment] = []
        self.run: Run | None = None
        self.state = "idle"          # idle | buffering
        self.long: LongRecording | None = None
        self.game: str | None = None
        self.monitor: dict | None = None
        self.display_hdr: hdrmod.DisplayHDR | None = None
        self.pins: dict[str, float] = {}   # id -> abs time to keep segments from
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._want_running = False
        self._force_cpu_path = False
        self._fail_count = 0
        self.last_error = ""
        self.status_line = ""
        self._jobs = 0
        self._jobs_lock = threading.Lock()
        self._launching = False
        self._reserved: set[str] = set()
        self.sessions: list[Run] = []      # finished long-recording files still inside the clip window
        self._session_level = 0            # 0: hybrid MP4, 1: plain fragmented MP4 (if 0 won't start), 2: gave up
        # now, before anything records: what did a crash leave behind? (finished in the background)
        try:
            leftovers = self._find_leftovers()
        except Exception:
            log.exception("crash recovery scan failed")
            leftovers = ([], [])
        self._cleanup_stale_buffers()
        self._watch = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch.start()
        if leftovers[0] or leftovers[1]:
            self._job(lambda: self._finish_leftovers(*leftovers))

    # ------------------------------------------------------------------ paths
    @property
    def buffer_root(self) -> Path:
        p = self.cfg.storage / ".buffer"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _cleanup_stale_buffers(self):
        for pat in ("run_*", "cut_*", "salvage_*"):
            for d in self.buffer_root.glob(pat):
                shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------------ control
    def set_game(self, game: str | None):
        self.game = game
        self.events.emit("game", game)

    def start(self):
        """Non-blocking: first launch probes monitors/encoders (a few seconds), so do it off the caller's thread."""
        with self._lock:
            self._want_running = True
            if self.run is not None or self._launching:
                return
            self._launching = True
            self.state = "starting"
        self.events.emit("state", self.state)

        def go():
            try:
                with self._lock:
                    if self._want_running and self.run is None:
                        self._launch()
            except Exception as e:
                log.exception("launch failed")
                self.last_error = str(e)
                self.events.emit("error", f"Couldn't start recording: {e}")
            finally:
                self._launching = False
                if self.run is None and self.state == "starting":
                    self.state = "idle"
                    self.events.emit("state", self.state)
        threading.Thread(target=go, daemon=True, name="RecorderStart").start()

    def stop(self, reason: str = ""):
        with self._lock:
            self._want_running = False
            if self.long:
                self.stop_long_recording(reason, resume=False)
            self._kill_run()
            self.state = "idle"
        self.events.emit("state", self.state)

    def restart(self):
        """Apply new settings: restart capture (segments already on disk are kept for clips)."""
        def go():
            with self._lock:
                if self.run is None:
                    return
                self._kill_run()
                self._force_cpu_path = False
                self._fail_count = 0
                self._session_level = 0
                self._launch()
        threading.Thread(target=go, daemon=True, name="RecorderRestart").start()

    def busy(self) -> bool:
        return self._jobs > 0

    def _job(self, fn):
        with self._jobs_lock:
            self._jobs += 1  # counted before the thread starts, so busy() is never falsely idle

        def wrap():
            try:
                fn()
            except Exception:
                log.exception("export job failed")
            finally:
                with self._jobs_lock:
                    self._jobs -= 1
        threading.Thread(target=wrap, daemon=True).start()

    def shutdown(self, wait: float | None = None):
        """Finish everything in progress. Waits for clip/session exports (no timeout by default —
        a multi-hour session on a slow drive must not be cut off and its buffer deleted)."""
        if self.long:
            self.stop_long_recording(resume=False)
        deadline = time.time() + wait if wait else None
        while self._jobs and (deadline is None or time.time() < deadline):
            time.sleep(0.2)
        self._stop.set()
        self.stop()
        if not self._jobs:
            self._cleanup_stale_buffers()

    # ------------------------------------------------------------------ run lifecycle
    def _launch(self):
        rec = self.cfg.get("recording")
        mons = getattr(self, "_mons", None)
        if not mons:
            self._mons = mons = ff.probe_monitors()
        idx = int(rec.get("monitor", 0))
        self.monitor = next((m for m in mons if m["idx"] == idx), mons[0])
        self.display_hdr = hdrmod.display_state(ff.gdi_name_for_monitor(self.monitor))

        run_dir = self.buffer_root / f"run_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time() - 1.0
        run = Run(dir=run_dir, t0=t0, started=time.time())
        long = self.long
        if long is not None and long.out is not None:
            run.session_part = long.out.with_name(f"{long.out.stem}.p{len(long.parts) + 1}{long.out.suffix}.part")

        self.audio.acquire()
        audio_inputs = []
        tag = uuid.uuid4().hex[:6]
        for kind, src in (("system", self.audio.system), ("mic", self.audio.mic)):
            if src is None:
                continue
            feeder = PipeFeeder(src, f"clipvault_{kind}_{tag}")
            feeder.start()
            run.feeders.append(feeder)
            audio_inputs.append({"pipe": feeder.path, "rate": src.rate, "kind": kind})

        plan = ff.build_record_command(self.cfg, self.monitor, audio_inputs, run_dir, t0, self.display_hdr,
                                       force_cpu_path=self._force_cpu_path, session_out=run.session_part,
                                       session_plain=self._session_level >= 1)
        run.plan = plan
        run.tidy = run.session_part is not None and (self._session_level >= 1 or "hybrid" not in ff.session_movflags())
        try:
            run.proc = ff.popen(plan.args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, errors="replace")
        except Exception as e:
            self.last_error = f"Could not start ffmpeg: {e}"
            log.error(self.last_error)
            for f in run.feeders:
                f.stop()
            self.audio.release()
            self.state = "error"
            self.events.emit("state", self.state)
            self.events.emit("error", self.last_error)
            return
        threading.Thread(target=self._read_log, args=(run,), daemon=True).start()
        self.run = run
        if run.session_part is not None:
            long.parts.append(run)
            self._save_marker(long)
        self.state = "buffering"
        self.status_line = plan.description
        log.info("capture started: %s", plan.description)
        self.events.emit("state", self.state)
        self.events.emit("status", plan.description)

    def _read_log(self, run: Run):
        try:
            for line in run.proc.stderr:
                line = line.rstrip()
                if line:
                    run.log.append(line)
        except Exception:
            pass

    def _kill_run(self):
        run, self.run = self.run, None
        if not run:
            return
        try:
            if run.proc and run.proc.poll() is None:
                try:
                    run.proc.stdin.write("q")
                    run.proc.stdin.flush()
                except Exception:
                    pass
                try:  # a long recording writes its index on close: give it time
                    run.proc.wait(timeout=30 if run.session_part else 4)
                except Exception:
                    run.proc.kill()
                    try:
                        run.proc.wait(timeout=4)
                    except Exception:
                        pass
        finally:
            for f in run.feeders:
                f.stop()
            self.audio.release()
            self._end_run(run)

    def _end_run(self, run: Run):
        run.ended = time.time()
        run.clean = bool(run.proc) and run.proc.returncode == 0
        if run.session_part is not None and (run.tidy or not run.clean):
            self._scan_session(run)  # (the file's layout is unchanged: pick up the last chunks)
        if run.session_part is None:
            self._adopt_tail(run)
            return
        run.session_file = run.session_part  # (not indexed any further: closing rewrites the file's layout)
        if run.clean and not run.tidy and run.frags:
            self.sessions.append(run)

    # ------------------------------------------------------------------ watcher
    def _watch_loop(self):
        while not self._stop.wait(0.5):
            try:
                self._tick()
            except Exception as e:  # never let the watcher die
                self.last_error = f"watcher: {e}"

    def _tick(self):
        with self._lock:
            run = self.run
            if run:
                if run.session_part is not None:
                    self._scan_session(run)
                else:
                    self._ingest_csv(run)
                if run.proc and run.proc.poll() is not None:
                    self._on_crash(run)
                else:
                    self._check_stall(run)
            self._prune()

    def _check_stall(self, run: Run):
        """ffmpeg can hang instead of exiting (e.g. the GPU driver reset when a game crashed). If nothing
        has been written for a while, kill it - the watcher then restarts capture like after any crash."""
        now = time.time()
        if now - run.grow_check < 5:
            return
        run.grow_check = now
        try:
            newest = run.session_part or max(run.dir.glob("seg_*.ts"), default=None)
            key = (newest.name, newest.stat().st_size) if newest else ()
        except OSError:
            return
        if key != run.grow_key or not run.grow_at:
            run.grow_key, run.grow_at = key, now
            return
        # audio keeps the current segment growing even on a still screen; without audio allow much longer
        limit = CAPTURE_STALL_S if run.feeders else CAPTURE_STALL_SILENT_S
        if now - run.grow_at > limit and now - run.started > 30:
            log.warning("capture stalled (nothing written for %.0fs) - restarting ffmpeg", now - run.grow_at)
            run.grow_at = now
            try:
                run.proc.kill()
            except Exception:
                pass

    def _scan_session(self, run: Run):
        """Index the long-recording file as it grows: where each finished ~1 s chunk starts and what time
        it holds. That's what lets a clip be cut out of the middle of a file that's still being written."""
        try:
            size = run.session_part.stat().st_size
            if size <= run.scan_off:
                return
            now = time.time()
            with open(run.session_part, "rb") as f:
                off = run.scan_off
                while off + 16 <= size:
                    f.seek(off)
                    head = f.read(16)
                    box, typ = struct.unpack_from(">I4s", head)
                    hdr = 8
                    if box == 1:
                        box, hdr = struct.unpack_from(">Q", head, 8)[0], 16
                    if box < hdr or off + box > size:
                        break  # not completely written yet
                    if typ == b"moov" and not run.timescale:
                        f.seek(off + hdr)
                        run.vtrack, run.timescale = _video_track(f.read(box - hdr)) or (0, 0)
                    elif typ == b"moof" and run.timescale:
                        f.seek(off + hdr)
                        t = _frag_time(f.read(box - hdr), run.vtrack)
                        run.pending = (off, t / run.timescale) if t is not None else None
                    elif typ == b"mdat" and run.pending:
                        start, t = run.pending
                        run.pending = None
                        if run.frags:
                            # a chunk is written out the moment the next one's first frame arrives, so
                            # "when the previous chunk showed up" - "this chunk's time" = the clock time of 0
                            est = run.frag_seen - t
                            run.abs0 = est if run.abs0 is None else min(run.abs0, est)
                        run.frags.append((start, t))
                        run.frag_seen = now
                        run.data_end = off + box
                    off += box
                run.scan_off = off
        except OSError:
            pass

    def _ingest_csv(self, run: Run):
        f = run.dir / "segments.csv"
        if not f.exists():
            return
        try:
            rows = list(csv.reader(f.read_text("utf-8", errors="replace").splitlines()))
        except OSError:
            return
        for row in rows[run.csv_rows:]:
            if len(row) < 3:
                continue
            try:
                seg = Segment(run.dir / row[0], float(row[1]), float(row[2]), run)
            except ValueError:
                continue
            if row[0] in run.listed:
                continue
            run.listed.add(row[0])
            self.segments.append(seg)
        run.csv_rows = len(rows)

    @staticmethod
    def _tail_segments(run: Run, listed_end: float | None) -> list[Segment]:
        """Segment files ffmpeg never finished (it crashed or was killed mid-segment). A cut-off .ts still
        plays up to the cut, so keep them instead of losing the last seconds before a crash."""
        out = []
        last = listed_end
        for f in sorted(run.dir.glob("seg_*.ts")):
            if f.name in run.listed:
                continue
            try:
                if f.stat().st_size < 4096:
                    continue
            except OSError:
                continue
            dur = _probe_duration(f)
            if dur <= 0.05:
                continue
            start = last if last is not None else 0.0
            out.append(Segment(f, start, start + dur, run))
            run.listed.add(f.name)
            last = start + dur
        return out

    def _adopt_tail(self, run: Run):
        self._ingest_csv(run)
        ends = [s.end for s in self.segments if s.run is run]
        self.segments.extend(self._tail_segments(run, max(ends) if ends else None))

    def _on_crash(self, run: Run):
        tail = "\n".join(list(run.log)[-12:])
        lived = time.time() - run.started
        self.run = None
        for f in run.feeders:
            f.stop()
        self.audio.release()
        try:
            self._end_run(run)
        except Exception:
            log.exception("couldn't rescue the last segment")
        self.last_error = tail or f"ffmpeg exited with code {run.proc.returncode}"
        log.error("ffmpeg exited (code %s after %.1fs). Command: %s\n%s", run.proc.returncode, lived,
                  " ".join(run.plan.args) if run.plan else "?", tail)
        self.events.emit("log", tail)
        if not self._want_running:
            return
        if run.session_part is not None and lived < 20 and not run.frags and self.long is not None \
                and not (lived < 6 and run.plan and run.plan.zero_copy and not self._force_cpu_path):
            # writing the long recording straight to its file doesn't work here: try the simpler format,
            # and if that fails too, keep the clip buffer working instead of failing over and over
            self._session_level += 1
            log.warning("long-recording capture failed to start (level %d)", self._session_level)
            if self._session_level >= 2:
                long, self.long = self.long, None
                self._marker(long).unlink(missing_ok=True)
                for r in long.parts:
                    if r.session_part:
                        r.session_part.unlink(missing_ok=True)
                with self._jobs_lock:
                    self._reserved.discard(str(long.out))
                self.events.emit("long_recording", False)
                self.events.emit("error", "The long recording couldn't start on this PC (see Settings → Advanced → "
                                          "Diagnostics). Clips still work.")
        if lived < 6 and run.plan and run.plan.zero_copy and not self._force_cpu_path:
            self._force_cpu_path = True  # zero-copy path not supported by this driver/build -> filtered path
            self.events.emit("status", "GPU zero-copy not supported here — switching to compatible capture path")
        else:
            self._fail_count = self._fail_count + 1 if lived < 20 else 1
        if self._fail_count >= 5:
            self.state = "error"
            self.events.emit("state", self.state)
            self.events.emit("error", "Capture keeps failing. Open Settings → Advanced → Diagnostics for the ffmpeg log.")
            self._want_running = False
            return
        time.sleep(min(5, 0.5 * self._fail_count + 0.5))
        try:
            self._launch()
        except Exception as e:
            log.exception("relaunch failed")
            self.last_error = str(e)
            self.state = "error"
            self.events.emit("state", self.state)
            self.events.emit("error", f"Couldn't restart recording: {e}")

    def _prune(self):
        keep_s = max(int(self.cfg.get("recording.buffer_seconds", 300)), int(self.cfg.get("recording.clip_seconds", 60)),
                     int(self.cfg.get("recording.clip_alt_seconds", 0)) if self.cfg.get("hotkeys.clip_alt") else 0)
        cutoff = time.time() - keep_s - 6
        pins = list(self.pins.values())
        if pins:
            cutoff = min(cutoff, min(pins) - 6)
        keep = []
        for s in self.segments:
            if s.abs_end < cutoff:
                try:
                    s.path.unlink(missing_ok=True)
                except OSError:
                    keep.append(s)
                    continue
            else:
                keep.append(s)
        self.segments = keep
        self.sessions = [r for r in self.sessions if r.ended >= cutoff]  # (only forgets them; never deletes)
        live_dirs = {s.run.dir for s in self.segments}
        if self.run:
            live_dirs.add(self.run.dir)
        for d in self.buffer_root.glob("run_*"):
            if d not in live_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def _session_runs(self) -> list[Run]:
        runs = list(self.sessions)
        if self.run is not None and self.run.session_part is not None and self.run.frags:
            runs.append(self.run)
        return runs

    @staticmethod
    def _span(run: Run) -> tuple[float, float]:
        """Wall-clock (start, end) of what a long-recording file holds."""
        return run.zero + run.frags[0][1], run.ended or run.zero + run.media_end

    def buffered_seconds(self) -> float:
        spans = [(s.abs_start, s.abs_end) for s in self.segments] + [self._span(r) for r in self._session_runs()]
        if not spans:
            return 0.0
        return max(0.0, max(e for _, e in spans) - min(st for st, _ in spans))

    # ------------------------------------------------------------------ export from buffer
    def _collect(self, a: float, b: float, timeout: float = 30.0) -> list:
        """What covers wall-clock [a, b], oldest first: buffer pieces (Segment) and long-recording files (Run).
        Waits for the part that's still being written."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                run = self.run
                if run and run.session_part is not None:
                    self._scan_session(run)
                    done = bool(run.frags) and self._span(run)[1] >= b
                elif run:
                    self._ingest_csv(run)
                    done = any(s.abs_end >= b for s in self.segments)
                else:
                    done = True
            if done:
                break
            time.sleep(0.25)
        with self._lock:
            items = [(s.abs_start, s) for s in self.segments if s.abs_end > a and s.abs_start < b]
            for r in self._session_runs():
                st, en = self._span(r)
                if en > a and st < b:
                    items.append((st, r))
        items.sort(key=lambda it: it[0])
        # settings changed mid-buffer (new encoder/resolution/HDR mode) -> those can't be stream-copied
        # together; keep the newest compatible stretch
        out = []
        for _, it in reversed(items):
            run = it if isinstance(it, Run) else it.run
            if out and _sig(run) != _sig(out[-1] if isinstance(out[-1], Run) else out[-1].run):
                break
            out.append(it)
        return list(reversed(out))

    def _export_range(self, a: float, b: float, out: Path, timeout: float = 30.0) -> bool:
        """Save wall-clock [a, b] as a clip (stream copy)."""
        tmp = None
        try:
            items = self._collect(a, b, timeout)
            if not items:
                self.last_error = "nothing in the buffer for that time"
                return False
            lines = []
            for i, it in enumerate(items):
                first, last = i == 0, i == len(items) - 1
                if isinstance(it, Run):
                    if tmp is None:
                        tmp = self.buffer_root / f"cut_{uuid.uuid4().hex[:8]}"
                        tmp.mkdir(parents=True, exist_ok=True)
                    cut = tmp / f"{i:03d}.ts"
                    if not self._cut_session(it, a, b, cut):
                        continue
                    lines.append(f"file '{cut.as_posix()}'")
                    if last and it.ended == 0 and b - it.zero > 0:  # live file: the cut runs to "now"
                        lines.append(f"outpoint {b - it.zero:.3f}")
                else:
                    safe_path = it.path.as_posix().replace("'", "'\\''")
                    lines.append(f"file '{safe_path}'")
                    if first and a > it.abs_start:
                        lines.append(f"inpoint {a - it.run.t0:.3f}")
                    if last and b < it.abs_end:
                        lines.append(f"outpoint {b - it.run.t0:.3f}")
            if not lines:
                self.last_error = self.last_error or "couldn't read the recording"
                return False
            plan = (items[-1] if isinstance(items[-1], Run) else items[-1].run).plan
            return self._concat(lines, out, plan)
        except Exception as e:
            self.last_error = str(e)
            log.exception("export failed (%s)", out.name)
            return False
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            with self._jobs_lock:
                self._reserved.discard(str(out))

    def _cut_session(self, run: Run, a: float, b: float, out_ts: Path) -> bool:
        """Copy wall-clock [a, b] out of a long-recording file into a .ts piece (starts on the keyframe
        at or before a). Works on the file that's still being written."""
        common = ["-map", "0", "-c", "copy", "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", str(out_ts)]
        if run.ended:  # finished file: a normal, seekable MP4
            src = run.session_file
            if src is None or not src.exists():
                return False
            st = self._span(run)[0]
            args = [ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-ss", f"{max(0.0, a - st):.3f}",
                    "-t", f"{b - max(a, st) + 0.5:.3f}", "-i", str(src)] + common
            r = ff.run(args, timeout=600)
            if r.returncode != 0:
                self.last_error = r.stderr[-400:]
            return r.returncode == 0 and out_ts.exists()
        with self._lock:
            frags, end, zero = list(run.frags), run.data_end, run.zero
        if not frags:
            return False
        ta, tb = a - zero, b - zero
        i0 = max((i for i, (_, t) in enumerate(frags) if t <= ta), default=0)
        stop = next((off for off, t in frags if t >= tb + 0.5), end)
        args = [ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-copyts", "-f", "mp4", "-i", "pipe:0"] + common
        errf = out_ts.with_suffix(".log")
        with open(errf, "wb") as ef:
            proc = ff.popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=ef)
            try:
                with open(run.session_part, "rb") as f:
                    for start, length in ((0, frags[0][0]), (frags[i0][0], stop - frags[i0][0])):  # header, then the chunks
                        f.seek(start)
                        while length > 0:
                            chunk = f.read(min(1 << 20, length))
                            if not chunk:
                                break
                            proc.stdin.write(chunk)
                            length -= len(chunk)
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        ok = proc.returncode == 0 and out_ts.exists() and out_ts.stat().st_size > 0
        if not ok:
            self.last_error = errf.read_text("utf-8", "replace")[-400:]
        return ok

    def _concat(self, lines: list[str], out: Path, plan) -> bool:
        lst = out.with_name(out.name + ".concat.txt")
        part = out.with_name(out.name + ".part")   # never a half-written .mp4 lying around
        try:
            lst.write_text("\n".join(lines) + "\n", "utf-8")
            args = ff.concat_command(lst, part, plan.audio_titles if plan else None,
                                     hdr10=bool(plan and plan.hdr_mode == "hdr10"))
            ok, err = self._run_watched(args, part)
            if ok and part.exists() and part.stat().st_size > 0:
                os.replace(part, out)
                return True
            self.last_error = err or "export failed"
            log.error("export failed (%s): %s", out.name, self.last_error[-800:])
        finally:
            lst.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
        return False

    @staticmethod
    def _run_watched(args: list[str], growing: Path) -> tuple[bool, str]:
        """Run an export with no overall time limit - only give up if the output file stops growing."""
        proc = ff.popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        text=True, errors="replace")
        err: deque = deque(maxlen=60)
        reader = threading.Thread(target=lambda: [err.append(l.rstrip()) for l in proc.stderr], daemon=True)
        reader.start()
        size, moved = -1, time.time()
        while proc.poll() is None:
            time.sleep(0.2)
            try:
                cur = growing.stat().st_size
            except OSError:
                cur = -1
            if cur != size:
                size, moved = cur, time.time()
            elif time.time() - moved > EXPORT_STALL_S:
                proc.kill()
                proc.wait()
                reader.join(2)
                return False, f"export stopped making progress for {EXPORT_STALL_S // 60} minutes"
        reader.join(5)
        return proc.returncode == 0, "\n".join(err)

    def _name(self, kind: str, game: str | None, ext: str, when: float | None = None, note: str = "") -> Path:
        stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H-%M-%S") if when else datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        safe = "".join(c for c in (game or "Desktop") if c not in '<>:"/\\|?*').strip() or "Desktop"
        folder = self.library.folder(kind, game)
        with self._jobs_lock:
            out, n = folder / f"{safe} {stamp}{note}{ext}", 2
            while out.exists() or str(out) in self._reserved:
                out, n = folder / f"{safe} {stamp}{note} ({n}){ext}", n + 1
            self._reserved.add(str(out))
        return out

    # ------------------------------------------------------------------ long recordings: finish / recover
    def _marker(self, long: LongRecording) -> Path:
        return self.buffer_root / f"session_{long.id}.json"

    def _save_marker(self, long: LongRecording, end: float | None = None):
        """On disk while a long recording isn't safely in the library yet (see _find_leftovers)."""
        try:
            _write_json(self._marker(long), {
                "id": long.id, "start": long.start, "game": long.game, "end": end, "bookmarks": long.bookmarks,
                "parts": [{"part": str(r.session_part), "abs0": r.abs0} for r in long.parts]})
        except OSError:
            log.exception("couldn't write session marker")

    def _finish_session(self, info: dict, parts: list[dict], out: Path, recovered: bool = False) -> list[dict]:
        """Turn the file(s) capture wrote into library items. parts: [{'part': Path, 'clean': bool, 'abs0': float}]"""
        good = []
        for p in parts:
            try:
                if p["part"].stat().st_size > 100_000:
                    good.append(p)
                    continue
            except OSError:
                continue
            p["part"].unlink(missing_ok=True)
        items = []
        finals = [out] if len(good) == 1 else [out.with_name(f"{out.stem} (part {i}){out.suffix}")
                                                for i in range(1, len(good) + 1)]
        for i, (p, final) in enumerate(zip(good, finals)):
            done = False
            if not p.get("clean"):
                # capture was cut off (crash, power loss): the file plays, but tidy it into a normal MP4
                fix = final.with_name(final.name + ".fix.part")
                ok, err = self._run_watched([ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-i", str(p["part"]),
                                             "-map", "0", "-c", "copy", "-f", "mp4", str(fix)], fix)
                if ok and fix.exists() and fix.stat().st_size > 100_000 and _move(fix, final, 20):
                    p["part"].unlink(missing_ok=True)
                    done = True
                else:
                    fix.unlink(missing_ok=True)
                    log.warning("couldn't tidy %s (%s) - keeping it as recorded", p["part"].name, err[-200:])
            if not done and not _move(p["part"], final):
                log.error("couldn't move %s into place", p["part"].name)
                continue
            if p.get("run") is not None:
                p["run"].session_file = final
            marks = []
            if i == 0:
                lead = info["start"] - p["abs0"] if p.get("abs0") else 0.0   # the file starts when capture did
                marks = [dict(b, t=max(0.0, b["t"] + lead)) for b in info.get("bookmarks") or []]
            items.append(self.library.add(final, "session", info["game"], bookmarks=marks, session_id=info["id"],
                                          created=info["start"], ended_by="recovered" if recovered else info.get("ended_by", ""),
                                          hdr=info.get("hdr")))
        return items

    def _find_leftovers(self) -> tuple[list, list]:
        """Startup: long recordings a crash left unfinished (their marker is still there), and junk files
        from exports that were cut off."""
        sessions, claimed = [], set()
        for m in sorted(self.buffer_root.glob("session_*.json")):
            try:
                info = json.loads(m.read_text("utf-8"))
                parts = [{"part": Path(p["part"]), "clean": False, "abs0": p.get("abs0")} for p in info.get("parts", [])]
            except (OSError, ValueError, KeyError, TypeError):
                info, parts = None, []
            parts = [p for p in parts if p["part"].exists()]
            if info and parts:
                sessions.append((m, info, parts))
                claimed.update(str(p["part"]) for p in parts)
            else:
                m.unlink(missing_ok=True)
        junk = []
        for sub in ("Sessions", "Clips"):
            base = self.cfg.storage / sub
            if base.exists():
                for pat in ("*.part", "*.concat.txt"):
                    junk += [f for f in base.rglob(pat) if str(f) not in claimed]
        return sessions, junk

    def _finish_leftovers(self, sessions: list, junk: list):
        for f in junk:
            try:
                f.unlink()
            except OSError:
                pass
        for marker, info, parts in sessions:
            game = info.get("game") or "Desktop"
            out = self._name("session", game, ".mp4", when=info.get("start"), note=" (recovered)")
            try:
                items = self._finish_session(dict(info, game=game), parts, out, recovered=True)
            finally:
                with self._jobs_lock:
                    self._reserved.discard(str(out))
            if items:
                marker.unlink(missing_ok=True)
                self.events.emit("toast", ("Long recording recovered", f"{game} · {items[0]['path'] and Path(items[0]['path']).name}"))
                log.info("recovered long recording: %s", ", ".join(Path(i["path"]).name for i in items))

    def _overlay(self, kind: str, title: str, sub: str = ""):
        """In-game pop-up (shown by the UI on the monitor being recorded)."""
        try:
            screen = ff.gdi_name_for_monitor(self.monitor) if self.monitor else None
        except Exception:
            screen = None
        self.events.emit("overlay", {"kind": kind, "title": title, "sub": sub, "screen": screen})

    def clip(self, seconds: int | None = None):
        """Save the last N seconds. Runs in a background thread; emits clip_saved / error."""
        if self.state != "buffering" and not self.segments and not self.sessions:
            self.events.emit("toast", ("Not recording", "Start the buffer (or launch a game) before clipping."))
            self.alerts.play("error")
            self._overlay("error", "Not recording", "Nothing to clip yet")
            return
        seconds = int(seconds or self.cfg.get("recording.clip_seconds", 60))
        at = time.time()
        pin = uuid.uuid4().hex
        self.pins[pin] = at - seconds
        game = self.game or (self.long.game if self.long else None)
        long_at_press = self.long
        if long_at_press:  # mark the moment in the long recording now (it may be saved before the clip is)
            long_at_press.bookmarks.append({"t": at - long_at_press.start, "label": "Clip", "clip": True})
            self._save_marker(long_at_press)
        self.alerts.play("clip")
        self.events.emit("toast", ("Saving clip…", f"Last {seconds}s"))
        self._overlay("clip", "Clip saved", (f"Last {seconds} s" if seconds < 120 else f"Last {seconds // 60} min")
                      + (f" · {game}" if game else ""))

        def work():
            out = self._name("clip", game, ".mp4")
            try:
                ok = self._export_range(at - seconds, at, out)
            finally:
                self.pins.pop(pin, None)
            if ok:
                meta = {"hdr": self.run.plan.hdr_mode if self.run and self.run.plan else None}
                lg = long_at_press
                if lg:
                    meta["session_id"] = lg.id
                item = self.library.add(out, "clip", game, **meta)
                if lg:
                    lg.clip_ids.append(item["id"])
                    if lg is not self.long:  # session already finished saving — link the clip directly
                        self.library.update(item["id"], session_id=lg.id)
                self.events.emit("clip_saved", item)
                self.events.emit("toast", ("Clip saved", out.name))
                self.library.enforce_limit()
            else:
                self.alerts.play("error")
                self._overlay("error", "Clip failed", "Open ClipVault for details")
                self.events.emit("error", "Couldn't save clip — " + (self.last_error.splitlines()[-1] if self.last_error else "buffer empty"))
        self._job(work)

    def toggle_long_recording(self):
        if self.long:
            self.stop_long_recording()
        else:
            self.start_long_recording()

    def split_long_recording(self):
        """Hold-to-split: end the current session and immediately begin a new one."""
        if self.long:
            self.stop_long_recording("split", resume=False)
        self.start_long_recording()

    def start_long_recording(self):
        """Capture switches from the rolling buffer to writing the session's MP4 directly."""
        with self._lock:
            self._want_running = True
            long = LongRecording(id=uuid.uuid4().hex[:10], start=time.time(), game=self.game or "Desktop")
            long.out = self._name("session", long.game, ".mp4", when=long.start)
            self.long = long
            self._save_marker(long)
            if self.run is not None:  # already buffering: restart capture into the file (a ~1 s gap)
                try:
                    self._kill_run()
                    self._launch()
                except Exception as e:
                    log.exception("couldn't switch capture to the long recording")
                    self.last_error = str(e)
        if self.run is None:
            self.start()  # (a launch already under way picks the long recording up too)
        self.alerts.play("start")
        self._overlay("start", "Long recording started", long.game)
        self.events.emit("long_recording", True)
        self.events.emit("toast", ("Long recording started", long.game))

    def stop_long_recording(self, reason: str = "", resume: bool | None = None):
        """resume: go back to the rolling buffer afterwards (default: yes if capture is meant to stay on)."""
        with self._lock:
            long, self.long = self.long, None
            if not long:
                return
            end = time.time()
            self.alerts.play("stop")
            hdr_mode = self.run.plan.hdr_mode if self.run and self.run.plan else None
            if self.run is not None and self.run.session_part is not None:
                self._kill_run()  # closes the file: this is the whole "save"
                if (self._want_running if resume is None else resume):
                    try:
                        self._launch()
                    except Exception as e:
                        log.exception("couldn't restart the buffer")
                        self.last_error = str(e)
            self._save_marker(long, end)
        if reason != "split":
            self._overlay("stop", "Long recording saved", f"{_fmt(end - long.start)} · {long.game}")
        else:
            self._overlay("stop", "Session split", "New recording started")
        self.events.emit("long_recording", False)

        def work():
            info = {"id": long.id, "start": long.start, "game": long.game, "bookmarks": long.bookmarks,
                    "ended_by": reason, "hdr": hdr_mode}
            parts = [{"part": r.session_part, "clean": r.clean and not r.tidy, "abs0": r.abs0, "run": r} for r in long.parts]
            try:
                items = self._finish_session(info, parts, long.out)
            finally:
                with self._jobs_lock:
                    self._reserved.discard(str(long.out))
            if items:
                for cid in long.clip_ids:
                    self.library.update(cid, session_id=long.id)
                items[0]["duration"] = end - long.start
                self.events.emit("session_saved", items[0])
                self.events.emit("toast", ("Long recording saved", Path(items[0]["path"]).name))
                self._marker(long).unlink(missing_ok=True)
                self.library.enforce_limit()
            elif any(p["part"].exists() for p in parts):
                self.events.emit("error", "Couldn't finish the long recording — the file is kept and ClipVault "
                                          "will try again the next time it starts.")
            else:
                self._marker(long).unlink(missing_ok=True)
                self.events.emit("error", "Couldn't save the long recording — nothing was captured. "
                                          + (self.last_error or "")[-160:])
        self._job(work)

    def bookmark(self, label: str = ""):
        if not self.long:
            # no long recording: a bookmark becomes a short clip so the moment isn't lost
            self.events.emit("toast", ("Bookmark", "No long recording running — saving a clip instead"))
            self.clip()
            return
        t = time.time() - self.long.start
        self.long.bookmarks.append({"t": t, "label": label or f"Bookmark {len(self.long.bookmarks) + 1}"})
        self._save_marker(self.long)
        self.alerts.play("bookmark")
        self._overlay("bookmark", "Bookmarked", _fmt(t))
        self.events.emit("bookmark", t)
        self.events.emit("toast", ("Bookmarked", _fmt(t)))

    def screenshot(self):
        game = self.game
        out = self._name("screenshot", game, ".png")
        self.alerts.play("screenshot")
        self._overlay("screenshot", "Screenshot saved", game or "")

        def work():
            mon = self.monitor
            if not mon:
                mons = getattr(self, "_mons", None) or ff.probe_monitors()
                self._mons = mons
                idx = int(self.cfg.get("recording.monitor", 0))
                mon = next((m for m in mons if m["idx"] == idx), mons[0])
            state = self.display_hdr or hdrmod.display_state(ff.gdi_name_for_monitor(mon))
            r = ff.run(ff.screenshot_command(self.cfg, mon, out, state), timeout=20)
            if r.returncode == 0 and out.exists():
                item = self.library.add(out, "screenshot", game)
                self.events.emit("screenshot_saved", item)
                self.events.emit("toast", ("Screenshot saved", out.name))
            else:
                self.events.emit("error", "Screenshot failed — " + r.stderr[-300:])
        threading.Thread(target=work, daemon=True).start()

    def diagnostics(self) -> str:
        lines = [f"State: {self.state}", f"Game: {self.game}", f"Monitor: {self.monitor}",
                 f"Display HDR: {self.display_hdr}", f"Buffered: {self.buffered_seconds():.1f}s in {len(self.segments)} segments",
                 f"Pins: {list(self.pins)}", f"Last error: {self.last_error}"]
        if self.run and self.run.plan:
            lines += ["", "Pipeline: " + self.run.plan.description, "", "Command:",
                      " ".join(f'"{a}"' if " " in a else a for a in self.run.plan.args), "", "ffmpeg log:"]
            lines += list(self.run.log)[-40:]
        return "\n".join(lines)


def _fmt(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
