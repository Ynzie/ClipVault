"""Locating ffmpeg, probing GPU encoders, and building every ffmpeg command ClipVault runs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from . import hdr as hdrmod
from .config import resource_path

IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0
LOW_PRIORITY = (0x08000000 | 0x00004000) if IS_WIN else 0  # no window + BELOW_NORMAL_PRIORITY_CLASS

CODEC_ENCODERS = {
    "amf":   {"h264": "h264_amf",   "hevc": "hevc_amf",   "av1": "av1_amf"},
    "nvenc": {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"},
    "qsv":   {"h264": "h264_qsv",   "hevc": "hevc_qsv",   "av1": "av1_qsv"},
    "cpu":   {"h264": "libx264",    "hevc": "libx265",    "av1": "libsvtav1"},
}
VENDOR_LABEL = {"amf": "AMD (AMF)", "nvenc": "NVIDIA (NVENC)", "qsv": "Intel (Quick Sync)", "cpu": "CPU (software)"}


def ffmpeg_path() -> str:
    for cand in (resource_path("ffmpeg", "ffmpeg.exe"), resource_path("ffmpeg", "bin", "ffmpeg.exe"),
                 Path(sys.executable).parent / "ffmpeg" / "ffmpeg.exe",
                 Path(__file__).resolve().parents[2] / "ffmpeg" / "ffmpeg.exe"):
        if cand.exists():
            return str(cand)
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_path() -> str:
    ff = Path(ffmpeg_path())
    probe = ff.with_name("ffprobe.exe" if IS_WIN else "ffprobe")
    return str(probe) if probe.exists() else (shutil.which("ffprobe") or "ffprobe")


def run(args: list[str], timeout: float | None = None, low_priority: bool = False, **kw) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe. low_priority: background chores (thumbnails, waveforms) never compete with your game."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          creationflags=LOW_PRIORITY if low_priority else NO_WINDOW, errors="replace", **kw)


def popen(args: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(args, creationflags=NO_WINDOW, **kw)


# ----------------------------------------------------------------------------- capability probing

@lru_cache(maxsize=1)
def available_encoders() -> set[str]:
    try:
        out = run([ffmpeg_path(), "-hide_banner", "-encoders"], timeout=15).stdout
    except Exception:
        return set()
    return {m.group(1) for m in re.finditer(r"^\s*V\S*\s+(\S+)", out, re.M)}


@lru_cache(maxsize=32)
def encoder_works(name: str, ten_bit: bool = False) -> bool:
    """Actually open the encoder (drivers can be missing even if ffmpeg lists it)."""
    if name not in available_encoders():
        return False
    pix = "p010le" if ten_bit else "nv12"
    args = [ffmpeg_path(), "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
            "color=c=black:s=640x360:r=30", "-frames:v", "3", "-pix_fmt", pix, "-c:v", name, "-f", "null", "-"]
    try:
        return run(args, timeout=20).returncode == 0
    except Exception:
        return False


@lru_cache(maxsize=16)
def has_filter(name: str) -> bool:
    try:
        out = run([ffmpeg_path(), "-hide_banner", "-filters"], timeout=15).stdout
    except Exception:
        return False
    return re.search(rf"\s{re.escape(name)}\s", out) is not None


@lru_cache(maxsize=1)
def gpu_tonemap_ok() -> bool:
    """libplacebo present AND a Vulkan device actually initializes (needs a GPU driver)."""
    if not has_filter("libplacebo"):
        return False
    try:
        r = run([ffmpeg_path(), "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64",
                 "-vf", "format=gbrp10le,libplacebo=format=yuv420p", "-frames:v", "1", "-f", "null", "-"], timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def resolve_encoder(vendor: str, codec: str, ten_bit: bool = False) -> tuple[str, str]:
    """Return (vendor, encoder_name), falling back down the chain if needed."""
    order = [vendor] if vendor != "auto" else []
    order += [v for v in ("amf", "nvenc", "qsv", "cpu") if v not in order]
    for v in order:
        for c in (codec, "hevc", "h264"):
            if ten_bit and c == "h264":
                continue
            name = CODEC_ENCODERS[v][c]
            if encoder_works(name, ten_bit):
                return v, name
    return "cpu", "libx264"


def detected_vendors() -> list[str]:
    return [v for v in ("amf", "nvenc", "qsv", "cpu") if encoder_works(CODEC_ENCODERS[v]["h264"])]


# ----------------------------------------------------------------------------- monitors

def probe_monitors(max_outputs: int = 6) -> list[dict]:
    """Ask ddagrab which outputs exist. Returns [{idx, w, h, x, y}]."""
    mons = []
    if not IS_WIN:
        return [{"idx": 0, "w": 1920, "h": 1080, "x": 0, "y": 0}]
    for i in range(max_outputs):
        args = [ffmpeg_path(), "-hide_banner", "-v", "verbose", "-f", "lavfi", "-i",
                f"ddagrab=output_idx={i}:framerate=1", "-frames:v", "1", "-f", "null", "-"]
        try:
            r = run(args, timeout=10)
        except Exception:
            break
        if r.returncode != 0:
            break
        text = r.stderr
        m = re.search(r"(\d{3,5})x(\d{3,5})", text)
        rect = re.search(r"\((-?\d+)\s*,\s*(-?\d+)\)\s*-\s*\((-?\d+)\s*,\s*(-?\d+)\)", text)
        d = {"idx": i, "w": int(m.group(1)) if m else 0, "h": int(m.group(2)) if m else 0, "x": 0, "y": 0}
        if rect:
            d["x"], d["y"] = int(rect.group(1)), int(rect.group(2))
            d["w"], d["h"] = int(rect.group(3)) - d["x"], int(rect.group(4)) - d["y"]
        mons.append(d)
    return mons or [{"idx": 0, "w": 0, "h": 0, "x": 0, "y": 0}]


def gdi_name_for_monitor(mon: dict) -> str | None:
    """Match a ddagrab output to a Windows display name by desktop position."""
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD), ("szDevice", ctypes.c_wchar * 32)]
        found = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

        def cb(hmon, hdc, rect, data):
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
            user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi))
            found.append((mi.rcMonitor.left, mi.rcMonitor.top, mi.szDevice))
            return 1
        user32.EnumDisplayMonitors(None, None, CB(cb), None)
        for x, y, name in found:
            if x == mon.get("x") and y == mon.get("y"):
                return name
        return found[mon.get("idx", 0)][2] if mon.get("idx", 0) < len(found) else None
    except Exception:
        return None


# ----------------------------------------------------------------------------- probe media

def probe_checked(path: str, timeout: float = 30, low_priority: bool = False) -> tuple[dict, str]:
    """ffprobe a file and say how it went: 'ok', 'timeout' (slow, not broken) or 'error' (unreadable).
    Unfinished Medal sessions are fragmented MP4s with thousands of fragments: ffprobe reads every one of
    them, which takes a while on a hard drive - that's slow, not broken."""
    try:
        r = run([ffprobe_path(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                timeout=timeout, low_priority=low_priority)
    except subprocess.TimeoutExpired:
        return {}, "timeout"
    except Exception:
        return {}, "error"
    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        info = {}
    return info, ("ok" if info.get("streams") else "error")


def probe(path: str, low_priority: bool = False, timeout: float = 30) -> dict:
    return probe_checked(path, timeout, low_priority)[0]


def duration_of(path: str) -> float:
    info = probe(path)
    try:
        return float(info.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


# ----------------------------------------------------------------------------- recording command

def video_encoder_args(vendor: str, enc: str, q: dict, fps: int, ten_bit: bool) -> list[str]:
    rc = q.get("rate_control", "vbr")
    br = int(q.get("bitrate", 40))
    cq = int(q.get("cq", 20))
    speed = q.get("speed", "balanced")
    gop = max(1, int(fps * float(q.get("keyframe_sec", 1))))
    a = ["-c:v", enc, "-g", str(gop)]
    if vendor == "amf":
        qual = {"speed": "speed", "balanced": "balanced", "quality": "quality"}[speed]
        a += ["-usage", "transcoding", "-quality", qual]
        if rc == "cqp":
            a += ["-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq + 2)]
            if enc != "av1_amf":
                a += ["-qp_b", str(cq + 4)]
        else:
            a += ["-rc", "cbr" if rc == "cbr" else "vbr_peak", "-b:v", f"{br}M",
                  "-maxrate", f"{int(br * (1 if rc == 'cbr' else 1.5))}M", "-bufsize", f"{br * 2}M"]
        if ten_bit and enc == "hevc_amf":
            a += ["-profile:v", "main10"]
    elif vendor == "nvenc":
        a += ["-preset", {"speed": "p2", "balanced": "p4", "quality": "p6"}[speed], "-tune", "hq"]
        if rc == "cqp":
            a += ["-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
        else:
            a += ["-rc", rc, "-b:v", f"{br}M", "-maxrate", f"{int(br * 1.5)}M", "-bufsize", f"{br * 2}M"]
        if ten_bit and enc == "hevc_nvenc":
            a += ["-profile:v", "main10"]
    elif vendor == "qsv":
        a += ["-preset", {"speed": "veryfast", "balanced": "medium", "quality": "slower"}[speed]]
        a += ["-global_quality", str(cq)] if rc == "cqp" else ["-b:v", f"{br}M", "-maxrate", f"{int(br*1.5)}M"]
    else:
        preset = {"speed": "ultrafast", "balanced": "veryfast", "quality": "medium"}[speed]
        if enc == "libsvtav1":
            a += ["-preset", {"speed": "12", "balanced": "10", "quality": "8"}[speed]]
        else:
            a += ["-preset", preset]
        a += ["-crf", str(cq)] if rc == "cqp" else ["-b:v", f"{br}M", "-maxrate", f"{int(br*1.5)}M",
                                                    "-bufsize", f"{br*2}M"]
    if enc.startswith("hevc") or enc == "libx265":
        a += ["-tag:v", "hvc1"]
    return a


def scaled_size(src_w: int, src_h: int, target_h: int) -> tuple[int | None, int | None]:
    if not target_h or not src_h or target_h >= src_h:
        return None, None
    w = int(round(src_w * target_h / src_h / 2)) * 2
    return w, target_h


class RecordPlan:
    """Everything the recorder needs to launch one buffer run."""

    def __init__(self, args: list[str], vendor: str, encoder: str, hdr_mode: str, zero_copy: bool,
                 description: str, audio_titles: list[str] | None = None):
        self.audio_titles = audio_titles or []
        self.args, self.vendor, self.encoder = args, vendor, encoder
        self.hdr_mode, self.zero_copy, self.description = hdr_mode, zero_copy, description


def build_record_command(cfg, mon: dict, audio_inputs: list[dict], run_dir: Path, t0: float,
                         display_hdr: hdrmod.DisplayHDR | None, force_cpu_path: bool = False) -> RecordPlan:
    """audio_inputs: [{'pipe': path, 'rate': 48000, 'kind': 'system'|'mic'}]"""
    q = cfg.get("quality")
    h = cfg.get("hdr")
    rec = cfg.get("recording")
    fps = int(q.get("fps", 60))

    hdr_on = {"on": True, "off": False}.get(h.get("detect", "auto"),
                                           bool(display_hdr and display_hdr.hdr_enabled))
    hdr_mode = "sdr"
    if hdr_on:
        hdr_mode = {"tonemap": "tonemap", "hdr10": "hdr10", "windows": "windows"}.get(h.get("mode"), "tonemap")
        if hdr_mode == "tonemap" and h.get("engine") == "gpu" and not gpu_tonemap_ok():
            h = dict(h, engine="cpu")
    ten_bit = hdr_mode == "hdr10"
    codec = q.get("codec", "hevc")
    if ten_bit and codec == "h264":
        codec = "hevc"
    vendor, enc = resolve_encoder(q.get("encoder", "auto"), codec, ten_bit)

    out_w, out_h = scaled_size(mon.get("w", 0), mon.get("h", 0), int(q.get("height", 0)))
    cfr = rec.get("framerate_mode") == "cfr"
    dda = (f"ddagrab=output_idx={mon.get('idx', 0)}:framerate={fps}"
           f":draw_mouse={1 if rec.get('show_cursor') else 0}:dup_frames={1 if cfr else 0}"
           f":output_fmt={'10bit' if hdr_mode in ('tonemap', 'hdr10') else '8bit'}")

    zero_copy = (q.get("zero_copy", True) and not force_cpu_path and hdr_mode in ("sdr", "windows")
                 and out_w is None and vendor in ("amf", "nvenc"))

    if zero_copy:
        vchain = "null"
    elif hdr_mode == "tonemap":
        vchain = "hwdownload,format=x2bgr10le,format=gbrp10le," + hdrmod.tonemap_chain(h, display_hdr, out_w, out_h)
    elif hdr_mode == "hdr10":
        vchain = "hwdownload,format=x2bgr10le," + hdrmod.hdr10_chain(out_w, out_h)
    else:
        size = f"{out_w}:{out_h}:flags=bicubic:" if out_w else ""
        vchain = f"hwdownload,format=bgra,scale={size}out_color_matrix=bt709:out_range=tv,format=nv12"
    if cfr and not zero_copy:
        vchain += f",fps={fps}"

    if not IS_WIN:  # dev/test harness: synthetic source standing in for ddagrab
        dda = f"testsrc2=size=1280x720:rate={fps}"
        if hdr_mode in ("tonemap", "hdr10"):
            fake = ("zscale=tin=bt709:min=bt709:pin=bt709:rin=tv:t=smpte2084:p=bt2020:m=gbr:r=full:npl=203,"
                    "format=gbrp10le,format=x2bgr10le")
        else:
            fake = "format=bgra"
        vchain = vchain.replace("hwdownload,format=x2bgr10le", fake, 1).replace("hwdownload,format=bgra", fake, 1)

    common_in = ["-use_wallclock_as_timestamps", "1", "-itsoffset", f"{-t0:.6f}"]
    args = [ffmpeg_path(), "-hide_banner", "-loglevel", "warning", "-nostats", "-y", "-copyts"]
    # (no -thread_queue_size on inputs: FFmpeg 8+ rejects it there and sizes input queues itself)
    args += common_in + ["-f", "lavfi", "-i", dda]

    a = cfg.get("audio")
    sync = float(a.get("sync_offset_ms", 0)) / 1000.0
    fc = [f"[0:v]{vchain}[v]"]
    labels = []
    for i, ai in enumerate(audio_inputs, start=1):
        args += ["-use_wallclock_as_timestamps", "1", "-itsoffset", f"{-t0 + sync:.6f}",
                 "-f", "s16le", "-ar", str(ai["rate"]), "-ac", "2", "-i", ai["pipe"]]
        chain = "aresample=48000:async=1000:min_hard_comp=0.1"
        if ai["kind"] == "mic" and a.get("noise_suppression"):
            chain += f",afftdn=nf={int(a.get('noise_floor_db', -50))}:nr=18:tn=1"
        fc.append(f"[{i}:a]{chain},asplit=2[{ai['kind']}A][{ai['kind']}B]")
        labels.append(ai["kind"])

    maps = ["-map", "[v]"]
    titles = []
    if len(labels) == 2:
        fc.append(f"[{labels[0]}A][{labels[1]}A]amix=inputs=2:normalize=0:dropout_transition=0[mix]")
        maps += ["-map", "[mix]"]
        titles.append("Mixed")
        if a.get("separate_tracks", True):
            for l in labels:
                maps += ["-map", f"[{l}B]"]
                titles.append("Game / PC audio" if l == "system" else "Microphone")
        else:
            fc += [f"[{l}B]anullsink" for l in labels]
    elif len(labels) == 1:
        maps += ["-map", f"[{labels[0]}A]"]
        titles.append("Game / PC audio" if labels[0] == "system" else "Microphone")
        fc.append(f"[{labels[0]}B]anullsink")

    args += ["-filter_complex", ";".join(fc)] + maps
    args += video_encoder_args(vendor, enc, q, fps, ten_bit)
    if ten_bit:
        args += hdrmod.HDR10_ENCODER_FLAGS
    elif not zero_copy:
        args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
    if labels:
        args += ["-c:a", "aac", "-b:a", f"{int(a.get('bitrate_k', 192))}k", "-ar", "48000"]
        for i, t in enumerate(titles):
            args += [f"-metadata:s:a:{i}", f"title={t}"]
    seg = int(rec.get("segment_seconds", 2))
    args += ["-max_muxing_queue_size", "4096", "-f", "segment", "-segment_time", str(seg),
             "-segment_format", "mpegts", "-reset_timestamps", "0",
             "-segment_list", str(run_dir / "segments.csv"), "-segment_list_type", "csv",
             str(run_dir / "seg_%06d.ts")]

    desc = f"{VENDOR_LABEL.get(vendor, vendor)} · {enc} · {'zero-copy' if zero_copy else 'filtered'}"
    if hdr_mode == "tonemap":
        desc += f" · HDR→SDR ({h.get('engine', 'gpu').upper()})"
    elif hdr_mode == "hdr10":
        desc += " · HDR10"
    plan = RecordPlan(args, vendor, enc, hdr_mode, zero_copy, desc, titles)
    plan.signature = (enc, hdr_mode, zero_copy, out_w, out_h, mon.get("w"), mon.get("h"), fps, tuple(titles),
                      q.get("rate_control"), cfr)
    return plan


# ----------------------------------------------------------------------------- screenshots & previews

def screenshot_command(cfg, mon: dict, out_png: Path, display_hdr, raw_hdr_png: Path | None = None) -> list[str]:
    h = cfg.get("hdr")
    hdr_on = {"on": True, "off": False}.get(h.get("detect", "auto"), bool(display_hdr and display_hdr.hdr_enabled))
    tonemap = hdr_on and h.get("mode") in ("tonemap", "hdr10")
    if tonemap and h.get("engine") == "gpu" and not gpu_tonemap_ok():
        h = dict(h, engine="cpu")
    dda = f"ddagrab=output_idx={mon.get('idx', 0)}:framerate=5:draw_mouse=0:output_fmt={'10bit' if tonemap else '8bit'}"
    args = [ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", dda]
    if tonemap:
        chain = ("hwdownload,format=x2bgr10le,format=gbrp10le,"
                 + hdrmod.tonemap_chain(h, display_hdr, None, None, out_fmt="yuv420p")
                 + ",scale=in_color_matrix=bt709:in_range=tv,format=rgb24")
        if raw_hdr_png:  # also keep the untouched HDR frame for the tuning preview
            args += ["-filter_complex", f"[0:v]hwdownload,format=x2bgr10le,split=2[a][b];"
                                        f"[a]format=gbrp10le,{chain.split(',', 2)[2]}[o];[b]format=rgb48le[r]",
                     "-map", "[o]", "-frames:v", "1", str(out_png), "-map", "[r]", "-frames:v", "1", str(raw_hdr_png)]
            return args
        args += ["-vf", chain]
    else:
        args += ["-vf", "hwdownload,format=bgra,format=rgb24"]
    return args + ["-frames:v", "1", str(out_png)]


def grab_raw_hdr_command(mon: dict, out_png: Path) -> list[str]:
    """Grab one untouched 10-bit HDR frame (PQ values stored in a 16-bit PNG)."""
    dda = f"ddagrab=output_idx={mon.get('idx', 0)}:framerate=5:draw_mouse=0:output_fmt=10bit"
    return [ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", dda,
            "-vf", "hwdownload,format=x2bgr10le,format=rgb48le", "-frames:v", "1", str(out_png)]


def preview_tonemap_command(hdr_cfg: dict, display_hdr, raw_png: Path, out_png: Path, width: int = 960) -> list[str]:
    h = dict(hdr_cfg)
    if h.get("engine") == "gpu" and not gpu_tonemap_ok():
        h["engine"] = "cpu"
    chain = "format=gbrp16le," + hdrmod.tonemap_chain(h, display_hdr, None, None, out_fmt="yuv420p")
    chain += f",scale={width}:-2:in_color_matrix=bt709:in_range=tv,format=rgb24"
    return [ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-i", str(raw_png), "-vf", chain,
            "-frames:v", "1", str(out_png)]


def preview_naive_command(raw_png: Path, out_png: Path, width: int = 960) -> list[str]:
    """What an HDR clip looks like when nothing fixes it (PQ shown as if SDR: washed out)."""
    return [ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-i", str(raw_png),
            "-vf", f"scale={width}:-2,format=rgb24", "-frames:v", "1", str(out_png)]


# ----------------------------------------------------------------------------- clip / export

def concat_command(list_file: Path, out: Path, audio_titles: list[str] | None = None,
                   hdr10: bool = False) -> list[str]:
    args = [ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-map", "0", "-c", "copy", "-bsf:a", "aac_adtstoasc", "-avoid_negative_ts", "make_zero"]
    for i, t in enumerate(audio_titles or []):
        args += [f"-metadata:s:a:{i}", f"title={t}", f"-metadata:s:a:{i}", f"handler_name={t}"]
    if hdr10:
        args += hdrmod.HDR10_ENCODER_FLAGS
    return args + ["-movflags", "+faststart", str(out)]


def thumbnail_command(src: str, out_jpg: Path, at: float = 1.0) -> list[str]:
    return [ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-ss", f"{max(0.0, at):.2f}", "-i", src,
            "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out_jpg)]


def waveform_command(src: str, stream: int = 0, rate: int = 2000, start: float | None = None,
                     length: float | None = None) -> list[str]:
    """Decode one audio track to 2 kHz mono for drawing (optionally just a time range, input-seeked)."""
    rng = []
    if start is not None:
        rng += ["-ss", f"{max(0.0, start):.3f}"]
    if length is not None:
        rng += ["-t", f"{length:.3f}"]
    return [ffmpeg_path(), "-hide_banner", "-v", "error", *rng, "-i", src, "-map", f"0:a:{stream}?",
            "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]


ASPECTS = {"9:16": (9, 16, 1080, 1920), "1:1": (1, 1, 1080, 1080), "4:5": (4, 5, 1080, 1350)}


def filter_path(p) -> str:
    """A file path as a filter option value. Escaped twice (option level, then filtergraph level), which is
    what ffmpeg needs for Windows drive colons and names like O'Brien."""
    s = str(p).replace("\\", "/")
    s = re.sub(r"([\\':])", r"\\\1", s)
    return re.sub(r"([\\'\[\],;])", r"\\\1", s)


def caption_font() -> str | None:
    for f in (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(f):
            return f
    return None


def effects_filter(aspect: str | None = None, crop_x: float = 0.5, speed: float = 1.0, caption_file=None,
                   caption_pos: str = "bottom", scale: bool = True) -> str:
    """Video filter chain for crop-to-vertical/square, speed and caption ('' when nothing to do)."""
    vf = []
    if aspect in ASPECTS:
        aw, ah, ow, oh = ASPECTS[aspect]
        x = min(1.0, max(0.0, crop_x))
        vf.append(f"crop=w='min(iw,ih*{aw}/{ah})':h='min(ih,iw*{ah}/{aw})':x='(iw-ow)*{x:.4f}':y='(ih-oh)/2'")
        if scale:
            vf.append(f"scale={ow}:{oh}:flags=lanczos")
    if abs(speed - 1.0) > 1e-3:
        vf.append(f"setpts=PTS/{speed:.4f}")
    if caption_file:
        font = caption_font()
        try:
            longest = max(len(x) for x in Path(caption_file).read_text("utf-8").splitlines() or [""])
        except OSError:
            longest = 20
        y = "h*0.07" if caption_pos == "top" else "h-text_h-h*0.09"
        # as big as fits: ~1/15 of the height, smaller when the longest line wouldn't fit the width
        size = f"min(h/15\\,w*0.88/({max(longest, 8)}*0.64))"
        vf.append("drawtext=" + ":".join(
            ([f"fontfile={filter_path(font)}"] if font else []) +
            [f"textfile={filter_path(caption_file)}", "expansion=none", "fontcolor=white", f"fontsize={size}",
             "text_align=C", "borderw=5", "bordercolor=black@0.85", "x=(w-text_w)/2", f"y={y}",
             "line_spacing=10"]))
    return ",".join(vf)


def atempo_chain(speed: float) -> str:
    """atempo only takes 0.5–2.0 per stage: chain stages for 0.25× / 4×."""
    parts, s = [], speed
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    parts.append(f"atempo={s:.4f}")
    return ",".join(parts)


def export_command(src: str, out: str, start: float, end: float, *, precise: bool,
                   audio_mix: list[tuple[int, float]] | None = None, keep_separate: bool = False, cfg=None,
                   hdr10: bool = False, audio_reencode: bool = False, speed: float = 1.0,
                   aspect: str | None = None, crop_x: float = 0.5, caption_file=None,
                   caption_pos: str = "bottom") -> list[str]:
    """Trim/export.

    audio_mix: None = keep the file's default audio as-is (track 0, lossless); otherwise a list of
    (track_index, gain) to mix into one track (e.g. game at 100% + mic at 60%, or mic left out).
    keep_separate: also keep every original audio track. Video is stream-copied unless precise.
    speed / aspect (9:16, 1:1, 4:5 crop) / caption_file: effects; any of them means re-encoding.
    """
    sped = abs(speed - 1.0) > 1e-3
    vf = effects_filter(aspect, crop_x, speed, caption_file, caption_pos)
    if vf:
        precise = True
    if sped:
        keep_separate = False  # every kept track would need its own tempo filter; the export gets one track
    args = [ffmpeg_path(), "-hide_banner", "-v", "error", "-y"]
    # input seeking for both modes: re-encoding decodes from the keyframe before `start` and drops frames up to it,
    # so precise cuts stay frame-exact without decoding the whole file (hours of a long session) first
    args += ["-ss", f"{start:.3f}", "-to", f"{max(end, start + 0.05):.3f}", "-i", src]
    maps = ["-map", "0:v:0"]
    fc = []
    if audio_mix is not None:
        live = [(i, g) for i, g in audio_mix if g > 0.001]
        if live:
            parts = [f"[0:a:{i}]volume={g:.3f}[t{i}]" for i, g in live]
            ins = "".join(f"[t{i}]" for i, _ in live)
            tail = f",{atempo_chain(speed)}" if sped else ""
            parts.append(f"{ins}amix=inputs={len(live)}:normalize=0{tail}[mix]" if len(live) > 1
                         else f"[t{live[0][0]}]{atempo_chain(speed) if sped else 'anull'}[mix]")
            fc = parts
            maps += ["-map", "[mix]"]
        if keep_separate:
            maps += ["-map", "0:a?"]
    else:
        maps += ["-map", "0:a?"] if keep_separate else ["-map", "0:a:0?"]
        if sped:
            args_af = ["-af", atempo_chain(speed)]
    if fc:
        args += ["-filter_complex", ";".join(fc)]
    args += maps
    if vf:
        args += ["-vf", vf]
    if sped and audio_mix is None:
        args += args_af
    if precise and cfg is not None:
        q = dict(cfg.get("quality"))
        q["rate_control"] = "cqp"
        codec = "hevc" if hdr10 and q.get("codec") == "h264" else q.get("codec", "hevc")
        vendor, enc = resolve_encoder(q.get("encoder", "auto"), codec, hdr10)
        args += video_encoder_args(vendor, enc, q, int(q.get("fps", 60)), hdr10)
        if hdr10:  # keep HDR10 clips HDR when re-encoding
            args += ["-pix_fmt", "p010le"] + hdrmod.HDR10_ENCODER_FLAGS
    elif precise:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]
    args += ["-c:a", "aac", "-b:a", "192k"] if (fc or precise or audio_reencode or sped) else ["-c:a", "copy"]
    args += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out]
    return args


def concat_parts_command(list_file: Path, out: str) -> list[str]:
    """Join exported parts (same codecs) into one clip without re-encoding."""
    return [ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-map", "0", "-c", "copy", "-movflags", "+faststart", out]
