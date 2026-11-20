"""Video player for the editor.

Primary engine: libmpv (GPU decoding via D3D11, smooth playback of 1440p/4K HEVC/AV1, exact
seeking, frame stepping, HDR10 shown correctly). It renders into a native child window.
Fallback: Qt Multimedia (QMediaPlayer + QVideoWidget) when libmpv-2.dll isn't available.

Both engines expose the same small API, so the editor doesn't care which one is running.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

log = logging.getLogger("clipvault.player")

_mpv_mod = None
_mpv_error = ""


def _dll_dirs() -> list[Path]:
    here = Path(__file__).resolve()
    dirs = []
    if getattr(sys, "_MEIPASS", None):
        dirs += [Path(sys._MEIPASS), Path(sys._MEIPASS) / "mpv"]
    dirs += [Path(sys.executable).parent, Path(sys.executable).parent / "mpv",
             here.parents[2] / "mpv", here.parents[2]]
    return dirs


def load_mpv():
    """Import python-mpv with our bundled libmpv-2.dll. Returns the module or None."""
    global _mpv_mod, _mpv_error
    if _mpv_mod is not None or _mpv_error:
        return _mpv_mod
    if os.environ.get("CLIPVAULT_PLAYER") == "qt":
        _mpv_error = "disabled by CLIPVAULT_PLAYER=qt"
        return None
    try:
        if sys.platform == "win32":
            for d in _dll_dirs():
                if (d / "libmpv-2.dll").exists():
                    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                    try:
                        os.add_dll_directory(str(d))
                    except (OSError, AttributeError):
                        pass
                    break
        import mpv  # python-mpv
        _mpv_mod = mpv
    except Exception as e:  # missing DLL, missing vulkan-1.dll, etc.
        _mpv_error = str(e)
        log.warning("mpv unavailable, using Qt player: %s", e)
    return _mpv_mod


class VideoPlayer(QWidget):
    """Video surface + transport. Signals fire on the GUI thread."""
    durationChanged = Signal(float)
    playingChanged = Signal(bool)
    loaded = Signal()
    failed = Signal(str)
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #000;")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self.engine = None
        self._duration = 0.0
        self._playing = False
        self._path = None
        self._pending_seek = None
        self._volume = 80
        self._speed = 1.0
        mpv = load_mpv()
        if mpv is not None:
            try:
                self.engine = _MpvEngine(self, mpv)
            except Exception as e:
                log.warning("mpv init failed, using Qt player: %s", e)
                self.engine = None
        if self.engine is None:
            self.engine = _QtEngine(self)
        self.engine_name = self.engine.name
        # state poll (both engines): duration / end-of-file / play state
        self._poll = QTimer(self, interval=100, timeout=self._poll_state)
        self._poll.start()

    # ------------------------------------------------------------------ API
    def load(self, path: str, autoplay: bool = True):
        self._path = path
        self._duration = 0.0
        self.engine.load(path, autoplay)
        self._set_playing(autoplay)

    def unload(self):
        """Stop and release the file (so it can be moved/deleted)."""
        self._path = None
        self.engine.unload()
        self._set_playing(False)

    def play(self):
        if self._path:
            self.engine.set_paused(False)
            self._set_playing(True)

    def pause(self):
        self.engine.set_paused(True)
        self._set_playing(False)

    def toggle(self):
        self.pause() if self._playing else self.play()

    def is_playing(self) -> bool:
        return self._playing

    def seek(self, t: float, exact: bool = True):
        self.engine.seek(max(0.0, t), exact)

    def position(self) -> float:
        return self.engine.position()

    def duration(self) -> float:
        return self._duration

    def set_volume(self, v: int):
        self._volume = v
        self.engine.set_volume(v)

    def set_speed(self, s: float):
        self._speed = s
        self.engine.set_speed(s)

    def set_audio_track(self, index: int):
        self.engine.set_audio_track(index)

    def set_preview_filter(self, graph: str) -> bool:
        """Show export effects (crop / caption) live. Returns False if this engine can't."""
        return self.engine.set_vf(graph)

    def step(self, direction: int):
        self.engine.step(direction)
        self._set_playing(False)

    def shutdown(self):
        self._poll.stop()
        try:
            self.engine.shutdown()
        except Exception:
            pass

    # ------------------------------------------------------------------ internals
    def _set_playing(self, on: bool):
        if on != self._playing:
            self._playing = on
            self.playingChanged.emit(on)

    def _poll_state(self):
        if not self._path:
            return
        d = self.engine.duration()
        if d and abs(d - self._duration) > 0.01:
            self._duration = d
            self.durationChanged.emit(d)
            self.loaded.emit()
        if self._playing and self.engine.at_end():
            self._set_playing(False)
        err = self.engine.take_error()
        if err:
            self.failed.emit(err)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


# =============================================================================== mpv
class _MpvHost(QWidget):
    """Native child window that mpv draws into. Only this widget is native (not its ancestors),
    so switching pages never leaves stale/blank areas behind."""
    clicked = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setMouseTracking(False)

    def paintEvent(self, e):  # mpv paints this area
        pass

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()


class _MpvEngine:
    name = "mpv"

    def __init__(self, owner: VideoPlayer, mpv):
        self.owner = owner
        self.host = _MpvHost(owner)
        owner._lay.addWidget(self.host)
        self.host.clicked.connect(owner.clicked.emit)
        self._error = ""
        opts = {}
        vo = os.environ.get("CLIPVAULT_MPV_VO", "gpu-next,gpu")  # test hook: "null" for headless runs
        if vo != "null":
            opts["wid"] = str(int(self.host.winId()))
        if os.environ.get("CLIPVAULT_MPV_AO"):
            opts["ao"] = os.environ["CLIPVAULT_MPV_AO"]
        base = dict(vo=vo, hwdec="auto-safe", keep_open="yes", idle="yes", **opts,
                    input_default_bindings=False, input_vo_keyboard=False, osc=False, osd_level=0,
                    hr_seek="yes", hr_seek_framedrop="yes", audio_file_auto="no", sub_auto="no", ytdl=False,
                    cursor_autohide="no", demuxer_max_back_bytes="150MiB", loglevel="warn", log_handler=self._log)
        # mpv's built-in Lua scripts (stats, console, …) aren't used here; their internal Lua errors showed up
        # as "Windows fatal exception 0xe24c4a02" dumps in clipvault.log. Older libmpv may not know every switch.
        no_lua = dict(load_scripts="no", load_stats_overlay="no", load_console="no", load_auto_profiles="no",
                      load_select="no", load_commands="no", load_positioning="no")
        self.p = None
        for _ in range(len(no_lua) + 1):
            try:
                self.p = mpv.MPV(**base, **no_lua)
                break
            except Exception as e:  # drop the switch this libmpv doesn't know and try again
                bad = next((a.decode() for a in getattr(e, "args", ())[-1:][0] if isinstance(a, bytes)), "") \
                    if getattr(e, "args", None) and isinstance(e.args[-1], tuple) else ""
                key = bad.replace("-", "_")
                if key in no_lua:
                    no_lua.pop(key)
                else:
                    no_lua.clear()
        if self.p is None:
            self.p = mpv.MPV(**base)
        for k, v in (("input-cursor-passthrough", "yes"), ("background-color", "#000000")):
            try:  # newer-mpv-only niceties; ignore if this build doesn't know them
                self.p[k] = v
            except Exception:
                pass

    def _log(self, level, component, message):
        msg = f"mpv[{component}] {message.strip()}"
        if level in ("error", "fatal"):
            log.warning(msg)
            if "Failed to recognize file format" in message or "cannot open" in message.lower():
                self._error = message.strip()

    def load(self, path, autoplay):
        self.p.pause = not autoplay
        self.p.loadfile(path)

    def unload(self):
        try:
            self.p.command("stop")
        except Exception:
            pass

    def set_paused(self, paused):
        try:
            if not paused and self.at_end():
                self.p.seek(0, "absolute", "exact")
            self.p.pause = paused
        except Exception:
            pass

    def seek(self, t, exact):
        try:
            self.p.seek(t, "absolute", "exact" if exact else "keyframes")
        except Exception:
            pass  # not loaded yet

    def position(self):
        try:
            v = self.p.time_pos
            return float(v) if v is not None else 0.0
        except Exception:
            return 0.0

    def duration(self):
        try:
            v = self.p.duration
            return float(v) if v else 0.0
        except Exception:
            return 0.0

    def at_end(self):
        try:
            return bool(self.p.eof_reached)
        except Exception:
            return False

    def set_volume(self, v):
        try:
            self.p.volume = int(v)
        except Exception:
            pass

    def set_speed(self, s):
        try:
            self.p.speed = float(s)
        except Exception:
            pass

    def set_audio_track(self, index):
        try:
            self.p.aid = int(index) + 1
        except Exception:
            pass

    def set_vf(self, graph):
        try:
            # %N% quoting passes the libavfilter graph through untouched (commas, colons, escapes)
            self.p["vf"] = f"lavfi=graph=%{len(graph.encode('utf-8'))}%{graph}" if graph else ""
            return True
        except Exception as e:
            log.warning("preview filter failed: %s", e)
            return False

    def step(self, direction):
        try:
            self.p.frame_step() if direction > 0 else self.p.frame_back_step()
        except Exception:
            pass

    def take_error(self):
        e, self._error = self._error, ""
        return e

    def shutdown(self):
        try:
            self.p.terminate()
        except Exception:
            pass


# =============================================================================== Qt fallback
class _QtEngine:
    name = "qt"

    def __init__(self, owner: VideoPlayer):
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        from .widgets import audio_output
        self._QMP = QMediaPlayer
        self.owner = owner
        self.video = QVideoWidget(owner)
        owner._lay.addWidget(self.video)
        self.player = QMediaPlayer(owner)
        self.out = audio_output(owner)
        if hasattr(self.out, "deviceChanged"):
            self.player.setAudioOutput(self.out)
        self.player.setVideoOutput(self.video)
        self._error = ""
        self.player.errorOccurred.connect(lambda e, s: setattr(self, "_error", s))
        self.player.tracksChanged.connect(lambda: self.player.audioTracks() and self.player.setActiveAudioTrack(0))

    def load(self, path, autoplay):
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play() if autoplay else self.player.pause()

    def unload(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def set_paused(self, paused):
        if paused:
            self.player.pause()
        else:
            if self.at_end():
                self.player.setPosition(0)
            self.player.play()

    def seek(self, t, exact):
        self.player.setPosition(int(t * 1000))

    def position(self):
        return self.player.position() / 1000.0

    def duration(self):
        return self.player.duration() / 1000.0

    def at_end(self):
        return self.player.mediaStatus() == self._QMP.MediaStatus.EndOfMedia

    def set_volume(self, v):
        if hasattr(self.out, "setVolume"):
            self.out.setVolume(v / 100)

    def set_speed(self, s):
        self.player.setPlaybackRate(s)

    def set_audio_track(self, index):
        if index < len(self.player.audioTracks()):
            self.player.setActiveAudioTrack(index)

    def set_vf(self, graph):
        return not graph

    def step(self, direction):
        fps = 60.0
        self.player.pause()
        self.player.setPosition(max(0, self.player.position() + int(direction * 1000 / fps)))

    def take_error(self):
        e, self._error = self._error, ""
        return e

    def shutdown(self):
        self.player.stop()
