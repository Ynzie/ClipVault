# ClipVault

A local-only, ad-free game recorder for Windows — a Medal-style clipping app where everything stays on your PC.
No account, no uploads, no ads.

## Features

**Recording**
- Replay buffer: press **F8** to save the last minute (length configurable), **Shift+F8** for a longer clip (5 min by default)
- Full-session recording that starts when a game launches and saves when it closes, with a post-game review to keep / delete / clip
- Bookmarks (**F9**), screenshots (**F7**), manual long recordings (**Alt+F7**); hold the clip key to split a session
- GPU encoding: AMD AMF, NVIDIA NVENC, Intel QSV (x264 fallback), zero-copy capture where possible
- **HDR**: captures 10-bit when Windows HDR is on and tone-maps to SDR matched to Windows' SDR brightness slider (no washed-out or blown-out clips), or keeps HDR10
- Separate audio tracks (Mixed / Game / Mic), Discord-style mic sensitivity gate with calibration, noise suppression, push-to-talk, optional virtual mic output
- In-game pop-up ("Clip saved") and sounds, Xbox / XInput controller shortcuts
- Game detection with an "always a game" / "never a game" list and an app picker

**Library**
- Grid of clips and long recordings, grouped into *Last session / Today / This week / Earlier* (foldable)
- Picks up your old recordings already in the capture folder (Medal, NVIDIA, OBS) and extra folders you add — never moves or auto-deletes them
- Hover a card to scrub through it, multi-select with the circle on each card, albums, favorites, trash
- Storage limit with auto-cleanup of ClipVault's own recordings

**Editor**
- Smooth GPU playback (libmpv), zoomable timeline with audio waveform, bookmarks
- Instant lossless trim, or frame-exact re-encode
- Combine several moments into one clip, per-part speed (slow-mo / fast-forward)
- Vertical 9:16, square and 4:5 crops with live preview, text captions
- Pick and rebalance audio tracks for the export

## Build

Windows 10/11, Python 3.11+ (from python.org, with **Add to PATH** ticked).

1. Clone or download this repo.
2. Double-click **`build.bat`**. It creates a virtual environment, installs the Python packages,
   downloads FFmpeg (BtbN GPL build) and libmpv, builds the app with PyInstaller and packs
   `dist\ClipVault-portable.zip`.
3. Run `dist\ClipVault\ClipVault.exe` (also copied to `Downloads\ClipVault`). It starts with Windows,
   minimized to the tray.

`run_from_source.bat` runs it straight from the source (needs `ffmpeg\ffmpeg.exe` or FFmpeg on PATH).

## How it works

| | |
|---|---|
| Capture | FFmpeg `ddagrab` (Desktop Duplication) → AMF / NVENC / QSV / x264 |
| Replay buffer | 2-second MPEG-TS segments on disk; clips are stream-copied (instant, no quality loss) |
| HDR | GPU (libplacebo) or CPU (zscale) tone-mapping to the Windows SDR white level, or HDR10 passthrough |
| Audio | WASAPI loopback + mic captured in Python and piped to FFmpeg |
| Editor playback | libmpv embedded in Qt, falls back to Qt Multimedia |
| UI | PySide6 (Qt 6) |

## Default hotkeys

| Key | Action |
|---|---|
| F8 | Save clip (during a session: bookmark, configurable). Hold ~1 s: split the session |
| Shift+F8 | Save a longer clip |
| Alt+F7 | Start / stop a long recording |
| F9 | Bookmark |
| F7 | Screenshot |
| View + Menu (controller) | Save clip |

## Files

- Settings: `%APPDATA%\ClipVault\settings.json` (log: `clipvault.log` next to it)
- Media: `<capture folder>\{Clips,Sessions,Screenshots}\<Game>\` plus `library.json` (default `Videos\ClipVault`)
- Replay buffer: `<capture folder>\.buffer\` (cleaned up automatically)

Settings → Advanced → Diagnostics shows the exact FFmpeg command and its log if capture misbehaves.

## License

ClipVault's own code is MIT-licensed (see `LICENSE`). The build downloads and bundles third-party
binaries under their own licenses: FFmpeg and libmpv (GPL builds), Qt / PySide6 (LGPL). If you share
a built copy, those licenses apply to those parts.

Not affiliated with Medal, NVIDIA, AMD or OBS.
