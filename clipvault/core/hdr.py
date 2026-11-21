"""HDR detection (Windows DisplayConfig + DXGI) and HDR->SDR filter-graph builders.

The "auto brightness fix":
  * Windows tells us whether Advanced Color (HDR) is on for each display, what the
    "SDR content brightness" slider is set to (the SDR white level, in nits), and how bright
    the display can go.
  * When HDR is on we capture the desktop as 16-bit float (linear scRGB - the only grab format
    that actually carries HDR; 8/10-bit grabs are clamped at 80 nits) and turn it into SDR on the
    GPU: SDR white -> 100% white (so the desktop and SDR games look exactly like they do on screen),
    brighter HDR highlights roll off smoothly up to the display's peak.
"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WIN = sys.platform == "win32"


@dataclass
class DisplayHDR:
    gdi_name: str              # e.g. \\.\DISPLAY1
    hdr_supported: bool = False
    hdr_enabled: bool = False
    sdr_white_nits: float = 80.0
    bits_per_channel: int = 8
    max_nits: float = 0.0      # display peak reported by DXGI (0 = unknown)


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
    try:
        peaks = _dxgi_luminance_win()
    except Exception:
        peaks = {}
    for d in out:
        d.max_nits = peaks.get(d.gdi_name.lower(), 0.0)
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


# --------------------------------------------------------------------------- display peak (DXGI)

def _dxgi_luminance_win() -> dict[str, float]:
    """GDI device name -> the display's max luminance in nits (IDXGIOutput6::GetDesc1).

    Windows reports what the panel (or the HDR Calibration app) says it can do; HDR games use the
    same number for their own peak, so it's the brightest value we expect in a captured frame.
    """
    import uuid
    from ctypes import wintypes, byref, c_void_p, c_long, c_uint, c_float, POINTER, WINFUNCTYPE

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ubyte * 16)]

    def iid(s):
        return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)

    class DESC1(ctypes.Structure):
        _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", wintypes.RECT),
                    ("AttachedToDesktop", wintypes.BOOL), ("Rotation", c_uint), ("Monitor", c_void_p),
                    ("BitsPerColor", c_uint), ("ColorSpace", c_uint), ("RedPrimary", c_float * 2),
                    ("GreenPrimary", c_float * 2), ("BluePrimary", c_float * 2), ("WhitePoint", c_float * 2),
                    ("MinLuminance", c_float), ("MaxLuminance", c_float), ("MaxFullFrameLuminance", c_float)]

    def method(obj, index, *argtypes):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        return WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])

    def release(obj):
        if obj:
            method(obj, 2)(obj)

    dxgi = ctypes.WinDLL("dxgi")
    factory = c_void_p()
    if dxgi.CreateDXGIFactory1(byref(iid("770aae78-f26f-4dba-a829-253c83d1b387")), byref(factory)) < 0:
        return {}
    out: dict[str, float] = {}
    try:
        for ai in range(16):
            adapter = c_void_p()
            if method(factory, 12, c_uint, POINTER(c_void_p))(factory, ai, byref(adapter)) < 0:  # EnumAdapters1
                break
            try:
                for oi in range(16):
                    output = c_void_p()
                    if method(adapter, 7, c_uint, POINTER(c_void_p))(adapter, oi, byref(output)) < 0:  # EnumOutputs
                        break
                    try:
                        o6 = c_void_p()
                        if method(output, 0, POINTER(GUID), POINTER(c_void_p))(
                                output, byref(iid("068346e8-aaec-4b84-add7-137f513f77a1")), byref(o6)) < 0:
                            continue
                        try:
                            d = DESC1()
                            if method(o6, 27, POINTER(DESC1))(o6, byref(d)) >= 0 and d.MaxLuminance > 0:  # GetDesc1
                                out[d.DeviceName.lower()] = float(d.MaxLuminance)
                        finally:
                            release(o6)
                    finally:
                        release(output)
            finally:
                release(adapter)
    finally:
        release(factory)
    return out


def display_peak_nits(gdi_name: str | None) -> float:
    """Max luminance Windows reports for this display (0 if unknown)."""
    if not IS_WIN or not gdi_name:
        return 0.0
    try:
        return _dxgi_luminance_win().get(gdi_name.lower(), 0.0)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- filters
#
# How Windows hands us an HDR desktop (Desktop Duplication):
#   * 16-bit float (output_fmt=16bit) is the real thing: linear scRGB, BT.709 primaries, 1.0 = 80 nits.
#     SDR content (the desktop, browsers, SDR games) sits at 0..(SDR white / 80); HDR games go above.
#   * 8-bit and 10-bit grabs are NOT HDR: Windows just clamps those same linear values to 0..1 and
#     stores them as-is. Anything brighter than 80 nits is cut off - that's the blown-out look.
# So the fix grabs float, divides by the SDR white level (UI white -> 100% white, exactly as the
# screen shows it), rolls off anything brighter, and sRGB-encodes. All on the GPU (libplacebo):
# FFmpeg's CPU filters clamp float input to 0..1, so there is no CPU route.

SCRGB_NITS = 80.0
CAPTURE_PIXFMT = "rgbaf16le"   # what hwdownload gives us from ddagrab output_fmt=16bit
DEFAULT_PEAK_NITS = 1000.0
KNEE = 0.8                     # fraction of SDR white that is always reproduced 1:1


def white_nits(hdr_cfg: dict, state: DisplayHDR | None) -> float:
    if hdr_cfg.get("auto_white", True) and state is not None and state.sdr_white_nits:
        return max(80.0, float(state.sdr_white_nits))
    return float(hdr_cfg.get("white_nits", 203))


def peak_nits(hdr_cfg: dict, state: DisplayHDR | None) -> float:
    """Brightest value to expect: manual setting, else what the display reports, else 1000 nits."""
    manual = float(hdr_cfg.get("peak_nits", 0) or 0)
    if manual > 0:
        return manual
    if state is not None and getattr(state, "max_nits", 0):
        return float(state.max_nits)
    return DEFAULT_PEAK_NITS


def mobius_params(peak: float, knee: float = KNEE) -> tuple[float, float]:
    """Constants for the Mobius roll-off (identity below the knee, slope-matched, hits 1.0 at peak)."""
    j = knee
    a = -j * j * (peak - 1) / (j * j - 2 * j + peak)
    b = (j * j - 2 * j * peak + peak) / max(peak - 1, 1e-6)
    return a, b


def rolloff(x: float, peak: float, knee: float = KNEE) -> float:
    """Python twin of the shader curve (x and the result are relative to SDR white)."""
    if peak <= 1.0 + 1e-4:
        return min(max(x, 0.0), 1.0)
    if x <= knee:
        return max(x, 0.0)
    a, b = mobius_params(peak, knee)
    j = knee
    return min(1.0, (b * b + 2 * b * j + j * j) / (b - a) * (x + a) / (x + b))


def _f(v: float) -> str:
    return f"{v:.8f}"


def sdr_shader(hdr_cfg: dict, state: DisplayHDR | None) -> str:
    """mpv/libplacebo user shader: linear scRGB -> finished sRGB-encoded SDR values (trims baked in).

    libplacebo is told input and output are both 'linear BT.709', so it doesn't touch the values
    (its own SDR handling would crush the deepest shadows); it only scales, dithers and converts to YUV.
    """
    white = white_nits(hdr_cfg, state)
    peak = max(peak_nits(hdr_cfg, state), white) / white
    a, b = mobius_params(peak)
    j = KNEE
    c = (b * b + 2 * b * j + j * j) / (b - a) if peak > 1.0 + 1e-4 else 0.0
    br = float(hdr_cfg.get("brightness", 0.0))
    co = float(hdr_cfg.get("contrast", 1.0))
    sa = float(hdr_cfg.get("saturation", 1.0))
    ga = float(hdr_cfg.get("gamma", 1.0))
    trims = abs(br) > 1e-3 or abs(co - 1) > 1e-3 or abs(ga - 1) > 1e-3
    lines = [
        "//!HOOK MAIN",
        "//!BIND HOOKED",
        "//!DESC ClipVault HDR to SDR",
        "",
        f"#define INV_WHITE {_f(SCRGB_NITS / white)}",
        f"#define ROLL {1 if peak > 1.0 + 1e-4 else 0}",
        f"#define KNEE {_f(j)}",
        f"#define MA {_f(a)}",
        f"#define MB {_f(b)}",
        f"#define MC {_f(c)}",
        f"#define SAT {_f(sa)}",
        f"#define TRIMS {1 if trims else 0}",
        f"#define BRI {_f(br)}",
        f"#define CON {_f(co)}",
        f"#define INV_GAMMA {_f(1.0 / max(ga, 0.05))}",
        "",
        "vec3 srgb_enc(vec3 x) { return mix(12.92 * x, 1.055 * pow(x, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, x)); }",
        "",
        "vec4 hook() {",
        "    vec4 c = HOOKED_texOff(0);",
        "    vec3 x = max(c.rgb * INV_WHITE, 0.0);  // 1.0 = Windows SDR white",
        "#if ROLL",
        "    float m = max(max(x.r, x.g), x.b);",
        "    if (m > KNEE) x *= min(MC * (m + MA) / (m + MB), 1.0) / m;  // hue-preserving highlight roll-off",
        "#endif",
        "    x = min(x, 1.0);",
        "    float y = dot(x, vec3(0.2126, 0.7152, 0.0722));",
        "    x = clamp(mix(vec3(y), x, SAT), 0.0, 1.0);",
        "    vec3 e = srgb_enc(x);",
        "#if TRIMS",
        "    e = pow(clamp((e - 0.5) * CON + 0.5 + BRI, 0.0, 1.0), vec3(INV_GAMMA));",
        "#endif",
        "    return vec4(e, c.a);",
        "}",
    ]
    return "\n".join(lines) + "\n"


# BT.709 -> BT.2020 primaries (linear light), ITU-R BT.2087
_M709_2020 = ((0.6274040, 0.3292820, 0.0433136),
              (0.0690970, 0.9195400, 0.0113612),
              (0.0163916, 0.0880132, 0.8955950))


def hdr10_shader() -> str:
    """scRGB -> PQ-encoded BT.2020 (absolute nits). libplacebo just passes these values through."""
    rows = ",\n                ".join("vec3(" + ", ".join(_f(_M709_2020[r][c]) for r in range(3)) + ")"
                                       for c in range(3))  # GLSL mat3 is column-major
    return f"""//!HOOK MAIN
//!BIND HOOKED
//!DESC ClipVault HDR10

const mat3 TO2020 = mat3({rows});

vec3 pq_enc(vec3 nits) {{
    vec3 y = pow(clamp(nits / 10000.0, 0.0, 1.0), vec3(0.1593017578125));
    return pow((0.8359375 + 18.8515625 * y) / (1.0 + 18.6875 * y), vec3(78.84375));
}}

vec4 hook() {{
    vec4 c = HOOKED_texOff(0);
    vec3 lin = max(TO2020 * c.rgb, 0.0) * {_f(SCRGB_NITS)};
    return vec4(pq_enc(lin), c.a);
}}
"""


def _shader_file(text: str) -> Path:
    import hashlib
    from .config import app_data_dir
    d = app_data_dir() / "shaders"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"hdr_{hashlib.sha1(text.encode()).hexdigest()[:12]}.glsl"
    if not f.exists() or f.read_text(encoding="utf-8") != text:
        f.write_text(text, encoding="utf-8")
    return f


_TAG_IN = "setparams=color_primaries=bt709:color_trc=linear:colorspace=gbr:range=pc"
_PASSIVE = ":tonemapping=clip:peak_detect=0:gamut_mode=clip:dithering=ordered"


def tonemap_chain(hdr_cfg: dict, state: DisplayHDR | None, out_w: int | None, out_h: int | None,
                  out_fmt: str = "nv12") -> str:
    """Filter chain: linear scRGB software frames (rgbaf16le) -> SDR BT.709 frames (GPU, libplacebo).

    out_fmt: nv12 / yuv420p for encoding (limited range, BT.709 matrix), or gbrp for a PNG.
    """
    from .ffmpeg import filter_path
    shader = filter_path(_shader_file(sdr_shader(hdr_cfg, state)))
    size = f"w={out_w}:h={out_h}:" if out_w and out_h else ""
    if out_fmt == "gbrp":
        repr_ = "format=gbrp:colorspace=gbr:range=pc"
    else:
        repr_ = f"format={'yuv420p' if out_fmt == 'yuv420p' else 'nv12'}:colorspace=bt709:range=tv"
    return (f"{_TAG_IN},libplacebo={size}{repr_}:color_primaries=bt709:color_trc=linear"
            f"{_PASSIVE}:custom_shader_path={shader},setparams=color_trc=iec61966-2-1")


def hdr10_chain(out_w: int | None, out_h: int | None) -> str:
    """Keep HDR: linear scRGB (rgbaf16le) -> PQ BT.2020 p010 for a 10-bit HEVC/AV1 encode.

    The shader writes finished PQ values; libplacebo is told input and output are both 'linear BT.709'
    so it leaves them alone and only scales + converts to 10-bit YUV with the BT.2020 matrix.
    """
    from .ffmpeg import filter_path
    shader = filter_path(_shader_file(hdr10_shader()))
    size = f"w={out_w}:h={out_h}:" if out_w and out_h else ""
    return (f"{_TAG_IN},libplacebo={size}format=p010le:colorspace=bt2020nc:range=tv:color_primaries=bt709"
            f":color_trc=linear:tonemapping=clip:peak_detect=0:gamut_mode=clip:dithering=none"
            f":custom_shader_path={shader},"
            "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv")


HDR10_ENCODER_FLAGS = ["-color_primaries", "bt2020", "-color_trc", "smpte2084",
                       "-colorspace", "bt2020nc", "-color_range", "tv"]
