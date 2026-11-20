"""The recording engine.

One ffmpeg process per "run" captures the display (ddagrab) + audio pipes and writes
2-second MPEG-TS segments into <storage>/.buffer/<run>/.  Everything is built on that
rolling buffer:

  * Clip (F8):          concat the segments covering [now - N s, now] -> mp4 (stream copy, instant)
  * Long recording:     pin segments from start..stop, concat on stop
  * Bookmarks:          timestamps attached to the active long recording
  * Screenshot:         separate one-shot ddagrab grab (HDR-fixed too)

Segment times come from ffmpeg's segment list and are wall-clock based (every input
uses wall-clock timestamps offset by the run's t0), so a hotkey press maps exactly to
a point in the buffer even across ffmpeg restarts.
"""
from __future__ import annotations

import csv
import shutil
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


@dataclass
class LongRecording:
    id: str
    start: float
    game: str
    bookmarks: list = field(default_factory=list)
    clip_ids: list = field(default_factory=list)


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
        self._watch = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch.start()
        self._cleanup_stale_buffers()

    # ------------------------------------------------------------------ paths
    @property
    def buffer_root(self) -> Path:
        p = self.cfg.storage / ".buffer"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _cleanup_stale_buffers(self):
        for d in self.buffer_root.glob("run_*"):
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
                self.stop_long_recording(reason)
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
            self.stop_long_recording()
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
                                       force_cpu_path=self._force_cpu_path)
        run.plan = plan
        try:
            import subprocess
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
                try:
                    run.proc.wait(timeout=4)
                except Exception:
                    run.proc.kill()
        finally:
            for f in run.feeders:
                f.stop()
            self.audio.release()
            self._ingest_csv(run)

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
                self._ingest_csv(run)
                if run.proc and run.proc.poll() is not None:
                    self._on_crash(run)
            self._prune()

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
            self.segments.append(seg)
        run.csv_rows = len(rows)

    def _on_crash(self, run: Run):
        tail = "\n".join(list(run.log)[-12:])
        lived = time.time() - run.started
        self.run = None
        for f in run.feeders:
            f.stop()
        self.audio.release()
        self.last_error = tail or f"ffmpeg exited with code {run.proc.returncode}"
        log.error("ffmpeg exited (code %s after %.1fs). Command: %s\n%s", run.proc.returncode, lived,
                  " ".join(run.plan.args) if run.plan else "?", tail)
        self.events.emit("log", tail)
        if not self._want_running:
            return
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
        self._launch()

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
        live_dirs = {s.run.dir for s in self.segments}
        if self.run:
            live_dirs.add(self.run.dir)
        for d in self.buffer_root.glob("run_*"):
            if d not in live_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def buffered_seconds(self) -> float:
        if not self.segments:
            return 0.0
        return max(0.0, self.segments[-1].abs_end - self.segments[0].abs_start)

    # ------------------------------------------------------------------ export from buffer
    def _export_range(self, a: float, b: float, out: Path, timeout: float = 30.0) -> bool:
        """Concat buffered segments covering absolute times [a, b] into `out`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.run:
                    self._ingest_csv(self.run)
                done = any(s.abs_end >= b for s in self.segments)
            if done or not self.run:
                break
            time.sleep(0.25)
        with self._lock:
            segs = [s for s in self.segments if s.abs_end > a and s.abs_start < b]
        if not segs:
            return False
        # settings changed mid-buffer (new encoder/resolution/HDR mode) -> those files can't be
        # stream-copied together; keep the newest compatible stretch
        sig = getattr(segs[-1].run.plan, "signature", None)
        tail = []
        for sg in reversed(segs):
            if getattr(sg.run.plan, "signature", None) != sig:
                break
            tail.append(sg)
        segs = list(reversed(tail))
        lst = out.with_suffix(".concat.txt")
        lines = []
        for i, s in enumerate(segs):
            safe_path = s.path.as_posix().replace("'", "'\\''")
            lines.append(f"file '{safe_path}'")
            if i == 0 and a > s.abs_start:
                lines.append(f"inpoint {a - s.run.t0:.3f}")
            if i == len(segs) - 1 and b < s.abs_end:
                lines.append(f"outpoint {b - s.run.t0:.3f}")
        lst.write_text("\n".join(lines) + "\n", "utf-8")
        plan = segs[-1].run.plan
        r = ff.run(ff.concat_command(lst, out, plan.audio_titles if plan else None,
                                     hdr10=bool(plan and plan.hdr_mode == "hdr10")), timeout=3600)
        lst.unlink(missing_ok=True)
        with self._jobs_lock:
            self._reserved.discard(str(out))
        if r.returncode != 0 or not out.exists():
            self.last_error = r.stderr[-800:]
            log.error("export failed: %s", self.last_error)
            return False
        return True

    def _name(self, kind: str, game: str | None, ext: str) -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        safe = "".join(c for c in (game or "Desktop") if c not in '<>:"/\\|?*').strip() or "Desktop"
        folder = self.library.folder(kind, game)
        with self._jobs_lock:
            out, n = folder / f"{safe} {stamp}{ext}", 2
            while out.exists() or str(out) in self._reserved:
                out, n = folder / f"{safe} {stamp} ({n}){ext}", n + 1
            self._reserved.add(str(out))
        return out

    def _overlay(self, kind: str, title: str, sub: str = ""):
        """In-game pop-up (shown by the UI on the monitor being recorded)."""
        try:
            screen = ff.gdi_name_for_monitor(self.monitor) if self.monitor else None
        except Exception:
            screen = None
        self.events.emit("overlay", {"kind": kind, "title": title, "sub": sub, "screen": screen})

    def clip(self, seconds: int | None = None):
        """Save the last N seconds. Runs in a background thread; emits clip_saved / error."""
        if self.state != "buffering" and not self.segments:
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
        self.alerts.play("clip")
        self.events.emit("toast", ("Saving clip…", f"Last {seconds}s"))
        self._overlay("clip", "Clip saved", (f"Last {seconds} s" if seconds < 120 else f"Last {seconds // 60} min")
                      + (f" · {game}" if game else ""))

        def work():
            out = self._name("clip", game, ".mp4")
            ok = self._export_range(at - seconds, at, out)
            self.pins.pop(pin, None)
            if ok:
                meta = {"hdr": self.run.plan.hdr_mode if self.run and self.run.plan else None}
                lg = long_at_press
                if lg:
                    meta["session_id"] = lg.id
                    lg.bookmarks.append({"t": at - lg.start, "label": "Clip", "clip": True})
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
            self.stop_long_recording("split")
        self.start_long_recording()

    def start_long_recording(self):
        if self.state != "buffering":
            self._want_running = True
            self.start()
        self.long = LongRecording(id=uuid.uuid4().hex[:10], start=time.time(), game=self.game or "Desktop")
        self.pins[f"long_{self.long.id}"] = self.long.start
        self.alerts.play("start")
        self._overlay("start", "Long recording started", self.long.game)
        self.events.emit("long_recording", True)
        self.events.emit("toast", ("Long recording started", self.long.game))

    def stop_long_recording(self, reason: str = ""):
        long, self.long = self.long, None
        if not long:
            return
        end = time.time()
        self.alerts.play("stop")
        if reason != "split":
            self._overlay("stop", "Long recording saved", f"{_fmt(end - long.start)} · {long.game}")
        else:
            self._overlay("stop", "Session split", "New recording started")
        self.events.emit("long_recording", False)
        self.events.emit("toast", ("Finishing long recording…", f"{_fmt(end - long.start)} · {long.game}"))

        def work():
            out = self._name("session", long.game, ".mp4")
            ok = self._export_range(long.start, end, out, timeout=30)
            self.pins.pop(f"long_{long.id}", None)
            if ok:
                item = self.library.add(out, "session", long.game, bookmarks=long.bookmarks,
                                        session_id=long.id, created=long.start, ended_by=reason,
                                        hdr=self.run.plan.hdr_mode if self.run and self.run.plan else None)
                for cid in long.clip_ids:
                    self.library.update(cid, session_id=long.id)
                item["duration"] = end - long.start
                self.events.emit("session_saved", item)
                self.events.emit("toast", ("Long recording saved", out.name))
                self.library.enforce_limit()
            else:
                self.events.emit("error", "Couldn't finish the long recording — " + self.last_error[-200:])
        self._job(work)

    def bookmark(self, label: str = ""):
        if not self.long:
            # no long recording: a bookmark becomes a short clip so the moment isn't lost
            self.events.emit("toast", ("Bookmark", "No long recording running — saving a clip instead"))
            self.clip()
            return
        t = time.time() - self.long.start
        self.long.bookmarks.append({"t": t, "label": label or f"Bookmark {len(self.long.bookmarks) + 1}"})
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
