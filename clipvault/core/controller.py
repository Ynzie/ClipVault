"""Controller shortcuts: press a button combo on an Xbox / XInput controller to clip, bookmark or screenshot.

Polls XInput (xinput1_4.dll, built into Windows) ~30 times a second from a background thread. Works with
Xbox controllers and anything Steam Input / DS4Windows presents as one. VR controllers aren't XInput.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time

log = logging.getLogger("clipvault.controller")

BUTTONS = {"A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000, "LB": 0x0100, "RB": 0x0200, "LT": 0x10000,
           "RT": 0x20000, "View": 0x0020, "Menu": 0x0010, "LS": 0x0040, "RS": 0x0080,
           "Up": 0x0001, "Down": 0x0002, "Left": 0x0004, "Right": 0x0008}
ORDER = list(BUTTONS)
ACTIONS = ("clip", "bookmark", "screenshot")


def parse(combo: str) -> int:
    m = 0
    for part in (combo or "").split("+"):
        part = part.strip()
        for name, bit in BUTTONS.items():
            if part.lower() == name.lower():
                m |= bit
    return m


def names(mask: int) -> str:
    return "+".join(n for n in ORDER if mask & BUTTONS[n])


class _GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _GAMEPAD)]


def _load_xinput():
    if sys.platform != "win32":
        return None
    for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            x = ctypes.WinDLL(dll)
            x.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(_STATE)]
            x.XInputGetState.restype = ctypes.c_uint
            return x
        except OSError:
            continue
    return None


class PadShortcuts:
    def __init__(self, cfg, on_action, read_state=None):
        """on_action(name) is called from the poll thread. read_state(slot) -> mask|None is a test hook."""
        self.cfg = cfg
        self.on_action = on_action
        self._read = read_state
        self._x = None
        self._stop = threading.Event()
        self._thread = None
        self.connected: set[int] = set()
        self._capture = None  # (callback, deadline, max_mask)
        self._prev: dict[int, int] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._read is None:
            self._x = _load_xinput()
            if self._x is None:
                return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="PadShortcuts")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def capture(self, callback, timeout: float = 8.0):
        """Next combo pressed (all buttons held together, then released) -> callback('View+Menu' | '')."""
        self._capture = [callback, time.time() + timeout, 0]

    # ------------------------------------------------------------------ polling
    def _state(self, slot: int):
        if self._read is not None:
            return self._read(slot)
        st = _STATE()
        if self._x.XInputGetState(slot, ctypes.byref(st)) != 0:
            return None
        g = st.Gamepad
        return g.wButtons | (BUTTONS["LT"] if g.bLeftTrigger > 120 else 0) | (BUTTONS["RT"] if g.bRightTrigger > 120 else 0)

    def _loop(self):
        last_scan = 0.0
        while not self._stop.is_set():
            now = time.time()
            # asking an empty slot is slow on some systems: only rescan for new controllers every 2 s
            slots = range(4) if now - last_scan > 2 or not self.connected else sorted(self.connected)
            if now - last_scan > 2:
                last_scan = now
            for slot in slots:
                try:
                    m = self._state(slot)
                except Exception:
                    m = None
                if m is None:
                    self.connected.discard(slot)
                    self._prev.pop(slot, None)
                    continue
                self.connected.add(slot)
                self._handle(slot, m)
            cap = self._capture
            if cap is not None and time.time() > cap[1]:  # nothing pressed (or no controller) in time
                self._capture = None
                cap[0](names(cap[2]) if cap[2] else "")
            self._stop.wait(0.033)

    def _handle(self, slot: int, mask: int):
        prev = self._prev.get(slot, 0)
        self._prev[slot] = mask
        cap = self._capture
        if cap is not None:
            if time.time() > cap[1]:
                self._capture = None
                cap[0]("")
            elif mask:
                cap[2] |= mask
            elif cap[2]:  # everything released: that's the combo
                self._capture = None
                cap[0](names(cap[2]))
            return
        if not self.cfg.get("controller.enabled", True):
            return
        for action in ACTIONS:
            want = parse(self.cfg.get(f"controller.{action}", ""))
            if want and (mask & want) == want and (prev & want) != want:
                try:
                    self.on_action(action)
                except Exception:
                    log.exception("controller action %s failed", action)
