"""Snappie's version lives here, and only here: pyproject.toml reads it (setuptools `attr:`),
the release workflow checks the tag against it, and `snappie -v` and the settings show it.
Kept import-light: every `snappie <command>` imports this package."""

import os

__version__ = "0.1.0"

_described = None


def version():
    """__version__, plus where it came from when run from a git checkout that isn't exactly the
    release tag, e.g. "0.1.0 (git v0.1.0-3-gefd4133-dirty)". Installed wheels show __version__."""
    global _described
    if _described is None:
        _described = __version__
        info = _git_describe(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if info and info != f"v{__version__}":
            _described = f"{__version__} (git {info})"
    return _described


def _git_describe(top):
    if not os.path.exists(os.path.join(top, ".git")):     # a file in worktrees
        return None
    import subprocess
    try:
        out = subprocess.run(["git", "-C", top, "describe", "--tags", "--match", "v[0-9]*", "--dirty",
                              "--always"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None
