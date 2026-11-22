"""Updates from GitHub.

The app's Python code lives in the repo (github.com/Ynzie/ClipVault). An update downloads the
`clipvault` package at the newest commit into %APPDATA%\\ClipVault\\update\\clipvault; the launcher
(run_clipvault.py) runs that instead of the code baked into the exe. No rebuild, FFmpeg and the
other bundled files stay as they are. Only what GitHub serves over HTTPS for this repo is used.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from .config import app_data_dir
from .. import version as _version

log = logging.getLogger("clipvault.updater")

REPO = "Ynzie/ClipVault"
BRANCH = "main"
PAGE = f"https://github.com/{REPO}"


def update_dir() -> Path:
    return app_data_dir() / "update"


def running_build() -> int:
    try:
        return int(os.environ.get("CLIPVAULT_BUILD", "0"))
    except ValueError:
        return 0


def can_update() -> bool:
    """Only the built app updates itself (running from source: use git)."""
    return bool(getattr(sys, "frozen", False)) and running_build() >= 1


def _read(name: str) -> dict:
    try:
        return json.loads((update_dir() / name).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict):
    update_dir().mkdir(parents=True, exist_ok=True)
    (update_dir() / name).write_text(json.dumps(data), "utf-8")


def current() -> dict:
    """What's running: {'version', 'sha' (if known), 'updated' (running downloaded code)}"""
    updated = str(update_dir()) in str(Path(_version.__file__).resolve())
    sha = (_read("installed.json") if updated else _read("baseline.json")).get("sha")
    return {"version": _version.VERSION, "sha": sha, "updated": updated}


def started_ok():
    """The app got going: the running update is good (the launcher drops one that never gets here)."""
    try:
        (update_dir() / "starting").unlink(missing_ok=True)
    except OSError:
        pass


def _get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ClipVault-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check() -> dict:
    """Ask GitHub for the newest commit. -> {'available', 'sha', 'version', 'message', 'date',
    'needs_rebuild', 'failed_before'}. Raises OSError when GitHub can't be reached."""
    c = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
    sha = c["sha"]
    msg = (c.get("commit", {}).get("message") or "").strip().splitlines()
    src = _get(f"https://raw.githubusercontent.com/{REPO}/{sha}/clipvault/version.py").decode("utf-8", "replace")
    ver = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    need = re.search(r"^MIN_BUILD\s*=\s*(\d+)", src, re.M)
    info = {"sha": sha, "version": ver.group(1) if ver else "?", "message": msg[0] if msg else "",
            "date": (c.get("commit", {}).get("committer", {}).get("date") or "")[:10],
            "needs_rebuild": bool(need) and int(need.group(1)) > running_build(),
            "failed_before": _read("bad.json").get("sha") == sha}
    cur = current()
    if cur["sha"] is None and info["version"] == cur["version"]:
        # a fresh build of this same version: take GitHub's newest as the starting point
        _write("baseline.json", {"sha": sha})
        cur["sha"] = sha
    info["available"] = cur["sha"] != sha
    return info


def apply(info: dict, progress=None):
    """Download the code at info['sha'] and put it in place (used from the next start). Raises on any problem
    and leaves the running version untouched."""
    if not can_update():
        raise RuntimeError("Only the built app can update itself.")
    if info.get("needs_rebuild"):
        raise RuntimeError("This update needs a new build of ClipVault.")
    root = update_dir()
    stage = root / "staging"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("Downloading…")
    data = _get(f"https://codeload.github.com/{REPO}/zip/{info['sha']}", timeout=120)
    if progress:
        progress("Installing…")
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.infolist():
            parts = Path(m.filename).parts[1:]          # drop the "ClipVault-<sha>/" folder
            if m.is_dir() or len(parts) < 2 or parts[0] != "clipvault" or ".." in parts or "__pycache__" in parts:
                continue
            dest = stage.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(m))
            n += 1
    pkg = stage / "clipvault"
    if n < 10 or not (pkg / "__main__.py").exists() or not (pkg / "version.py").exists():
        raise RuntimeError("The download doesn't look like ClipVault.")
    for f in pkg.rglob("*.py"):                          # a file that doesn't even parse must never be installed
        compile(f.read_text("utf-8"), str(f), "exec")
    old = root / "clipvault.old"
    shutil.rmtree(old, ignore_errors=True)
    live = root / "clipvault"
    if live.exists():
        os.replace(live, old)
    os.replace(pkg, live)
    _write("installed.json", {"sha": info["sha"], "version": info.get("version"), "applied": time.time()})
    for name in ("starting", "bad.json"):
        (root / name).unlink(missing_ok=True)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)
    log.info("update %s (%s) installed", info.get("version"), info["sha"][:7])
