"""Global hotkeys via Win32 RegisterHotKey (works over fullscreen games, no hooks)."""
from __future__ import annotations

import sys
import threading

from .audio import VK

IS_WIN = sys.platform == "win32"
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_APP_RELOAD, WM_QUIT = 0x0312, 0x8001, 0x0012


def parse(combo: str) -> tuple[int, int] | None:
    if not combo:
        return None
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    mods, key = 0, None
    for p in parts:
        if p.lower() in MODS:
            mods |= MODS[p.lower()]
        else:
            key = VK.get(p) or VK.get(p.upper())
    return (mods, key) if key else None


class HotkeyManager(threading.Thread):
    def __init__(self, bindings: dict[str, str], on_hotkey):
        super().__init__(daemon=True)
        self.bindings = dict(bindings)
        self.on_hotkey = on_hotkey
        self._tid = None
        self._ready = threading.Event()
        self.failed: list[str] = []

    def suspend(self, on: bool):
        """Temporarily release all global hotkeys (while a hotkey field is listening)."""
        if on:
            self._saved = getattr(self, "_saved", None) or dict(self.bindings)
            self.update({})
        elif getattr(self, "_saved", None) is not None:
            saved, self._saved = self._saved, None
            self.update(saved)

    def update(self, bindings: dict[str, str]):
        if getattr(self, "_saved", None) is not None and bindings:
            self._saved = dict(bindings)  # changed while suspended: apply on resume
            return
        self.bindings = dict(bindings)
        if IS_WIN and self._tid:
            import ctypes
            ctypes.WinDLL("user32").PostThreadMessageW(self._tid, WM_APP_RELOAD, 0, 0)

    def stop(self):
        if IS_WIN and self._tid:
            import ctypes
            ctypes.WinDLL("user32").PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def run(self):
        if not IS_WIN:
            return
        import ctypes
        from ctypes import wintypes
        user32, k32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
        self._tid = k32.GetCurrentThreadId()
        ids: dict[int, str] = {}
        mouse: list[tuple[str, int, int]] = []   # side mouse buttons can't use RegisterHotKey -> polled
        mouse_down: dict[str, bool] = {}
        timer = [0]

        def register():
            for i in list(ids):
                user32.UnregisterHotKey(None, i)
            ids.clear()
            mouse.clear()
            self.failed = []
            for n, (name, combo) in enumerate(self.bindings.items(), start=1):
                p = parse(combo)
                if not p:
                    continue
                if p[1] in (0x05, 0x06):
                    mouse.append((name, p[0], p[1]))
                    continue
                if user32.RegisterHotKey(None, n, p[0] | MOD_NOREPEAT, p[1]):
                    ids[n] = name
                else:
                    self.failed.append(name)
            if mouse and not timer[0]:
                timer[0] = user32.SetTimer(None, 0, 25, None)
            elif not mouse and timer[0]:
                user32.KillTimer(None, timer[0])
                timer[0] = 0

        def mods_down(mods):
            for bit, vk in ((0x1, 0x12), (0x2, 0x11), (0x4, 0x10)):
                if bool(mods & bit) != bool(user32.GetAsyncKeyState(vk) & 0x8000):
                    return False
            return True

        register()
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                try:
                    self.on_hotkey(ids[msg.wParam])
                except Exception:
                    pass
            elif msg.message == WM_APP_RELOAD:
                register()
            elif msg.message == 0x0113:  # WM_TIMER: poll side mouse buttons
                for name, mods, vk in mouse:
                    down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                    if down and not mouse_down.get(name) and mods_down(mods):
                        try:
                            self.on_hotkey(name)
                        except Exception:
                            pass
                    mouse_down[name] = down
        for i in ids:
            user32.UnregisterHotKey(None, i)
