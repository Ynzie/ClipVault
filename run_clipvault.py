"""Launcher for the built app.

The in-app updater (clipvault/core/updater.py) downloads newer app code from GitHub into
%APPDATA%\\ClipVault\\update\\clipvault. When that's there (and fits this build), it's used instead of
the code baked into the exe - so updating doesn't need a rebuild. If an update ever fails to start,
it's thrown away and the built-in code runs again.
"""
import os
import sys

BUILD = 2   # bump when an update can't work without rebuilding (new packages, new bundled files)


def _update_root():
    base = os.environ.get("APPDATA")
    return os.path.join(base, "ClipVault", "update") if base else os.path.join(os.path.expanduser("~"), ".clipvault", "update")


def _discard(root, why):
    import json
    import shutil
    pkg = os.path.join(root, "clipvault")
    try:
        with open(os.path.join(root, "installed.json"), encoding="utf-8") as f:
            sha = json.load(f).get("sha")
        if sha and why == "failed":
            with open(os.path.join(root, "bad.json"), "w", encoding="utf-8") as f:
                json.dump({"sha": sha}, f)
    except (OSError, ValueError):
        pass
    for name in ("clipvault", "installed.json", "starting"):
        p = os.path.join(root, name)
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass
    return not os.path.exists(pkg)


def _use_update():
    """Install an import hook for the downloaded code. Returns the hook (or None for built-in code)."""
    if not getattr(sys, "frozen", False):
        return None
    root = _update_root()
    pkg = os.path.join(root, "clipvault")
    if not os.path.isfile(os.path.join(pkg, "__main__.py")):
        return None
    try:
        import json
        import re
        import time
        flag = os.path.join(root, "starting")
        # the app deletes this flag once it's up (after ~5 s). Still there much later = that start never got going.
        if os.path.exists(flag) and time.time() - os.path.getmtime(flag) > 30:
            _discard(root, "failed")
            return None
        with open(os.path.join(root, "installed.json"), encoding="utf-8") as f:
            applied = float(json.load(f).get("applied") or 0)
        if os.path.getmtime(sys.executable) > applied:         # the app was rebuilt since: the build is newer
            _discard(root, "older")
            return None
        with open(os.path.join(pkg, "version.py"), encoding="utf-8") as f:
            m = re.search(r"^MIN_BUILD\s*=\s*(\d+)", f.read(), re.M)
        if m and int(m.group(1)) > BUILD:
            return None
        if not os.path.exists(flag):
            with open(flag, "w") as f:
                f.write("1")
    except (OSError, ValueError):
        return None

    import importlib.abc
    import importlib.machinery

    class UpdateFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "clipvault" or name.startswith("clipvault."):
                return importlib.machinery.PathFinder.find_spec(name, [root] if path is None else path)
            return None

    finder = UpdateFinder()
    sys.meta_path.insert(0, finder)
    return finder


def _start():
    os.environ["CLIPVAULT_BUILD"] = str(BUILD)
    finder = _use_update()
    try:
        from clipvault.__main__ import main
    except Exception:
        if finder is None:
            raise
        sys.meta_path.remove(finder)   # broken update: back to the built-in code
        for name in [n for n in sys.modules if n == "clipvault" or n.startswith("clipvault.")]:
            del sys.modules[name]
        _discard(_update_root(), "failed")
        from clipvault.__main__ import main
    main()


if __name__ == "__main__":
    _start()
