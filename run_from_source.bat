@echo off
REM Run ClipVault without building an exe (uses the same .venv as build.bat; needs ffmpeg\ffmpeg.exe or ffmpeg on PATH)
cd /d "%~dp0"
if not exist .venv (py -3 -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt)
.venv\Scripts\python -m clipvault %*
