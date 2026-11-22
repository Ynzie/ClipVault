"""ClipVault entry point:  python -m clipvault  [--minimized]"""
from __future__ import annotations

import os
import sys


def main():
    if sys.platform == "win32":
        import ctypes
        # (DPI awareness is left to Qt — forcing it here made Qt fall back to worse scaling)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Ynzie.ClipVault")
        os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

    from .core import logs
    logs.setup()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt, qInstallMessageHandler
    qInstallMessageHandler(logs.qt_message_handler)
    from .app import Controller
    from .ui.main_window import MainWindow

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("ClipVault")
    app.setQuitOnLastWindowClosed(False)

    # one instance only: a second launch (e.g. double-clicking the exe while it sits in the tray)
    # tells the running one to show itself, then exits — no second recorder/hotkey set.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from .core import updater
    if "--after-update" in sys.argv:  # the old copy is still closing down
        import time
        time.sleep(3)
    ipc_name = "ClipVault-" + (os.environ.get("USERNAME") or "user")
    probe = QLocalSocket()
    probe.connectToServer(ipc_name)
    if probe.waitForConnected(400):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(400)
        probe.disconnectFromServer()
        updater.started_ok()
        return
    QLocalServer.removeServer(ipc_name)
    server = QLocalServer()
    server.listen(ipc_name)

    ctl = Controller()
    minimized = "--minimized" in sys.argv or ctl.cfg.get("app.start_minimized")
    win = MainWindow(ctl, start_hidden=minimized)
    if not minimized:
        win.show()
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.bring_up()))
    ctl.start()
    from PySide6.QtCore import QTimer
    QTimer.singleShot(5000, updater.started_ok)  # up and running: the code we're on is good
    code = app.exec()
    sys.exit(code)


if __name__ == "__main__":
    main()
