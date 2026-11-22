# ClipVault

A local-only, ad-free game recorder (Medal replacement). Everything stays on your PC.

## Build the exe

1. Install Python 3.11+ from python.org (tick **Add to PATH**).
2. Quit ClipVault from its tray icon if it's running, then double-click **`build.bat`**. It sets up
   a venv, installs packages, downloads FFmpeg (BtbN GPL build: AMD AMF, Vulkan/libplacebo)
   and libmpv (editor playback) if they're missing, builds the app, copies it to
   `Downloads\ClipVault\ClipVault.exe` and packs `dist\ClipVault-portable.zip` for other PCs.
3. Run `ClipVault.exe`. First launch registers it to start with Windows (minimized to tray).

`run_from_source.bat` runs it without building (put `ffmpeg.exe` in `.\ffmpeg\` or on PATH).

## How it works

| | |
|---|---|
| Capture | FFmpeg `ddagrab` (Desktop Duplication) → AMD AMF / NVENC / QSV / x264, GPU zero-copy when possible |
| Replay buffer | 2-second MPEG-TS segments on disk; clips are stream-copied (instant, no quality loss) |
| Full session | Long recording starts when a game launches, saves when the game process exits |
| Post-game review | Pops up after the game closes: keep / delete / favorite / open & make clips |
| HDR | Captures the real HDR desktop (16-bit float scRGB) when Windows HDR is on and converts it to SDR on the GPU (libplacebo/Vulkan): Windows' SDR brightness slider maps to 100% white, brighter highlights roll off up to your display's peak. Or keeps HDR10 |
| Audio | WASAPI loopback + mic captured in Python, piped to FFmpeg; Mixed + Game + Mic tracks |
| Mic | Discord-style input sensitivity gate with Calibrate (you vs. room), noise suppression (afftdn), push-to-talk, mic test, optional virtual-mic output (VB-Cable) for Discord/games |
| Editor | libmpv GPU playback (falls back to Qt Multimedia), zoomable timeline with waveform (loaded per zoomed region for recordings over 10 min), bookmarks, trim, per-track mute/volume, fast or frame-exact export, frame screenshot |
| Linked recordings | Other videos in the capture folder (old Medal / NVIDIA / OBS captures) plus any extra folders show up in the library, grouped by game and dated from their file names. They're never moved or auto-deleted; "Remove from ClipVault" hides one without touching the file |

## Default hotkeys

| Key | Action |
|---|---|
| F8 | Save clip — during a session: bookmark (configurable). Hold ~1 s: split session |
| Alt+F7 | Start / stop long recording |
| F9 | Bookmark |
| F7 | Screenshot |

## Files

* Settings: `%APPDATA%\ClipVault\settings.json`
* Media: `<capture folder>\{Clips,Sessions,Screenshots}\<Game>\` + `library.json` (default `Videos\ClipVault`)
* Replay buffer: `Videos\ClipVault\.buffer\` (cleaned automatically)

Settings → Advanced → Diagnostics shows the exact FFmpeg command and log if capture misbehaves.

## Updates

The built app updates itself from this repo: **Settings → Advanced → Updates → Check for updates**, then
**Update and restart**. It downloads the app code at the newest commit into `%APPDATA%\ClipVault\update`
and runs that instead of the code baked into the exe, so no rebuild is needed. If an update fails to
start, ClipVault goes back to the built-in version by itself. A change that needs new bundled files
(a new Python package, a newer FFmpeg) still needs `build.bat`; the app tells you when that's the case.
