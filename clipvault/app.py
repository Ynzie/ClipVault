"""Wires the engine together and bridges engine events into Qt signals."""
from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import QObject, Signal, QTimer, QMetaObject, Qt, QThread

from .core.audio import AudioEngine
from .core.config import Config
from .core.games import GameWatcher
from .core.hotkeys import HotkeyManager
from .core.library import Library
from .core.recorder import Events, Recorder
from .core.sounds import Alerts


class Bridge(QObject):
    event = Signal(str, object)


class Controller(QObject):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.cfg = cfg or Config()
        self.events = Events()
        self.bridge = Bridge()
        self.events.subscribe(lambda n, p: self.bridge.event.emit(n, p))  # queued across threads
        self.library = Library(self.cfg, self.events)
        self.audio = AudioEngine(self.cfg)
        self.alerts = Alerts(self.cfg)
        self.audio.on_status = lambda t, m, err: self.events.emit("error" if err else "toast", f"{t}: {m}" if err else (t, m))
        self.recorder = Recorder(self.cfg, self.library, self.audio, self.alerts, self.events)
        self.games = GameWatcher(self.cfg, self._on_game)
        self.hotkeys = HotkeyManager(self.cfg.get("hotkeys"), self._on_hotkey)
        from .core.controller import PadShortcuts
        self.pad = PadShortcuts(self.cfg, self._on_hotkey)  # controller combos run the same actions as the keys
        self.cfg.on_change(self._on_cfg)
        self._restart_timer = QTimer(self, singleShot=True, interval=800, timeout=self.recorder.restart)
        self._maint = QTimer(self, interval=60_000, timeout=self._maintenance)
        self._maint_n = 0
        self._scan_lock = threading.Lock()
        self.last_scan: dict | None = None
        # everything captured while the current game runs (for the post-game review)
        self.game_run: dict | None = None
        self.events.subscribe(self._track_run)

    def start(self):
        if self.cfg.get("first_run"):
            self.cfg.set("first_run", False)
            if self.cfg.get("app.launch_on_startup"):
                set_startup(True)
        self.games.start()
        self.hotkeys.start()
        self.pad.start()
        QTimer.singleShot(3000, lambda: self.scan_library(manual=False))  # link existing recordings
        # resume the virtual mic if it was on (only ever targets a virtual cable)
        from .core.audio import is_virtual_output
        if self.cfg.get("audio.passthrough_enabled"):
            if is_virtual_output(self.cfg.get("audio.passthrough_device")):
                threading.Thread(target=self.audio._sync_passthrough, daemon=True).start()
            else:
                self.cfg.set("audio.passthrough_enabled", False)
        self._maint.start()
        threading.Thread(target=self._maintenance, daemon=True).start()
        if self.cfg.get("recording.mode") == "always":
            self.recorder.start()

    def shutdown(self):
        self.games.stop()
        self.hotkeys.stop()
        self.pad.stop()
        self.recorder.shutdown()
        try:  # close the mic / virtual mic / monitor streams cleanly
            self.audio.stop_monitor()
            if self.audio._passthrough:
                self.audio._passthrough.stop()
            self.audio._close()
        except Exception:
            pass
        self.library.save(now=True)

    # ------------------------------------------------------------- engine hooks
    def _on_game(self, game):
        self.recorder.set_game(game)
        mode = self.cfg.get("recording.mode")
        rec = self.cfg.get("recording")
        if game:
            self.game_run = {"game": game, "start": time.time(), "items": [], "session": None, "pending": 0}
            if mode in ("auto", "always"):
                self.recorder.start()
                if rec.get("full_session") and not self.recorder.long:
                    self.recorder.start_long_recording()
        else:
            run, self.game_run = self.game_run, None
            if run is not None:
                run["ended"] = time.time()
                self._closing_run = run
            if self.recorder.long and rec.get("full_session"):
                self.recorder.stop_long_recording("game_closed")
            if mode == "auto" and not self.recorder.long:
                self.recorder.stop("game_closed")
            if run is not None:
                threading.Thread(target=self._emit_review_when_ready, args=(run,), daemon=True).start()

    def _track_run(self, name, payload):
        run = self.game_run or getattr(self, "_closing_run", None)
        if run is None:
            return
        if name in ("clip_saved", "screenshot_saved"):
            run["items"].append(payload["id"])
        elif name == "session_saved":
            run["items"].append(payload["id"])
            if payload.get("ended_by") in ("game_closed", "split", "") :
                run["session"] = payload["id"]

    def _emit_review_when_ready(self, run):
        """Wait for the session file (and any clip exports) to finish, then ask the UI to show the review."""
        if not self.cfg.get("recording.review_after_game", True) or run.get("skip_review"):
            return
        deadline = time.time() + 600
        time.sleep(1.0)
        while self.recorder.busy() and time.time() < deadline:
            time.sleep(0.3)
        if self.game_run is not None and self.game_run is not run and self.game_run.get("game"):
            pass  # a new game already started; still show the review for the one that ended
        ids = [i for i in run["items"] if self.library.get(i)]
        if getattr(self, "_closing_run", None) is run:
            self._closing_run = None  # later desktop clips don't belong to this game anymore
        if ids:
            self.events.emit("review", {"game": run["game"], "start": run["start"], "end": run.get("ended"),
                                        "items": ids, "session": run.get("session"),
                                        "defer": bool(self.games.current)})  # already in another game

    # -- hotkeys (Medal-style smart clip key with hold-to-split)
    def _on_hotkey(self, name: str):
        rec = self.recorder
        if name == "clip":
            if self.cfg.get("recording.long_press_split") and rec.long and self._held_long(self.cfg.get("hotkeys.clip")):
                rec.split_long_recording()
                return
            if rec.long:
                mode = self.cfg.get("recording.session_key", "bookmark")
                if mode in ("bookmark", "both"):
                    rec.bookmark()
                if mode in ("clip", "both"):
                    rec.clip()
            else:
                rec.clip()
        elif name == "clip_alt":
            rec.clip(int(self.cfg.get("recording.clip_alt_seconds", 300)))
        elif name == "long_recording":
            rec.toggle_long_recording()
        elif name == "bookmark":
            rec.bookmark()
        elif name == "screenshot":
            rec.screenshot()

    def _held_long(self, combo: str, hold: float = 0.8) -> bool:
        if sys.platform != "win32":
            return False
        from .core.hotkeys import parse
        import ctypes
        p = parse(combo)
        if not p:
            return False
        u32 = ctypes.WinDLL("user32")
        t0 = time.time()
        while time.time() - t0 < hold:
            if not u32.GetAsyncKeyState(p[1]) & 0x8000:
                return False
            time.sleep(0.02)
        return True

    def _on_cfg(self, key: str, value):
        if key == "app.launch_on_startup":
            set_startup(bool(value))
        elif key.startswith("hotkeys"):
            self.hotkeys.update(self.cfg.get("hotkeys"))
        elif key == "recording.mode":
            if value == "always" or (value == "auto" and self.games.current):
                self.recorder.start()
            elif value == "auto" and not self.games.current and not self.recorder.long:
                threading.Thread(target=self.recorder.stop, daemon=True).start()
        elif key.startswith(("quality", "hdr", "recording.monitor", "recording.show_cursor",
                             "recording.framerate_mode", "audio.separate_tracks", "audio.noise",
                             "audio.system_device", "audio.mic_device", "audio.system_enabled",
                             "audio.mic_enabled", "audio.bitrate", "audio.sync")):
            if self.recorder.run is not None:
                if QThread.currentThread() is self.thread():
                    self._restart_timer.start()
                else:  # settings changed from a worker thread: QTimers only start on their own thread
                    QMetaObject.invokeMethod(self._restart_timer, "start", Qt.QueuedConnection)
        elif key in ("storage.scan_storage", "storage.linked_folders"):
            self.scan_library(manual=False)
        elif key == "storage.folder":
            old = self.library.snapshot()
            self.library.reload()
            with self.library._lock:  # carry existing clips over (their paths are absolute, they still play)
                for it in old:
                    self.library.items.setdefault(it["id"], it)
            self.library.save(now=True)
            self.events.emit("library_changed", None)
            self.scan_library(manual=False)

    def _maintenance(self):
        try:
            self.library.purge_old_trash()
            self.library.enforce_limit()
        except Exception:
            pass
        self._maint_n += 1
        if self._maint_n % 5 == 0:  # every ~5 minutes pick up new/removed files in linked folders
            self.scan_library(manual=False)

    def scan_library(self, manual: bool = True):
        """Link existing recordings in the capture folder + linked folders (background thread)."""
        def go():
            if not self._scan_lock.acquire(blocking=False):
                return
            try:
                with self.recorder._jobs_lock:
                    busy = set(self.recorder._reserved)
                res = self.library.scan(busy)
            except Exception as e:
                res = {"error": str(e)}
            finally:
                self._scan_lock.release()
            self.last_scan = res
            if res.get("error"):
                self.events.emit("error", f"Couldn't scan folders: {res['error']}")
            elif manual or res.get("added", 0) >= 5:
                added, removed = res.get("added", 0), res.get("removed", 0)
                msg = (f"{res.get('videos', 0)} video(s), {res.get('screenshots', 0)} screenshot(s) linked"
                       if added else "No new recordings found")
                if removed:
                    msg += f" · {removed} missing file(s) removed"
                self.events.emit("toast", ("Library updated", msg))
        threading.Thread(target=go, daemon=True, name="LibraryScan").start()

    # ------------------------------------------------------------- UI actions
    def not_a_game(self):
        """Wrongly detected app: ignore it from now on and end its session without a review popup."""
        if self.game_run is not None:
            self.game_run["skip_review"] = True

        def go():
            exe = self.games.ignore_current()
            if exe:
                self.events.emit("toast", ("Won't record this again", exe))
        threading.Thread(target=go, daemon=True).start()

    def toggle_buffer(self):
        if self.recorder.state in ("buffering", "starting"):
            threading.Thread(target=self.recorder.stop, daemon=True).start()  # waits on ffmpeg; keep UI responsive
        else:
            self.recorder.start()


def set_startup(on: bool):
    """Add/remove ClipVault from the HKCU Run key (starts minimized to tray)."""
    if sys.platform != "win32":
        return
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                             winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        if on:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}" --minimized'
            else:
                pyw = sys.executable.replace("python.exe", "pythonw.exe")
                cmd = f'"{pyw}" -m clipvault --minimized'
            winreg.SetValueEx(key, "ClipVault", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "ClipVault")
            except OSError:
                pass
    finally:
        winreg.CloseKey(key)
