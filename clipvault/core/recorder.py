"""The recording engine.

One ffmpeg process per "run" captures the display (ddagrab) + audio pipes and writes
2-second MPEG-TS segments into <storage>/.buffer/<run>/.  Everything is built on that
rolling buffer:

  * Clip (F8):          concat the segments covering [now - N s, now] -> mp4 (stream copy, instant)
  * Long recording:     pin segments from start..stop, concat on stop
                        (a session.json marker on disk means a crash/reboot can't lose it: whatever
                        wasn't saved is rebuilt from the leftover segments the next time ClipVault starts)
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
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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
        # before the watcher starts pruning: turn anything a crash left behind into salvage folders
        try:
            self._salvage_after_crash()
        except Exception:
            log.exception("crash recovery scan failed")
        self._cleanup_stale_buffers()
        self._watch = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch.start()
        self._job(self._startup_chores)

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
        try:  # what crash recovery needs to rebuild this run's timeline later
            _write_json(run_dir / "run.json", {"t0": t0, "signature": plan.signature, "audio_titles": plan.audio_titles,
                                               "hdr_mode": plan.hdr_mode})
        except OSError:
            pass
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
                    try:
                        run.proc.wait(timeout=4)
                    except Exception:
                        pass
        finally:
            for f in run.feeders:
                f.stop()
            self.audio.release()
            self._adopt_tail(run)

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
            newest = max(run.dir.glob("seg_*.ts"), default=None)
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
            self._adopt_tail(run)
        except Exception:
            log.exception("couldn't rescue the last segment")
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
    def _collect(self, a: float, b: float, timeout: float = 30.0) -> list[Segment]:
        """Buffered segments covering absolute times [a, b] (waits for the one that's still being written)."""
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
            return [s for s in self.segments if s.abs_end > a and s.abs_start < b]

    def _export_range(self, a: float, b: float, out: Path, timeout: float = 30.0,
                      long: bool = False) -> tuple[list[Path], list[Segment]]:
        """Concat the buffer for [a, b] -> (files written, segments that couldn't be written).

        Clips keep only the newest stretch if settings changed mid-buffer; a long recording keeps every
        stretch, each as its own "(part N)" file, so a capture restart never throws the start away."""
        try:
            return self._export_segments(self._collect(a, b, timeout), a, b, out, long)
        finally:
            with self._jobs_lock:
                self._reserved.discard(str(out))

    @staticmethod
    def _groups(segs: list[Segment]) -> list[list[Segment]]:
        """Split into stretches that can be stream-copied together (same encoder/size/HDR mode/tracks)."""
        groups: list[list[Segment]] = []
        for sg in segs:
            if groups and _sig(groups[-1][-1].run) == _sig(sg.run):
                groups[-1].append(sg)
            else:
                groups.append([sg])
        return groups

    def _export_segments(self, segs: list[Segment], a: float, b: float, out: Path,
                         long: bool) -> tuple[list[Path], list[Segment]]:
        """-> (files written, segments of any stretch that couldn't be written)"""
        if not segs:
            self.last_error = "nothing in the buffer for that time"
            return [], []
        groups = self._groups(segs)
        if not long:
            groups = groups[-1:]
        outs = [out] if len(groups) == 1 else [out.with_name(f"{out.stem} (part {i}){out.suffix}")
                                                for i in range(1, len(groups) + 1)]
        written, failed = [], []
        for g, o in zip(groups, outs):
            if self._concat(g, a, b, o, long):
                written.append(o)
            else:
                failed += g
        return written, failed

    def _concat(self, segs: list[Segment], a: float, b: float, out: Path, long: bool) -> bool:
        lst = out.with_name(out.name + ".concat.txt")
        part = out.with_name(out.name + ".part")   # never a half-written .mp4 lying around
        lines = []
        for i, s in enumerate(segs):
            safe_path = s.path.as_posix().replace("'", "'\\''")
            lines.append(f"file '{safe_path}'")
            if i == 0 and a > s.abs_start:
                lines.append(f"inpoint {a - s.run.t0:.3f}")
            if i == len(segs) - 1 and b < s.abs_end:
                lines.append(f"outpoint {b - s.run.t0:.3f}")
        plan = segs[-1].run.plan
        try:
            lst.write_text("\n".join(lines) + "\n", "utf-8")
            args = ff.concat_command(lst, part, plan.audio_titles if plan else None,
                                     hdr10=bool(plan and plan.hdr_mode == "hdr10"),
                                     faststart=not long)  # faststart re-writes the whole file: skip it for big sessions
            ok, err = self._run_watched(args, part)
            if ok and part.exists() and part.stat().st_size > 0:
                os.replace(part, out)
                return True
            self.last_error = err or "export failed"
            log.error("export failed (%s): %s", out.name, self.last_error[-800:])
        except Exception as e:
            self.last_error = str(e)
            log.exception("export failed (%s)", out.name)
        finally:
            lst.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
        return False

    @staticmethod
    def _run_watched(args: list[str], growing: Path) -> tuple[bool, str]:
        """Run an export with no overall time limit (a 2-hour session can take a long while on a hard
        drive) - only give up if the output file stops growing."""
        proc = ff.popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        text=True, errors="replace")
        err: deque = deque(maxlen=60)
        reader = threading.Thread(target=lambda: [err.append(l.rstrip()) for l in proc.stderr], daemon=True)
        reader.start()
        size, moved = -1, time.time()
        while proc.poll() is None:
            time.sleep(1.0)
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

    # ------------------------------------------------------------------ crash-proof long recordings
    def _marker(self, long: LongRecording) -> Path:
        return self.buffer_root / f"session_{long.id}.json"

    def _save_marker(self, long: LongRecording, end: float | None = None):
        try:
            _write_json(self._marker(long), {"id": long.id, "start": long.start, "game": long.game, "end": end,
                                             "bookmarks": long.bookmarks})
        except OSError:
            log.exception("couldn't write session marker")

    def _salvage(self, info: dict, segs: list[Segment]) -> Path | None:
        """Move a long recording's segments out of the rolling buffer into their own folder, so they
        survive pruning/restarts until they're saved. Returns the folder."""
        if not segs:
            return None
        d = self.buffer_root / f"salvage_{info['id']}"
        d.mkdir(parents=True, exist_ok=True)
        rows = []
        for s in segs:
            name = f"{s.run.dir.name}_{s.path.name}"
            try:
                if s.path.parent != d:
                    os.replace(s.path, d / name)
            except OSError:
                try:
                    shutil.copy2(s.path, d / name)
                except OSError:
                    continue
            plan = s.run.plan
            rows.append({"file": name, "start": s.start, "end": s.end, "t0": s.run.t0,
                         "signature": getattr(plan, "signature", None),
                         "audio_titles": getattr(plan, "audio_titles", []), "hdr_mode": getattr(plan, "hdr_mode", "sdr")})
        _write_json(d / "salvage.json", {"session": info, "segments": rows, "attempts": 0})
        with self._lock:
            moved = {id(s) for s in segs}
            self.segments = [s for s in self.segments if id(s) not in moved]
        return d

    def _salvage_after_crash(self):
        """Startup: a session.json marker still on disk means ClipVault (or the PC) went down before that long
        recording was saved. Rebuild it from the leftover buffer before the buffer is cleared."""
        markers = sorted(self.buffer_root.glob("session_*.json"))
        if not markers:
            return
        runs: list[tuple[Run, list[Segment]]] = []
        for d in sorted(self.buffer_root.glob("run_*")):
            try:
                info = json.loads((d / "run.json").read_text("utf-8"))
            except (OSError, ValueError):
                continue  # from an older version: no timing info, can't place it
            run = Run(dir=d, t0=float(info["t0"]),
                      plan=SimpleNamespace(signature=info.get("signature"), audio_titles=info.get("audio_titles") or [],
                                           hdr_mode=info.get("hdr_mode") or "sdr"))
            segs = []
            f = d / "segments.csv"
            if f.exists():
                for row in csv.reader(f.read_text("utf-8", errors="replace").splitlines()):
                    if len(row) >= 3 and (d / row[0]).exists():
                        try:
                            segs.append(Segment(d / row[0], float(row[1]), float(row[2]), run))
                            run.listed.add(row[0])
                        except ValueError:
                            pass
            segs += self._tail_segments(run, max((s.end for s in segs), default=None))
            runs.append((run, segs))
        for m in markers:
            try:
                info = json.loads(m.read_text("utf-8"))
            except (OSError, ValueError):
                m.unlink(missing_ok=True)
                continue
            start = float(info.get("start") or 0)
            end = float(info.get("end") or 0) or float("inf")
            mine = [s for _, segs in runs for s in segs if s.abs_end > start and s.abs_start < end]
            if mine:
                info["end"] = min(end, max(s.abs_end for s in mine))
                info["recovered"] = True
                self._salvage(info, mine)
                log.info("recovering long recording %s (%s) from the buffer", info.get("id"), info.get("game"))
            m.unlink(missing_ok=True)

    def _startup_chores(self):
        # an export that was cut off (app closed/crashed mid-save) leaves these behind; nothing is exporting yet
        root = self.cfg.storage
        for sub in ("Sessions", "Clips"):
            base = root / sub
            if not base.exists():
                continue
            for pat in ("*.mp4.part", "*.mp4.concat.txt", "*.concat.txt"):
                for f in base.rglob(pat):
                    try:
                        f.unlink()
                    except OSError:
                        pass
        if any(self.buffer_root.glob("salvage_*/salvage.json")):
            self._finish_salvage()

    def _finish_salvage(self):
        """Save every salvaged long recording (from a crash, or an export that failed earlier)."""
        for f in sorted(self.buffer_root.glob("salvage_*/salvage.json")):
            d = f.parent
            try:
                data = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            info = data.get("session") or {}
            segs = []
            for r in data.get("segments", []):
                p = d / r["file"]
                if not p.exists():
                    continue
                run = Run(dir=d, t0=float(r["t0"]),
                          plan=SimpleNamespace(signature=r.get("signature"), audio_titles=r.get("audio_titles") or [],
                                               hdr_mode=r.get("hdr_mode") or "sdr"))
                segs.append(Segment(p, float(r["start"]), float(r["end"]), run))
            # one Run object per original run so the stretches group correctly
            by_t0: dict[float, Run] = {}
            for sg in segs:
                sg.run = by_t0.setdefault(sg.run.t0, sg.run)
            segs.sort(key=lambda sg: sg.abs_start)
            game = info.get("game") or "Desktop"
            start = float(info.get("start") or (segs[0].abs_start if segs else time.time()))
            end = float(info.get("end") or (segs[-1].abs_end if segs else start))
            if not segs:
                shutil.rmtree(d, ignore_errors=True)
                continue
            out = self._name("session", game, ".mp4", when=start, note=" (recovered)" if info.get("recovered") else "")
            try:
                written, failed = self._export_segments(segs, start, end, out, long=True)
            finally:
                with self._jobs_lock:
                    self._reserved.discard(str(out))
            for o in written:
                self.library.add(o, "session", game, bookmarks=info.get("bookmarks") or [],
                                 session_id=info.get("id"), created=start,
                                 ended_by="recovered" if info.get("recovered") else (info.get("ended_by") or ""),
                                 hdr=segs[-1].run.plan.hdr_mode)
            if written and not failed:
                shutil.rmtree(d, ignore_errors=True)
                self.events.emit("toast", ("Long recording recovered" if info.get("recovered") else "Long recording saved",
                                           f"{game} · {_fmt(end - start)}"))
                log.info("salvaged long recording saved: %s", ", ".join(o.name for o in written))
            else:
                if written:  # keep only what still needs saving
                    keep = {sg.path.name for sg in failed}
                    data["segments"] = [r for r in data.get("segments", []) if r["file"] in keep]
                    for r in list(d.glob("*.ts")):
                        if r.name not in keep:
                            r.unlink(missing_ok=True)
                data["attempts"] = int(data.get("attempts", 0)) + 1
                _write_json(f, data)
                self.events.emit("error", f"Couldn't save the {game} long recording yet — ClipVault kept the pieces "
                                          f"in {d.name} and will try again next start.")

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
            try:
                ok = bool(self._export_range(at - seconds, at, out)[0])
            finally:
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
        self._save_marker(self.long)
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

        self._save_marker(long, end)

        def work():
            out = self._name("session", long.game, ".mp4", when=long.start)
            written, failed = [], []
            try:
                written, failed = self._export_range(long.start, end, out, timeout=30, long=True)
            except Exception as e:
                log.exception("long recording export crashed")
                self.last_error = str(e)
                with self._lock:
                    failed = [s for s in self.segments if s.abs_end > long.start and s.abs_start < end]
            hdr_mode = self.run.plan.hdr_mode if self.run and self.run.plan else None
            for i, o in enumerate(written):
                item = self.library.add(o, "session", long.game, bookmarks=long.bookmarks if i == 0 else [],
                                        session_id=long.id, created=long.start, ended_by=reason, hdr=hdr_mode)
                if i == 0:
                    for cid in long.clip_ids:
                        self.library.update(cid, session_id=long.id)
                    item["duration"] = end - long.start
                    self.events.emit("session_saved", item)
                    self.events.emit("toast", ("Long recording saved", o.name))
            if not written and not failed:
                self.events.emit("error", "Couldn't finish the long recording — nothing was captured for it. "
                                          + (self.last_error or "")[-160:])
            if failed:
                # don't lose it: park the pieces outside the rolling buffer; saved on the next start
                try:
                    self._salvage({"id": long.id, "start": long.start, "game": long.game, "end": end,
                                   "bookmarks": long.bookmarks, "ended_by": reason}, failed)
                except Exception:
                    log.exception("couldn't park the long recording's segments")
                why = (self.last_error or "").strip().splitlines()
                self.events.emit("error", "Couldn't finish the long recording — ClipVault kept it and will save it "
                                          "the next time it starts." + (f" ({why[-1][:160]})" if why else ""))
            self._marker(long).unlink(missing_ok=True)
            self.pins.pop(f"long_{long.id}", None)
            if written:
                self.library.enforce_limit()
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
