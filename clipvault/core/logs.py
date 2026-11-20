"""Crash + error logging to %APPDATA%\\ClipVault\\clipvault.log.

Catches Python exceptions on any thread and native crashes (faulthandler), so when
something "just breaks" there's a stack trace to look at.
"""
from __future__ import annotations

import faulthandler
import logging
import sys
import threading

from .config import app_data_dir

LOG_PATH = app_data_dir() / "clipvault.log"
log = logging.getLogger("clipvault")
_fault_file = None


def setup():
    global _fault_file
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
            LOG_PATH.replace(LOG_PATH.with_suffix(".old.log"))
    except OSError:
        pass
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if sys.stderr is not None:  # running from a console
        root.addHandler(logging.StreamHandler())
    try:
        _fault_file = open(LOG_PATH, "a", encoding="utf-8")
        faulthandler.enable(_fault_file, all_threads=True)
    except Exception:
        pass

    def excepthook(t, v, tb):
        log.critical("Unhandled exception", exc_info=(t, v, tb))

    def thread_hook(args):
        log.error("Unhandled exception in thread %s", getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    log.info("---- ClipVault starting (Python %s) ----", sys.version.split()[0])


def qt_message_handler(mode, context, message):
    if "propagateSizeHints" in message:
        return
    log.warning("Qt: %s", message)
