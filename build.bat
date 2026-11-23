@echo off
REM ============================================================================
REM  ClipVault — one-click Windows build
REM  Makes dist\ClipVault\ClipVault.exe (with ffmpeg bundled) and copies it to
REM  %USERPROFILE%\Downloads\ClipVault
REM  Needs: Python 3.11+ (python.org installer, "Add to PATH" ticked) + internet.
REM ============================================================================
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (set PY=python)

REM a running ClipVault locks its files - the build and the copy would fail half-way
:waitquit
tasklist /fi "imagename eq ClipVault.exe" 2>nul | find /i "ClipVault.exe" >nul
if not errorlevel 1 (
    echo  ClipVault is running - right-click its tray icon and pick Quit, then press any key.
    pause >nul
    goto :waitquit
)

REM a recorder process left behind by a ClipVault that crashed or was force-closed keeps ffmpeg.exe
REM locked ("Access is denied" at the end of the build). Only ones running from a built ClipVault folder.
powershell -NoProfile -Command "Get-Process ffmpeg,ffprobe -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*\_internal\ffmpeg\*' } | Stop-Process -Force" >nul 2>nul

echo [1/6] Creating virtual environment...
if not exist .venv (
    %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat

echo [2/6] Installing Python packages...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller || goto :fail

echo [3/6] Getting ffmpeg (GPL build with AMF, Vulkan/libplacebo, zimg)...
if not exist ffmpeg\ffmpeg.exe (
    if not exist ffmpeg mkdir ffmpeg
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ProgressPreference='SilentlyContinue';" ^
      "$u='https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip';" ^
      "Invoke-WebRequest $u -OutFile ffmpeg\ff.zip;" ^
      "Expand-Archive ffmpeg\ff.zip -DestinationPath ffmpeg\x -Force;" ^
      "$b=Get-ChildItem ffmpeg\x -Recurse -Filter ffmpeg.exe | Select-Object -First 1;" ^
      "Copy-Item $b.FullName ffmpeg\ffmpeg.exe; Copy-Item (Join-Path $b.DirectoryName 'ffprobe.exe') ffmpeg\ffprobe.exe;" ^
      "Remove-Item ffmpeg\x -Recurse -Force; Remove-Item ffmpeg\ff.zip" || goto :fail
)

echo [3b/6] Getting libmpv (smooth GPU video playback in the editor)...
if not exist mpv\libmpv-2.dll (
    if not exist mpv mkdir mpv
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ProgressPreference='SilentlyContinue';" ^
      "$r=Invoke-RestMethod 'https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest';" ^
      "$a=$r.assets | Where-Object { $_.name -match '^mpv-dev-x86_64-[0-9]' } | Select-Object -First 1;" ^
      "Invoke-WebRequest $a.browser_download_url -OutFile mpv\mpv-dev.7z;" ^
      "Invoke-WebRequest 'https://www.7-zip.org/a/7zr.exe' -OutFile mpv\7zr.exe;" ^
      "& mpv\7zr.exe e mpv\mpv-dev.7z libmpv-2.dll -ompv -y | Out-Null;" ^
      "Remove-Item mpv\mpv-dev.7z, mpv\7zr.exe" || echo   libmpv download failed - the editor will use basic playback
)

echo [4/6] Building ClipVault.exe...
set MPVBIN=
if exist mpv\libmpv-2.dll set MPVBIN=--add-binary "mpv\libmpv-2.dll;."
pyinstaller --noconfirm --clean --windowed --name ClipVault ^
  --icon clipvault\assets\clipvault.ico ^
  --add-data "clipvault\assets;clipvault\assets" ^
  --add-binary "ffmpeg\ffmpeg.exe;ffmpeg" ^
  --add-binary "ffmpeg\ffprobe.exe;ffmpeg" ^
  %MPVBIN% ^
  --hidden-import pyaudiowpatch ^
  --hidden-import mpv ^
  run_clipvault.py || goto :fail

echo [5/6] Copying to Downloads\ClipVault...
robocopy dist\ClipVault "%USERPROFILE%\Downloads\ClipVault" /MIR /NFL /NDL /NJH /NJS >nul

echo [6/6] Packing dist\ClipVault-portable.zip for other PCs...
if exist dist\ClipVault-portable.zip del dist\ClipVault-portable.zip
tar -a -c -f dist\ClipVault-portable.zip -C dist ClipVault 2>nul || powershell -NoProfile -Command "Compress-Archive -Path 'dist\ClipVault' -DestinationPath 'dist\ClipVault-portable.zip' -Force"

echo.
echo  Done!  %USERPROFILE%\Downloads\ClipVault\ClipVault.exe
echo  For another PC: unzip dist\ClipVault-portable.zip over the old ClipVault folder.
echo  First launch adds ClipVault to Windows startup (turn off in Settings ^> Appearance).
echo.
pause
exit /b 0

:fail
echo.
echo  Build failed - scroll up for the error.
pause
exit /b 1
