"""HDR detection (Windows DisplayConfig API) and HDR->SDR filter-graph builders.

The "auto brightness fix" works like this:
  * Windows tells us whether Advanced Color (HDR) is on for each display, and what
    the "SDR content brightness" slider is set to (the SDR white level, in nits).
  * When HDR is on we capture the desktop in 10-bit (PQ / BT.2020) instead of
    letting Windows squash it to 8-bit (that's what makes HDR clips look dull or
    blown out), then tone-map it to SDR ourselves:
      - GPU engine (libplacebo/Vulkan): dynamic per-scene peak detection + BT.2390
        curve, with brightness/contrast/saturation/gamma trims.
      - CPU engine (zscale): exact reference-white match — desktop white at the
        Windows SDR white level becomes 100% white in the clip.
"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

IS_WIN = sys.platform == "win32"


@dataclass
class DisplayHDR:
    gdi_name: str              # e.g. \\.\DISPLAY1
    hdr_supported: bool = False
    hdr_enabled: bool = False
    sdr_white_nits: float = 80.0
    bits_per_channel: int = 8


def query_displays() -> list[DisplayHDR]:
    """Return HDR state for every active display. Empty list off Windows / on error."""
    if not IS_WIN:
        return []
    try:
        return _query_displays_win()
    except Exception:
        return []


def _query_displays_win() -> list[DisplayHDR]:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class PATH_SOURCE(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32),
                    ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]

    class RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]

    class PATH_TARGET(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                    ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                    ("scaling", ctypes.c_uint32), ("refreshRate", RATIONAL),
                    ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wintypes.BOOL),
                    ("statusFlags", ctypes.c_uint32)]

    class PATH_INFO(ctypes.Structure):
        _fields_ = [("sourceInfo", PATH_SOURCE), ("targetInfo", PATH_TARGET), ("flags", ctypes.c_uint32)]

    class MODE_INFO(ctypes.Structure):
        _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32),
                    ("adapterId", LUID), ("_union", ctypes.c_byte * 48)]

    class HEADER(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32),
                    ("adapterId", LUID), ("id", ctypes.c_uint32)]

    class SOURCE_NAME(ctypes.Structure):
        _fields_ = [("header", HEADER), ("viewGdiDeviceName", ctypes.c_wchar * 32)]

    class ADV_COLOR(ctypes.Structure):
        _fields_ = [("header", HEADER), ("value", ctypes.c_uint32),
                    ("colorEncoding", ctypes.c_uint32), ("bitsPerColorChannel", ctypes.c_uint32)]

    class SDR_WHITE(ctypes.Structure):
        _fields_ = [("header", HEADER), ("SDRWhiteLevel", ctypes.c_uint32)]

    QDC_ONLY_ACTIVE_PATHS = 2
    n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)):
        return []
    paths = (PATH_INFO * n_paths.value)()
    modes = (MODE_INFO * n_modes.value)()
    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                 ctypes.byref(n_modes), modes, None):
        return []

    out = []
    for i in range(n_paths.value):
        p = paths[i]
        src = SOURCE_NAME()
        src.header.type, src.header.size = 1, ctypes.sizeof(SOURCE_NAME)
        src.header.adapterId, src.header.id = p.sourceInfo.adapterId, p.sourceInfo.id
        user32.DisplayConfigGetDeviceInfo(ctypes.byref(src))

        adv = ADV_COLOR()
        adv.header.type, adv.header.size = 9, ctypes.sizeof(ADV_COLOR)
        adv.header.adapterId, adv.header.id = p.targetInfo.adapterId, p.targetInfo.id
        ok_adv = user32.DisplayConfigGetDeviceInfo(ctypes.byref(adv)) == 0

        white = SDR_WHITE()
        white.header.type, white.header.size = 11, ctypes.sizeof(SDR_WHITE)
        white.header.adapterId, white.header.id = p.targetInfo.adapterId, p.targetInfo.id
        ok_white = user32.DisplayConfigGetDeviceInfo(ctypes.byref(white)) == 0

        d = DisplayHDR(gdi_name=src.viewGdiDeviceName)
        if ok_adv:
            d.hdr_supported = bool(adv.value & 1)
            d.hdr_enabled = bool(adv.value & 2) and not bool(adv.value & 8)
            d.bits_per_channel = adv.bitsPerColorChannel
        if ok_white and white.SDRWhiteLevel:
            d.sdr_white_nits = white.SDRWhiteLevel / 1000.0 * 80.0
        out.append(d)
    return out


def display_state(gdi_name: str | None) -> DisplayHDR | None:
    ds = query_displays()
    if not ds:
        return None
    if gdi_name:
        for d in ds:
            if d.gdi_name.lower() == gdi_name.lower():
                return d
    return ds[0]


# --------------------------------------------------------------------------- filters

def white_nits(hdr_cfg: dict, state: DisplayHDR | None) -> float:
    if hdr_cfg.get("auto_white", True) and state is not None and state.sdr_white_nits:
        return max(80.0, float(state.sdr_white_nits))
    return float(hdr_cfg.get("white_nits", 203))


def tonemap_chain(hdr_cfg: dict, state: DisplayHDR | None, out_w: int | None, out_h: int | None,
                  out_fmt: str = "nv12") -> str:
    """Filter chain that turns downloaded x2bgr10 PQ frames into SDR BT.709 frames.

    Input to this chain: software frames in x2bgr10le carrying HDR10 (PQ, BT.2020) values.
    """
    tag = "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=gbr:range=pc"
    engine = hdr_cfg.get("engine", "gpu")
    nits = white_nits(hdr_cfg, state)
    if engine == "gpu":
        curve = hdr_cfg.get("curve", "bt.2390")
        # libplacebo maps HDR reference white (203 nits) to SDR white. When Windows' SDR
        # white differs we pre-compensate with a gamma trim so desktop/UI white lands near 100%.
        white_gamma = (203.0 / nits) ** 0.45 if hdr_cfg.get("auto_white", True) else 1.0
        gamma = float(hdr_cfg.get("gamma", 1.0)) * white_gamma
        size = f"w={out_w}:h={out_h}:" if out_w and out_h else ""
        lp = (f"libplacebo={size}format={'yuv420p' if out_fmt == 'yuv420p' else 'nv12'}"
              f":colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
              f":tonemapping={curve}:peak_detect={1 if hdr_cfg.get('peak_detect', True) else 0}"
              f":contrast_recovery={float(hdr_cfg.get('contrast_recovery', 0.3)):.2f}"
              f":brightness={float(hdr_cfg.get('brightness', 0.0)):.3f}"
              f":contrast={float(hdr_cfg.get('contrast', 1.0)):.3f}"
              f":saturation={float(hdr_cfg.get('saturation', 1.0)):.3f}"
              f":gamma={gamma:.3f}:dithering=ordered")
        return f"{tag},{lp}"
    # CPU / zscale: exact reference-white match. Everything up to ~85% of Windows' SDR white is
    # reproduced 1:1; brighter highlights roll off smoothly up to a 1000-nit peak.
    curve = hdr_cfg.get("curve", "bt.2390")
    tm = {"bt.2390": "mobius", "st2094-40": "mobius", "spline": "mobius"}.get(curve, curve)
    if tm not in ("hable", "mobius", "reinhard", "clip"):
        tm = "mobius"
    npl, peak, param = _cpu_curve_fit(tm, nits)
    scale = f"zscale=w={out_w}:h={out_h}:f=spline36," if out_w and out_h else ""
    eq = _eq_filter(hdr_cfg)
    tparam = f":param={param}" if tm == "mobius" else ""
    return (f"{tag},zscale=tin=smpte2084:min=gbr:pin=bt2020:rin=full:t=linear:npl={npl:.0f},"
            f"format=gbrpf32le,zscale=p=bt709,tonemap={tm}:desat=0:peak={peak:.3f}{tparam},"
            f"{scale}zscale=t=bt709:m=bt709:r=tv,format={out_fmt}{eq}")


def _curve(tm: str, x: float, peak: float, j: float = 0.6) -> float:
    if tm == "clip":
        return min(1.0, x)
    if tm == "reinhard":
        off = (1 - 0.5) / 0.5  # ffmpeg default param 0.5
        return x / (x + off) * (peak + off) / peak
    if tm == "hable":
        def h(v):
            A, B, C, D, E, F = .15, .5, .1, .2, .02, .3
            return (v * (v * A + C * B) + D * E) / (v * (v * A + B) + D * F) - E / F
        return h(x) / h(peak)
    # mobius (ffmpeg tonemap)
    if x <= j:
        return x
    a = -j * j * (peak - 1) / (j * j - 2 * j + peak)
    b = (j * j - 2 * j * peak + peak) / max(peak - 1, 1e-6)
    return (b * b + 2 * b * j + j * j) / (b - a) * (x + a) / (x + b)


def _cpu_curve_fit(tm: str, white: float, peak_nits: float = 1000.0):
    """Reference white = Windows SDR white (midtones stay exact); only highlights get compressed.
    Returns (npl, peak, mobius knee)."""
    return white, max(1.5, peak_nits / white), 0.85


def _eq_filter(hdr_cfg: dict) -> str:
    b = float(hdr_cfg.get("brightness", 0.0))
    c = float(hdr_cfg.get("contrast", 1.0))
    s = float(hdr_cfg.get("saturation", 1.0))
    g = float(hdr_cfg.get("gamma", 1.0))
    if abs(b) < 1e-3 and abs(c - 1) < 1e-3 and abs(s - 1) < 1e-3 and abs(g - 1) < 1e-3:
        return ""
    return f",eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}:gamma={g:.3f}"


def hdr10_chain(out_w: int | None, out_h: int | None) -> str:
    """Keep HDR: convert x2bgr10 PQ RGB to p010 BT.2020 YUV for a 10-bit HEVC/AV1 encode."""
    scale = f"scale={out_w}:{out_h}:flags=bicubic:" if out_w and out_h else "scale="
    return ("setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=gbr:range=pc,"
            f"{scale}out_color_matrix=bt2020:out_range=tv,format=p010le")


HDR10_ENCODER_FLAGS = ["-color_primaries", "bt2020", "-color_trc", "smpte2084",
                       "-colorspace", "bt2020nc", "-color_range", "tv"]
