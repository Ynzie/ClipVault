"""Foreground game detection (no extra dependencies — plain Win32 via ctypes)."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

IS_WIN = sys.platform == "win32"
LAST_HWND = 0

PRETTY = {
    "iw4x": "IW4x (MW2)", "iw4m": "IW4x (MW2)", "vrchat": "VRChat", "rocksmith2014": "Rocksmith 2014",
    "lethal company": "Lethal Company", "cs2": "Counter-Strike 2", "valorant-win64-shipping": "VALORANT",
    "r5apex": "Apex Legends", "fortniteclient-win64-shipping": "Fortnite", "javaw": "Minecraft",
    "cod": "Call of Duty", "galacticracer": "Galactic Racer",
}


# Never games, whatever the settings say (unless you add one to your games list yourself). These open
# borderless fullscreen windows that look exactly like a game to the fullscreen check — e.g. Win+Shift+S /
# Print Screen puts Snipping Tool's capture overlay over the whole screen, which started a "game session".
NEVER_GAMES = {x.lower() for x in (
    "SnippingTool.exe", "ScreenClippingHost.exe", "ScreenSketch.exe", "TextInputHost.exe", "explorer.exe",
    "ShellHost.exe", "ShellExperienceHost.exe", "StartMenuExperienceHost.exe", "SearchHost.exe", "SearchApp.exe",
    "LockApp.exe", "LogonUI.exe", "Taskmgr.exe", "ApplicationFrameHost.exe", "dwm.exe", "ClipVault.exe",
    "ShareX.exe", "Lightshot.exe", "Greenshot.exe", "PowerToys.exe", "PowerToys.ScreenRuler.exe",
    "PowerToys.ColorPickerUI.exe", "PowerToys.MeasureToolUI.exe", "PowerToys.PowerOCR.exe", "PowerToys.Peek.UI.exe",
    "Magnify.exe", "osk.exe", "XboxGameBar.exe", "GameBar.exe", "GameBarFTServer.exe", "mstsc.exe", "msrdc.exe",
    "TeamViewer.exe", "TeamViewer_Desktop.exe", "AnyDesk.exe", "RustDesk.exe", "parsecd.exe",
    # fullscreen video in a browser / chat / player isn't a game either
    "brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "vivaldi.exe", "Discord.exe",
    "Spotify.exe", "Microsoft.Media.Player.exe", "vlc.exe", "mpc-hc64.exe", "PotPlayerMini64.exe", "mpv.exe",
    "Video.UI.exe", "Photos.exe", "PhotosApp.exe")}

# a fullscreen app nobody listed must stay in front this long before it counts (a snip or a quick
# fullscreen video shouldn't start a recording); games on your list start after 2 s
FULLSCREEN_GRACE = 6.0


# Java programs all run as javaw.exe, so the exe name says nothing: Minecraft, TuxGuitar and a
# dozen other apps look identical. Tell them apart by the window title.
JAVA_HOSTS = {"javaw.exe", "java.exe"}
MINECRAFT_TITLES = ("minecraft", "lunar client", "badlion", "feather client", "labymod", "forge", "fabric",
                    "optifine", "modrinth", "curseforge", "prism launcher", "multimc", "tlauncher")


def java_app_name(title: str) -> str:
    """Minecraft for anything that looks like it; otherwise the app's own name from its window title."""
    import re
    low = (title or "").lower()
    if any(k in low for k in MINECRAFT_TITLES):
        return "Minecraft"
    name = re.split(r"\s+[-–—|:]\s+|\s+\(", (title or "").strip())[0]
    name = re.sub(r"\s+v?\d[\d.]*\w*$", "", name).strip()          # drop a trailing version number
    name = "".join(c for c in name if c not in '<>:"/\\|?*').strip(" .")
    return name[:40] or "Java app"


def pretty_name(exe: str, title: str = "") -> str:
    if exe.lower() in JAVA_HOSTS:
        return java_app_name(title)
    stem = Path(exe).stem
    return PRETTY.get(stem.lower(), stem.replace("_", " ").replace("-Win64-Shipping", ""))


def process_alive(pid: int) -> bool:
    if not IS_WIN or not pid:
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32")
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    code = wintypes.DWORD()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def window_alive(hwnd: int) -> bool:
    if not IS_WIN or not hwnd:
        return False
    import ctypes
    return bool(ctypes.WinDLL("user32").IsWindow(ctypes.c_void_p(hwnd)))


def foreground_app() -> tuple[str, bool, str, int]:
    """(exe_name, is_fullscreen, window_title, pid) of the focused window."""
    global LAST_HWND
    if not IS_WIN:
        return ("", False, "", 0)
    import ctypes
    from ctypes import wintypes
    user32, k32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ("", False, "", 0)
    LAST_HWND = hwnd
    hwnd = ctypes.c_void_p(hwnd)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            exe = os.path.basename(buf.value)
        k32.CloseHandle(h)
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 256)
    # fullscreen = window covers its whole monitor
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    hmon = user32.MonitorFromWindow(hwnd, 2)
    if not user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
        return (exe, False, title.value, pid.value)
    m = mi.rcMonitor
    full = rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    user32.GetShellWindow.restype = ctypes.c_void_p
    user32.GetDesktopWindow.restype = ctypes.c_void_p
    if hwnd.value in (user32.GetShellWindow(), user32.GetDesktopWindow()):
        full = False
    return (exe, full, title.value, pid.value)


def _exe_for_pid(pid: int) -> str:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def list_open_apps() -> list[dict]:
    """Apps with a visible window right now: [{exe, path, title}], one entry per exe, biggest window first."""
    if not IS_WIN:
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    found: dict[str, dict] = {}
    me = os.getpid()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # skip owned popups
                return True
            if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            title = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, title, n + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me:
                return True
            path = _exe_for_pid(pid.value)
            if not path:
                return True
            exe = os.path.basename(path)
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            cur = found.get(exe.lower())
            if not cur or area > cur["area"]:
                found[exe.lower()] = {"exe": exe, "path": path, "title": title.value, "area": area}
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return sorted(found.values(), key=lambda d: (-d["area"], d["exe"].lower()))


class GameWatcher(threading.Thread):
    """Polls the foreground window and reports the current game (or None)."""

    def __init__(self, cfg, on_change):
        super().__init__(daemon=True)
        self.cfg, self.on_change = cfg, on_change
        self.current: str | None = None
        self.pid = 0
        self.hwnd = 0
        self.current_exe = ""
        self.last_game: str | None = None
        self._halt = threading.Event()
        self._candidate, self._since = None, 0.0
        self._gone_since = 0.0

    def is_listed(self, exe: str, title: str = "") -> bool:
        """On your games list. javaw.exe on the list means Minecraft, not every Java program."""
        low = exe.lower()
        if low not in (g.lower() for g in self.cfg.get("recording.games", [])):
            return False
        return low not in JAVA_HOSTS or java_app_name(title) == "Minecraft"

    def classify(self, exe: str, full: bool, title: str = "") -> str | None:
        if not exe:
            return None
        rec = self.cfg.get("recording")
        low = exe.lower()
        if self.is_listed(exe, title):  # your own list always wins
            return pretty_name(exe, title)
        if low in NEVER_GAMES or low in (g.lower() for g in rec.get("ignored", [])):
            return None
        if low in JAVA_HOSTS and java_app_name(title).lower() in (g.lower() for g in rec.get("ignored_java", [])):
            return None  # a Java app you marked "Not a game" (by name - Minecraft shares its exe)
        if rec.get("detect_fullscreen", True) and full:
            return pretty_name(exe, title)
        return None

    def run(self):
        """A game 'session' lasts as long as the game's process is alive — alt-tabbing doesn't end it,
        closing the game ends it immediately (that's when the review pops up)."""
        while not self._halt.is_set():
            now = time.time()
            try:
                exe, full, title, pid = foreground_app()
                game = self.classify(exe, full, title)
            except Exception:
                game, pid, exe, title = None, 0, "", ""
            if self.current:
                # anti-cheat games can refuse OpenProcess -> also trust the game's window still existing
                if not process_alive(self.pid) and not window_alive(self.hwnd):
                    if game and game != self.current and pid != self.pid:
                        self.current, self.pid, self.hwnd = game, pid, LAST_HWND  # switched straight into another game
                        self.last_game = game
                        self.on_change(None)
                        self.on_change(game)
                    else:
                        self.current, self.pid, self._candidate = None, 0, None
                        self.on_change(None)
            elif game:
                listed = self.is_listed(exe, title)
                if game != self._candidate:
                    self._candidate, self._since = game, now
                elif now - self._since >= (2.0 if listed else FULLSCREEN_GRACE):
                    self.current, self.pid, self.hwnd, self.last_game = game, pid, LAST_HWND, game
                    self.current_exe = exe
                    self.on_change(game)
            else:
                self._candidate = None
            self._halt.wait(1.0)

    def ignore_current(self):
        """'Not a game' button: never treat this exe as a game again, and end its session now."""
        exe = self.current_exe
        if not exe:
            return None
        if exe.lower() in JAVA_HOSTS and self.current != "Minecraft":
            # ignoring javaw.exe itself would switch Minecraft off too: remember this app by name instead
            names = list(self.cfg.get("recording.ignored_java", []) or [])
            if self.current and self.current.lower() not in (n.lower() for n in names):
                names.append(self.current)
            self.cfg.set("recording.ignored_java", names)
            name = self.current
            self.current, self.pid, self.hwnd, self._candidate, self.current_exe = None, 0, 0, None, ""
            self.on_change(None)
            return name
        ign = list(self.cfg.get("recording.ignored", []))
        if exe.lower() not in (x.lower() for x in ign):
            ign.append(exe)
        self.cfg.set("recording.ignored", ign)
        games = [g for g in self.cfg.get("recording.games", []) if g.lower() != exe.lower()]
        self.cfg.set("recording.games", games)
        self.current, self.pid, self.hwnd, self._candidate, self.current_exe = None, 0, 0, None, ""
        self.on_change(None)
        return exe

    def stop(self):
        self._halt.set()
