"""Tiny synthesized alert sounds (no bundled audio files needed)."""
from __future__ import annotations

import sys
import threading
import wave
from pathlib import Path

import numpy as np

from .config import app_data_dir

IS_WIN = sys.platform == "win32"
RATE = 44100

STYLES = {
    # name: list of (freq, start, dur)
    "chime": {"clip": [(880, 0, .09), (1320, .08, .16)], "bookmark": [(660, 0, .08)],
              "screenshot": [(1500, 0, .04), (1000, .045, .05)], "start": [(523, 0, .1), (784, .1, .16)],
              "stop": [(784, 0, .1), (523, .1, .16)], "error": [(220, 0, .2), (180, .2, .25)]},
    "pop":   {"clip": [(600, 0, .05)], "bookmark": [(900, 0, .04)], "screenshot": [(1200, 0, .03)],
              "start": [(500, 0, .06), (700, .07, .06)], "stop": [(700, 0, .06), (500, .07, .06)],
              "error": [(200, 0, .2)]},
    "click": {"clip": [(2400, 0, .015), (2400, .06, .015)], "bookmark": [(2400, 0, .015)],
              "screenshot": [(3000, 0, .01)], "start": [(1800, 0, .02)], "stop": [(1200, 0, .02)],
              "error": [(300, 0, .15)]},
}


def _render(notes, volume: float) -> np.ndarray:
    total = max(s + d for _, s, d in notes) + 0.05
    out = np.zeros(int(total * RATE), dtype=np.float32)
    for f, s, d in notes:
        n = int(d * RATE)
        t = np.arange(n) / RATE
        env = np.minimum(1, t / 0.005) * np.exp(-t / (d * 0.45))
        tone = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)
        i = int(s * RATE)
        out[i:i + n] += (tone * env).astype(np.float32)
    out = out / max(1e-6, np.max(np.abs(out))) * 0.8 * volume
    return (out * 32767).astype(np.int16)


class Alerts:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = app_data_dir() / "sounds"
        self.dir.mkdir(exist_ok=True)
        self._key = None

    def _ensure(self):
        a = self.cfg.get("alerts")
        key = (a.get("sound"), a.get("volume"))
        if key == self._key:
            return
        style = STYLES.get(a.get("sound"), STYLES["chime"])
        vol = max(0, min(100, int(a.get("volume", 60)))) / 100
        import os
        for name, notes in style.items():
            data = _render(notes, vol)
            tmp = self.dir / f"{name}.tmp.wav"
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(data.tobytes())
            try:
                os.replace(tmp, self.dir / f"{name}.wav")
            except OSError:
                pass  # file is playing right now; it gets refreshed next time
        self._key = key

    def _custom_wav(self, src: str, vol: float) -> str | None:
        """Convert any audio file (mp3/ogg/m4a/flac/opus/wav…) to a short WAV at the chosen volume."""
        p = Path(src)
        if not p.exists():
            return None
        key = (str(p), p.stat().st_mtime, round(vol, 2))
        out = self.dir / "custom.wav"
        if getattr(self, "_custom_key", None) == key and out.exists():
            return str(out)
        from . import ffmpeg as ff
        gain = max(vol, 0.001)
        r = ff.run([ff.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-i", str(p), "-t", "6", "-vn",
                    "-af", f"volume={gain:.3f}", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out)], timeout=20)
        if r.returncode != 0:
            return None
        self._custom_key = key
        return str(out)

    def play(self, name: str, force: bool = False):
        """Never raises — a sound problem must not stop a clip from saving."""
        try:
            self._play(name, force)
        except Exception:
            import logging
            logging.getLogger("clipvault.sounds").exception("alert sound failed")

    def _play(self, name: str, force: bool):
        a = self.cfg.get("alerts")
        gate = {"clip": "clip", "bookmark": "bookmark", "screenshot": "screenshot", "start": "session",
                "stop": "session"}.get(name)
        if not force and gate and not a.get(gate, True):
            return
        vol = max(0, min(100, int(a.get("volume", 60)))) / 100
        path = None
        if a.get("sound") == "custom" and a.get("custom_path") and name in ("clip", "bookmark"):
            path = self._custom_wav(a["custom_path"], vol)
        if path is None:
            self._ensure()
            path = str(self.dir / f"{name}.wav")
        if vol <= 0:
            return
        if IS_WIN:
            import winsound
            threading.Thread(target=winsound.PlaySound, args=(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT),
                             daemon=True).start()
