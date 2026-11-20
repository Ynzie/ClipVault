"""Audio capture: WASAPI loopback (PC audio) + microphone, with a Discord-style
input-sensitivity gate, live meters, calibration, mic test and virtual-mic passthrough.

Audio is captured in Python (PyAudioWPatch) and streamed to ffmpeg through named
pipes as raw s16le.  A wall-clock pump pads silence whenever the device goes quiet
(WASAPI loopback delivers nothing while nothing is playing), so ffmpeg's audio and
video never drift apart.
"""
from __future__ import annotations

import collections
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field

import logging

import numpy as np

log = logging.getLogger("clipvault.audio")

IS_WIN = sys.platform == "win32"

try:  # Windows-only dependency
    import pyaudiowpatch as pyaudio  # type: ignore
except Exception:  # pragma: no cover
    pyaudio = None


def db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-7))


# ----------------------------------------------------------------------------- devices

@dataclass
class Device:
    index: int
    name: str
    channels: int
    rate: int
    kind: str  # mic | loopback | output
    is_default: bool = False


_pa_lock = threading.Lock()
_pa = None


def _get_pa():
    global _pa
    with _pa_lock:
        if _pa is None and pyaudio is not None:
            _pa = pyaudio.PyAudio()
        return _pa


def list_devices() -> dict[str, list[Device]]:
    out = {"mic": [], "loopback": [], "output": []}
    pa = _get_pa()
    if pa is None:
        return out
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except Exception:
        return out
    def_in, def_out = wasapi.get("defaultInputDevice", -1), wasapi.get("defaultOutputDevice", -1)
    def_out_name = ""
    if def_out >= 0:
        def_out_name = pa.get_device_info_by_index(def_out)["name"]
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d.get("hostApi") != wasapi["index"]:
            continue
        name, rate = d["name"], int(d["defaultSampleRate"])
        if d.get("isLoopbackDevice"):
            out["loopback"].append(Device(i, name.replace(" [Loopback]", ""), d["maxInputChannels"], rate,
                                          "loopback", def_out_name and def_out_name in name))
        elif d["maxInputChannels"] > 0:
            out["mic"].append(Device(i, name, d["maxInputChannels"], rate, "mic", i == def_in))
        if d["maxOutputChannels"] > 0 and not d.get("isLoopbackDevice"):
            out["output"].append(Device(i, name, d["maxOutputChannels"], rate, "output", i == def_out))
    return out


def pick_device(kind: str, wanted: str) -> Device | None:
    devs = list_devices().get(kind, [])
    if wanted:
        for d in devs:
            if d.name == wanted:
                return d
    for d in devs:
        if d.is_default:
            return d
    return devs[0] if devs else None


# ----------------------------------------------------------------------------- gate

@dataclass
class GateSettings:
    enabled: bool = True
    auto: bool = False            # Discord-style "automatically determine input sensitivity"
    threshold_db: float = -45.0   # anything quieter than this is muted
    attack_ms: float = 5.0
    hold_ms: float = 250.0
    release_ms: float = 120.0
    floor_db: float = -80.0       # how far the gate attenuates when closed (-80 ~ silent)


class NoiseGate:
    """Envelope-follower gate with hold + smooth gain ramps (no clicks)."""

    def __init__(self, rate: int, s: GateSettings):
        self.rate, self.s = rate, s
        self.gain = 0.0
        self.env = 0.0
        self.hold_left = 0
        self.noise_floor_db = -60.0
        self.is_open = False
        self.last_level = -90.0  # the level the gate compares against (what the meter shows)

    def effective_threshold(self) -> float:
        if self.s.auto:
            return min(-20.0, max(-70.0, self.noise_floor_db + 14.0))
        return self.s.threshold_db

    def process(self, x: np.ndarray) -> np.ndarray:
        """x: float32 (frames, ch) in -1..1. Returns gated copy."""
        if x.size == 0:
            return x
        n = x.shape[0]
        rms = float(np.sqrt(np.mean(np.square(x))))
        peak = float(np.max(np.abs(x)))
        level = db(max(rms * 1.41, peak * 0.7))
        self.last_level = level
        if not self.s.enabled:
            self.is_open = True
            return x
        # slow noise-floor tracker for auto mode (rises slowly, drops fast)
        if level < self.noise_floor_db:
            self.noise_floor_db = 0.7 * self.noise_floor_db + 0.3 * level
        else:
            self.noise_floor_db += min(level - self.noise_floor_db, 0.05)
        thr = self.effective_threshold()
        if level >= thr:
            self.hold_left = int(self.s.hold_ms * self.rate / 1000)
            target = 1.0
        elif self.hold_left > 0:
            self.hold_left -= n
            target = 1.0
        else:
            target = 10 ** (self.s.floor_db / 20)
        self.is_open = target >= 1.0
        ms = self.s.attack_ms if target > self.gain else self.s.release_ms
        step = n / max(1.0, ms * self.rate / 1000)
        new_gain = self.gain + (target - self.gain) * min(1.0, step)
        ramp = np.linspace(self.gain, new_gain, n, dtype=np.float32)[:, None]
        self.gain = new_gain
        return x * ramp


# ----------------------------------------------------------------------------- sources

class Source:
    """One capture device. Processes audio and fans it out to subscribers."""

    BLOCK = 480  # 10 ms @ 48k

    def __init__(self, kind: str, device: Device | None, volume: float = 1.0,
                 gate: GateSettings | None = None, mono: bool = False):
        self.kind = kind
        self.device = device
        self.rate = device.rate if device else 48000
        self.in_channels = device.channels if device else 2
        self.channels = 2
        self.volume = volume
        self.mono = mono
        self.gate = NoiseGate(self.rate, gate) if gate else None
        self.muted = False
        self.ptt_vk: int = 0          # push-to-talk virtual key, 0 = off
        self.level_db = -90.0         # post-processing peak (what gets recorded)
        self.raw_level_db = -90.0     # pre-gate level (what the mic actually hears)
        self._subs: list[collections.deque] = []
        self._sub_lock = threading.Lock()
        self._stream = None
        self._fake_thread = None
        self._running = False
        self.error = ""

    # subscribers get processed int16 bytes
    def subscribe(self, maxlen: int = 2000) -> collections.deque:
        q: collections.deque = collections.deque(maxlen=maxlen)
        with self._sub_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._sub_lock:
            if q in self._subs:
                self._subs.remove(q)

    def start(self):
        if self._running:
            return
        self._running = True
        _com_init()
        pa = _get_pa()
        if pa is not None and self.device is None:
            self.error = "No device found"
        if pa is not None and self.device is not None:
            try:
                self._stream = pa.open(format=pyaudio.paInt16, channels=self.in_channels, rate=self.rate,
                                       frames_per_buffer=self.BLOCK, input=True,
                                       input_device_index=self.device.index,
                                       stream_callback=self._pa_callback)
                self._stream.start_stream()
                return
            except Exception as e:  # device busy / unplugged
                self.error = f"{self.device.name}: {e}"
                log.warning("could not open %s source: %s", self.kind, self.error)
        if os.environ.get("CLIPVAULT_FAKE_AUDIO"):
            self._fake_thread = threading.Thread(target=self._fake_loop, daemon=True)
            self._fake_thread.start()

    def stop(self):
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _pa_callback(self, in_data, frame_count, time_info, status):
        try:
            self._ingest(np.frombuffer(in_data, dtype=np.int16).reshape(-1, self.in_channels))
        except Exception:
            pass
        return (None, pyaudio.paContinue)

    def _fake_loop(self):  # test tone source for non-Windows dev/testing
        t0, n = time.time(), 0
        freq = 440 if self.kind == "system" else 220
        while self._running:
            frames = self.BLOCK
            t = (np.arange(frames) + n) / self.rate
            amp = 0.2 if (int(t[0]) % 2 == 0) else 0.002  # alternate loud/quiet seconds
            s = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)
            self._ingest(np.stack([s] * self.in_channels, axis=1))
            n += frames
            delay = t0 + n / self.rate - time.time()
            if delay > 0:
                time.sleep(delay)

    def _ingest(self, pcm: np.ndarray):
        x = pcm.astype(np.float32) / 32768.0
        # channel fold to stereo
        if x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        elif x.shape[1] > 2:
            x = x[:, :2]
        if self.mono:
            m = x.mean(axis=1, keepdims=True)
            x = np.repeat(m, 2, axis=1)
        x = x * self.volume
        self.raw_level_db = db(float(np.max(np.abs(x))) if x.size else 0)
        if self.gate is not None:
            x = self.gate.process(x)
        if self.muted or (self.ptt_vk and not _key_down(self.ptt_vk)):
            x = x * 0.0
        np.clip(x, -1.0, 1.0, out=x)
        self.level_db = db(float(np.max(np.abs(x))) if x.size else 0)
        data = (x * 32767).astype(np.int16).tobytes()
        with self._sub_lock:
            for q in self._subs:
                q.append(data)


def _key_down(vk: int) -> bool:
    if not IS_WIN:
        return True
    return bool(ctypes_user32().GetAsyncKeyState(vk) & 0x8000)


_u32 = None


def ctypes_user32():
    global _u32
    if _u32 is None:
        import ctypes
        _u32 = ctypes.WinDLL("user32")
    return _u32


# ----------------------------------------------------------------------------- pipes to ffmpeg

class PipeFeeder(threading.Thread):
    """Serves one source to ffmpeg through a named pipe (Windows) or FIFO (POSIX).

    Keeps the byte stream locked to the wall clock: pads silence when the device is
    quiet, drops backlog if it builds up, so the audio track never drifts.
    """

    def __init__(self, source: Source, name: str):
        super().__init__(daemon=True)
        self.source = source
        self.path = (rf"\\.\pipe\{name}" if IS_WIN else f"/tmp/{name}.fifo")
        self.bytes_per_sec = source.rate * source.channels * 2
        self._halt = threading.Event()
        self._q = source.subscribe(maxlen=4000)
        self._handle = None
        self.connected = threading.Event()
        self._create()

    def _create(self):
        if IS_WIN:
            import ctypes
            k32 = ctypes.WinDLL("kernel32")
            k32.CreateNamedPipeW.restype = ctypes.c_void_p
            PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE, PIPE_WAIT = 0x2, 0x0, 0x0
            self._k32 = k32
            self._handle = k32.CreateNamedPipeW(self.path, PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT,
                                                1, 1 << 20, 1 << 20, 0, None)
        else:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            os.mkfifo(self.path)

    def run(self):
        try:
            if IS_WIN:
                self._k32.ConnectNamedPipe(ctypes_void(self._handle), None)
                write = self._win_write
            else:
                fd = os.open(self.path, os.O_WRONLY)
                write = lambda b: os.write(fd, b)  # noqa: E731
            self.connected.set()
            start = time.perf_counter()
            sent = 0
            frame = self.source.channels * 2
            while not self._halt.is_set():
                time.sleep(0.01)
                target = int((time.perf_counter() - start) * self.bytes_per_sec) // frame * frame
                chunks = []
                avail = 0
                while self._q:
                    c = self._q.popleft()
                    chunks.append(c)
                    avail += len(c)
                buf = b"".join(chunks)
                behind = target - sent
                if len(buf) < behind - self.bytes_per_sec // 20:  # >50ms short -> pad silence
                    buf += bytes(behind - len(buf))
                elif len(buf) > behind + self.bytes_per_sec // 4:  # >250ms backlog -> drop oldest
                    buf = buf[len(buf) - behind:]
                    buf = buf[len(buf) % frame:] if len(buf) % frame else buf
                if buf:
                    write(buf)
                    sent += len(buf)
        except (BrokenPipeError, OSError):
            pass
        finally:
            self.source.unsubscribe(self._q)
            self._close()

    def _win_write(self, b: bytes):
        import ctypes
        written = ctypes.c_ulong(0)
        ok = self._k32.WriteFile(ctypes_void(self._handle), b, len(b), ctypes.byref(written), None)
        if not ok:
            raise BrokenPipeError

    def _close(self):
        if IS_WIN and self._handle:
            try:
                self._k32.DisconnectNamedPipe(ctypes_void(self._handle))
                self._k32.CloseHandle(ctypes_void(self._handle))
            except Exception:
                pass
            self._handle = None
        elif not IS_WIN:
            try:
                os.unlink(self.path)
            except Exception:
                pass

    def stop(self):
        self._halt.set()
        if not self.connected.is_set():  # unblock a pending connect
            try:
                if IS_WIN:
                    import ctypes
                    h = self._k32.CreateFileW(self.path, 0x80000000, 0, None, 3, 0, None)
                    self._k32.CloseHandle(ctypes.c_void_p(h))
                else:
                    fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
                    os.close(fd)
            except Exception:
                pass


def ctypes_void(h):
    import ctypes
    return ctypes.c_void_p(h)


# ----------------------------------------------------------------------------- engine

VIRTUAL_HINTS = ("cable", "vb-audio", "voicemeeter", "virtual", "vac ", "line 1", "blackhole", "steam streaming")


def is_virtual_output(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in VIRTUAL_HINTS)


class AudioEngine:
    """Owns the two live sources. Ref-counted: meters, recorder and passthrough share them."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.system: Source | None = None
        self.mic: Source | None = None
        self._refs = 0
        self._lock = threading.RLock()
        self._passthrough: Passthrough | None = None
        self._pt_holds_ref = False
        self.passthrough_status = "Off"
        self.on_status = None  # callback(title, message, is_error) — set by the Controller
        self.gate_settings = GateSettings()
        self.reload_gate()
        cfg.on_change(self._on_cfg)

    def _notify(self, title, msg, error=False):
        if error:
            log.warning("%s: %s", title, msg)
        if self.on_status:
            try:
                self.on_status(title, msg, error)
            except Exception:
                pass

    def reload_gate(self):
        a = self.cfg.get("audio")
        g = self.gate_settings
        g.enabled = bool(a.get("gate_enabled", True))
        g.auto = bool(a.get("gate_auto", False))
        g.threshold_db = float(a.get("gate_threshold_db", -45))
        g.hold_ms = float(a.get("gate_hold_ms", 250))
        g.release_ms = float(a.get("gate_release_ms", 120))
        g.attack_ms = float(a.get("gate_attack_ms", 5))

    def _on_cfg(self, key, value):
        if not key.startswith("audio"):
            return
        self.reload_gate()
        a = self.cfg.get("audio")
        with self._lock:
            if self.system:
                self.system.volume = a["system_volume"] / 100
            if self.mic:
                self.mic.volume = a["mic_volume"] / 100
                self.mic.mono = a.get("mono_mic", False)
                self.mic.ptt_vk = _vk_for(a.get("ptt_key", "")) if a.get("push_to_talk") else 0
        if key in ("audio.system_device", "audio.mic_device", "audio.system_enabled", "audio.mic_enabled"):
            self.restart()
        if key in ("audio.passthrough_enabled", "audio.passthrough_device", "audio.mic_enabled", "audio.mic_device"):
            self._sync_passthrough()

    def acquire(self):
        with self._lock:
            self._refs += 1
            if self._refs == 1:
                self._open()

    def release(self):
        with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs == 0:
                self._close()

    def restart(self):
        with self._lock:
            if self._refs:
                self._close()
                self._open()

    def _open(self):
        a = self.cfg.get("audio")
        if a["system_enabled"]:
            self.system = Source("system", pick_device("loopback", a["system_device"]), a["system_volume"] / 100)
            self.system.start()
        if a["mic_enabled"]:
            self.mic = Source("mic", pick_device("mic", a["mic_device"]), a["mic_volume"] / 100,
                              gate=self.gate_settings, mono=a.get("mono_mic", False))
            self.mic.ptt_vk = _vk_for(a.get("ptt_key", "")) if a.get("push_to_talk") else 0
            self.mic.start()
        for s in (self.system, self.mic):
            if s and s.error:
                self._notify(f"{'PC audio' if s.kind == 'system' else 'Microphone'} unavailable", s.error, True)
        if self.mic:  # re-point an active virtual mic / monitor at the new mic
            for p in (self._passthrough, getattr(self, "_monitor", None)):
                if p:
                    p.retarget(self.mic)

    def _close(self):
        for s in (self.system, self.mic):
            if s:
                s.stop()
        self.system = self.mic = None

    # -- virtual mic passthrough (send the gated mic to e.g. VB-Cable for Discord/games)
    def _sync_passthrough(self):
        a = self.cfg.get("audio")
        want = a.get("passthrough_device") if a.get("passthrough_enabled") else ""
        with self._lock:
            if self._passthrough and (not want or self._passthrough.device_name != want):
                self._passthrough.stop()
                self._passthrough = None
            if not want:
                self.passthrough_status = ("Pick an output device first" if a.get("passthrough_enabled") else "Off")
                if self._pt_holds_ref:
                    self._pt_holds_ref = False
                    self.release()
                return
            if not a.get("mic_enabled"):
                self.passthrough_status = "Turn on 'Record microphone' first"
                self._notify("Virtual mic", self.passthrough_status, True)
                return
            if self._passthrough:
                return
            if not self._pt_holds_ref:
                self._pt_holds_ref = True
                self.acquire()
            if not self.mic:
                self.passthrough_status = "Microphone couldn't be opened"
                self._notify("Virtual mic", self.passthrough_status, True)
                return
            self.passthrough_status = "Starting…"
            self._passthrough = Passthrough(self.mic, want, self._pt_state)
            self._passthrough.start()

    # -- temporary "listen to my mic" monitoring (never persisted, auto-stops)
    def start_monitor(self, seconds: float = 120.0):
        with self._lock:
            if getattr(self, "_monitor", None):
                return
            self.acquire()
            if not self.mic:
                self.release()
                self._notify("Listen to mic", "Microphone is off or unavailable", True)
                return
            out = pick_device("output", "")  # Windows default output = your headphones
            if not out:
                self.release()
                return

            def state(ok, msg):
                if not ok:
                    self._notify("Listen to mic", msg, True)
                    self.stop_monitor()
            self._monitor = Passthrough(self.mic, out.name, state)
            self._monitor.start()
            self._monitor_deadline = time.time() + seconds
            threading.Thread(target=self._monitor_timeout, args=(self._monitor,), daemon=True).start()

    def _monitor_timeout(self, mon):
        while getattr(self, "_monitor", None) is mon:
            if time.time() > self._monitor_deadline:
                self.stop_monitor()
                break
            time.sleep(0.5)

    def stop_monitor(self):
        with self._lock:
            mon = getattr(self, "_monitor", None)
            if not mon:
                return
            self._monitor = None
            mon.stop()
            self.release()

    @property
    def monitoring(self) -> bool:
        return getattr(self, "_monitor", None) is not None

    def _pt_state(self, ok: bool, msg: str):
        self.passthrough_status = msg
        if not ok:
            self._notify("Virtual mic stopped", msg, True)
            with self._lock:
                self._passthrough = None
            self.cfg.set("audio.passthrough_enabled", False)
            self.passthrough_status = f"Stopped — {msg}"  # keep the reason visible (the set() above resets it)


class Passthrough(threading.Thread):
    """Plays the processed mic into an output device (normally a virtual cable).

    Runs a callback-driven PortAudio output stream on its own thread with a small
    jitter buffer: underruns play silence, overruns drop the oldest audio, so the
    virtual mic never drifts behind real time.
    """

    def __init__(self, src: Source, device_name: str, on_state):
        super().__init__(daemon=True, name="VirtualMic")
        self.src, self.device_name, self.on_state = src, device_name, on_state
        self._q = src.subscribe(maxlen=40)
        self._halt = threading.Event()
        self._buf = np.zeros((0, 2), dtype=np.int16)
        self._lock = threading.Lock()
        self.out_rate = 48000

    def retarget(self, src: Source):
        self.src.unsubscribe(self._q)
        self.src = src
        self._q = src.subscribe(maxlen=40)

    def _callback(self, in_data, frame_count, time_info, status):
        with self._lock:
            have = self._buf.shape[0]
            if have >= frame_count:
                out = self._buf[:frame_count]
                self._buf = self._buf[frame_count:]
            else:
                out = np.vstack([self._buf, np.zeros((frame_count - have, 2), dtype=np.int16)])
                self._buf = self._buf[:0]
        return (out.tobytes(), pyaudio.paContinue)

    def run(self):
        _com_init()
        dev = pick_device("output", self.device_name)
        pa = _get_pa()
        if pa is None:
            self.on_state(False, "Audio system unavailable")
            return
        if not dev or dev.name != self.device_name:
            self.on_state(False, f"Output device not found: {self.device_name}")
            return
        self.out_rate = dev.rate
        try:
            stream = pa.open(format=pyaudio.paInt16, channels=2, rate=dev.rate, output=True,
                             output_device_index=dev.index, frames_per_buffer=480, stream_callback=self._callback)
            stream.start_stream()
        except Exception as e:
            log.exception("virtual mic open failed")
            self.on_state(False, f"Couldn't open {self.device_name}: {e}")
            return
        self.on_state(True, f"Live → {self.device_name}")
        max_frames = int(dev.rate * 0.12)  # keep at most 120 ms queued
        try:
            while not self._halt.is_set():
                if not self._q:
                    time.sleep(0.003)
                    continue
                data = np.frombuffer(self._q.popleft(), dtype=np.int16).reshape(-1, 2)
                if dev.rate != self.src.rate:
                    data = resample(data, self.src.rate, dev.rate)
                with self._lock:
                    self._buf = np.vstack([self._buf, data])
                    if self._buf.shape[0] > max_frames:
                        self._buf = self._buf[-max_frames:]
                if not stream.is_active():
                    raise RuntimeError("output stream stopped")
        except Exception as e:
            log.exception("virtual mic failed")
            self.on_state(False, str(e))
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            self.src.unsubscribe(self._q)

    def stop(self):
        self._halt.set()


def resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    n_out = max(1, int(round(x.shape[0] * dst_rate / src_rate)))
    xi = np.linspace(0, x.shape[0] - 1, n_out)
    out = np.empty((n_out, x.shape[1]), dtype=np.float32)
    for c in range(x.shape[1]):
        out[:, c] = np.interp(xi, np.arange(x.shape[0]), x[:, c].astype(np.float32))
    return out.astype(np.int16)


def _com_init():
    """WASAPI needs COM on the thread that opens a stream (game-launch starts happen on a worker thread)."""
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.WinDLL("ole32").CoInitializeEx(None, 0)  # MTA; harmless if already initialized
    except Exception:
        pass


# ----------------------------------------------------------------------------- calibration + mic test

class LevelSampler:
    """Collects raw (pre-gate) block levels from a source for N seconds."""

    def __init__(self, src: Source, seconds: float):
        self.src, self.seconds = src, seconds
        self.levels: list[float] = []

    def run(self, progress=None, stop_flag=None):
        t0 = time.time()
        while time.time() - t0 < self.seconds:
            if stop_flag and stop_flag():
                break
            g = self.src.gate
            self.levels.append(g.last_level if g is not None else self.src.raw_level_db)
            if progress:
                progress((time.time() - t0) / self.seconds)
            time.sleep(0.02)
        return self.levels


def suggest_threshold(voice_levels: list[float], room_levels: list[float]) -> dict:
    """Pick a gate threshold between 'me talking' and 'room / wife talking'.

    Uses the loud part of your speech (30th percentile of voiced blocks) and the
    loud part of the room (98th percentile), and splits the gap — biased a bit
    toward the room side so your quiet words still get through.
    """
    v = np.array([x for x in voice_levels if x > -85]) if voice_levels else np.array([])
    r = np.array([x for x in room_levels if x > -95]) if room_levels else np.array([])
    if v.size < 10:
        return {"ok": False, "reason": "Didn't hear your voice — check the mic and try again."}
    voiced = v[v > np.percentile(v, 40)]
    you = float(np.percentile(voiced, 30))
    room = float(np.percentile(r, 98)) if r.size else -70.0
    gap = you - room
    if gap < 3:
        thr = you - 2
        return {"ok": True, "threshold": round(thr, 1), "you": round(you, 1), "room": round(room, 1),
                "gap": round(gap, 1), "warn": "Your voice and the room are almost the same level at the mic. "
                "Moving the mic closer to your mouth (or turning its gain down and talking closer) will help a lot."}
    thr = room + gap * 0.4
    return {"ok": True, "threshold": round(thr, 1), "you": round(you, 1), "room": round(room, 1),
            "gap": round(gap, 1), "warn": "" if gap >= 8 else "The gap is small — expect some of the room to leak through on loud moments."}


class MicTest:
    """Record N seconds of the processed mic, then return a WAV path for playback."""

    def __init__(self, src: Source, seconds: float, out_path: str):
        self.src, self.seconds, self.out_path = src, seconds, out_path

    def run(self, progress=None):
        q = self.src.subscribe(maxlen=100000)
        t0 = time.time()
        while time.time() - t0 < self.seconds:
            if progress:
                progress((time.time() - t0) / self.seconds)
            time.sleep(0.05)
        self.src.unsubscribe(q)
        data = b"".join(q)
        import wave
        with wave.open(self.out_path, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(self.src.rate)
            w.writeframes(data)
        return self.out_path


# ----------------------------------------------------------------------------- key names

VK = {**{f"F{i}": 0x6F + i for i in range(1, 25)},
      **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
      **{str(d): 0x30 + d for d in range(10)},
      "Space": 0x20, "Tab": 0x09, "CapsLock": 0x14, "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24,
      "End": 0x23, "PageUp": 0x21, "PageDown": 0x22, "Pause": 0x13, "ScrollLock": 0x91,
      "PrintScreen": 0x2C, "Mouse4": 0x05, "Mouse5": 0x06, "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB,
      "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
      "Num0": 0x60, "Num1": 0x61, "Num2": 0x62, "Num3": 0x63, "Num4": 0x64,
      "Num5": 0x65, "Num6": 0x66, "Num7": 0x67, "Num8": 0x68, "Num9": 0x69}


def _vk_for(name: str) -> int:
    if not name:
        return 0
    return VK.get(name.split("+")[-1], 0)
