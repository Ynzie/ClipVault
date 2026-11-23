"""Version info read by the updater (and by the launcher, as plain text)."""

VERSION = "1.1.1"

# The launcher build (BUILD in run_clipvault.py) this code needs. Raise it together with BUILD when a
# change needs a full rebuild (a new Python package, new bundled files) - older installs are then told
# to rebuild instead of getting an update that can't run.
MIN_BUILD = 2
