"""Settings model + persistence for ClipVault.

Everything lives in one JSON file under %APPDATA%\\ClipVault (or ~/.clipvault
elsewhere) so it survives reinstalls and is easy to back up.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path

APP_NAME = "ClipVault"
APP_VERSION = "1.0.0"


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    p = Path(base) / APP_NAME if base else Path.home() / ".clipvault"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_storage_dir() -> Path:
    vids = Path.home() / "Videos"
    return (vids if vids.exists() else Path.home()) / "ClipVault"


# Quality presets — values feed straight into the ffmpeg command builder.
QUALITY_PRESETS = {
    "low":      {"label": "Low",      "desc": "720p · 30 FPS", "height": 720,  "fps": 30, "bitrate": 6,  "codec": "h264"},
    "standard": {"label": "Standard", "desc": "1080p · 60 FPS",          "height": 1080, "fps": 60, "bitrate": 15, "codec": "h264"},
    "high":     {"label": "High",     "desc": "Native · 60 FPS",              "height": 0,    "fps": 60, "bitrate": 40, "codec": "hevc"},
    "ultra":    {"label": "Ultra",    "desc": "Native · 120 FPS", "height": 0,  "fps": 120, "bitrate": 80, "codec": "hevc"},
}

EXTRA_IGNORED_V2 = ["Microsoft.Media.Player.exe", "vlc.exe", "mpc-hc64.exe", "PotPlayerMini64.exe", "Video.UI.exe",
                    "ApplicationFrameHost.exe", "msedgewebview2.exe", "Photos.exe", "obs64.exe", "Unity Hub.exe",
                    "ShellExperienceHost.exe", "SearchHost.exe", "LockApp.exe", "steam.exe", "python.exe",
                    "pythonw.exe", "Photoshop.exe", "Premiere Pro.exe", "OculusClient.exe", "vrmonitor.exe",
                    "vrserver.exe", "Taskmgr.exe", "mmc.exe", "SystemSettings.exe"]

EXTRA_IGNORED_V3 = ["Elgato.Studio.exe", "Elgato Studio.exe", "Camera Hub.exe", "StreamDeck.exe", "WaveLink.exe",
                    "4KCaptureUtility.exe", "ControlCenter.exe", "Wallpaper64.exe", "wallpaper32.exe",
                    "NVIDIA Broadcast.exe", "RadeonSoftware.exe", "AMDRSServ.exe"]

DEFAULTS: dict = {
    "theme": "ynzie",  # accent theme key, see ui/theme.py
    "first_run": True,
    "recording": {
        "mode": "auto",            # manual | auto (game detected) | always
        "full_session": True,      # auto-start a long recording for the whole game session (Medal's default)
        "session_key": "bookmark", # what the clip key does during a long recording: bookmark | clip | both
        "long_press_split": True,  # hold the clip key ~1 s during a session to end it and start a new one
        "review_after_game": True, # pop up the keep/delete/clip review when the game closes
        "clip_alt_seconds": 300,
        "monitor": 0,              # ddagrab output_idx
        "clip_seconds": 60,        # length saved by the clip hotkey
        "buffer_seconds": 300,     # how much history is kept on disk
        "segment_seconds": 2,
        "show_cursor": False,
        "framerate_mode": "vfr",   # vfr | cfr
        "games": ["iw4x.exe", "iw4m.exe", "VRChat.exe", "Rocksmith2014.exe", "Lethal Company.exe",
                  "cod.exe", "cs2.exe", "VALORANT-Win64-Shipping.exe", "r5apex.exe",
                  "FortniteClient-Win64-Shipping.exe", "javaw.exe"],
        "ignored": ["explorer.exe", "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "Discord.exe",
                    "Spotify.exe", "ClipVault.exe", "Medal.exe", "steamwebhelper.exe", "AfterFX.exe",
                    "Adobe Media Encoder.exe", "Code.exe", "devenv.exe", "Unity.exe", "blender.exe"],
        "detect_fullscreen": True,  # treat any fullscreen app (not ignored) as a game
    },
    "quality": {
        "preset": "high",
        "height": 0,       # 0 = native
        "fps": 60,
        "bitrate": 40,     # Mbps
        "codec": "hevc",   # h264 | hevc | av1
        "encoder": "auto", # auto | amf | nvenc | qsv | cpu
        "rate_control": "vbr",  # cbr | vbr | cqp
        "cq": 20,
        "speed": "balanced",    # speed | balanced | quality
        "keyframe_sec": 1,
        "zero_copy": True,      # feed GPU frames straight to the encoder when possible
    },
    "hdr": {
        "detect": "auto",       # auto | on | off  (is the display in HDR?)
        "mode": "tonemap",      # tonemap (HDR->SDR fix) | hdr10 (keep HDR) | windows (DDA built-in)
        "engine": "gpu",        # gpu (libplacebo) | cpu (zscale)
        "curve": "bt.2390",     # bt.2390 | hable | mobius | reinhard | spline | st2094-40 | clip
        "auto_white": True,     # use Windows' SDR content brightness as reference white
        "white_nits": 203,      # manual reference white when auto_white is off
        "peak_detect": True,    # dynamic per-scene brightness
        "brightness": 0.0,      # -0.3..0.3
        "contrast": 1.0,        # 0.5..1.5
        "saturation": 1.0,      # 0.5..1.5
        "gamma": 1.0,           # 0.6..1.6
        "contrast_recovery": 0.3,
    },
    "audio": {
        "system_enabled": True,
        "system_device": "",     # "" = default output (loopback)
        "system_volume": 100,
        "mic_enabled": True,
        "mic_device": "",        # "" = default input
        "mic_volume": 100,
        "separate_tracks": True,
        "noise_suppression": True,
        "noise_floor_db": -50,   # afftdn noise floor (-80 .. -20)
        "push_to_talk": False,
        "ptt_key": "",
        "mono_mic": False,
        "bitrate_k": 192,
        "sync_offset_ms": 0,
        # Discord-style input sensitivity (noise gate) on the mic
        "gate_enabled": True,
        "gate_auto": False,
        "gate_threshold_db": -45,
        "gate_attack_ms": 5,
        "gate_hold_ms": 250,
        "gate_release_ms": 120,
        # send the processed mic to a virtual cable (VB-Cable) so Discord/games get it too
        "passthrough_enabled": False,
        "passthrough_device": "",
    },
    "hotkeys": {
        "clip": "F8",
        "clip_alt": "",
        "long_recording": "Alt+F7",
        "bookmark": "F9",
        "screenshot": "F7",
    },
    "storage": {
        "folder": str(default_storage_dir()),
        "limit_gb": 0,          # 0 = no limit
        "auto_delete": False,
        "only_delete_sessions": True,
        "trash_days": 30,
        "scan_storage": True,     # also show other videos already in the capture folder (Medal/NVIDIA/OBS…)
        "linked_folders": [],     # extra folders to link (files stay where they are)
    },
    "alerts": {
        "clip": True,
        "bookmark": True,
        "screenshot": True,
        "session": True,
        "volume": 60,
        "sound": "chime",       # chime | pop | click | custom
        "custom_path": "",
        "toast": True,
    },
    "editor": {
        "default_export": "copy",   # copy (instant, keyframe accurate) | precise (re-encode)
        "export_preset": "source",
    },
    "app": {
        "start_minimized": False,
        "close_to_tray": True,
        "launch_on_startup": True,
        "overlay": True,             # "Clip saved" pop-up over the game
        "overlay_pos": "top-right",  # top-right | top-left | bottom-right | bottom-left
        "hover_preview": True,       # hover a card to scrub through the video
    },
    "controller": {                  # Xbox / XInput controller shortcuts (button names joined with +)
        "enabled": True,
        "clip": "View+Menu",
        "bookmark": "",
        "screenshot": "",
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Thread-safe dict-ish settings with dotted access and change listeners."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else (app_data_dir() / "settings.json")
        self._lock = threading.RLock()
        self._listeners: list = []
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
            except Exception:
                data = {}
        self.data = _merge(DEFAULTS, data)
        self._migrate(int(data.get("cfg_version", 1)))

    def _migrate(self, v: int):
        if v < 2:  # more never-a-game apps (video players, overlays, tools)
            ign = list(self.data["recording"].get("ignored", []))
            for exe in EXTRA_IGNORED_V2:
                if exe.lower() not in (x.lower() for x in ign):
                    ign.append(exe)
            self.data["recording"]["ignored"] = ign
            self.data["cfg_version"] = 2
        if v < 3:  # capture-card / streaming / wallpaper apps (skip anything you added as a game)
            ign = list(self.data["recording"].get("ignored", []))
            mine = {g.lower() for g in self.data["recording"].get("games", [])}
            for exe in EXTRA_IGNORED_V3:
                if exe.lower() not in (x.lower() for x in ign) and exe.lower() not in mine:
                    ign.append(exe)
            self.data["recording"]["ignored"] = ign
            self.data["cfg_version"] = 3
        if v < 4:  # the second clip key ships bound (Shift+F8) unless that combo is already taken
            hk = self.data["hotkeys"]
            if not hk.get("clip_alt") and "shift+f8" not in {str(x).lower() for x in hk.values()}:
                hk["clip_alt"] = "Shift+F8"
            self.data["cfg_version"] = 4

    def get(self, dotted: str, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value, save: bool = True):
        with self._lock:
            parts = dotted.split(".")
            cur = self.data
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            if cur.get(parts[-1]) == value:
                return
            cur[parts[-1]] = value
        if save:
            self.save()
        for fn in list(self._listeners):
            try:
                fn(dotted, value)
            except Exception:
                pass

    def on_change(self, fn):
        self._listeners.append(fn)

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
            os.replace(tmp, self.path)

    def apply_preset(self, key: str):
        p = QUALITY_PRESETS.get(key)
        if not p:
            self.set("quality.preset", "custom")
            return
        with self._lock:
            q = self.data["quality"]
            q.update({"preset": key, "height": p["height"], "fps": p["fps"],
                      "bitrate": p["bitrate"], "codec": p["codec"]})
        self.save()
        for fn in list(self._listeners):
            fn("quality", self.data["quality"])

    @property
    def storage(self) -> Path:
        p = Path(self.get("storage.folder") or default_storage_dir())
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:  # drive unplugged/offline — fall back instead of crashing
            fb = default_storage_dir()
            fb.mkdir(parents=True, exist_ok=True)
            return fb


def resource_path(*parts) -> Path:
    """Path to bundled files (works in PyInstaller one-folder/one-file builds)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base.joinpath(*parts)
