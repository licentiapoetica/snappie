"""Detach tray startup before importing D-Bus, GLib or Qt.

The original process waits for a readiness pipe, so failed startup still has a
nonzero exit status. A second fork leaves no session leader tied to the terminal.
"""

import os
from pathlib import Path
import select
import signal
import sys

_ready_fd = None
STARTUP_TIMEOUT = 30


def _log_path():
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "snappie" / "console.log"


def _notify(message):
    global _ready_fd
    if _ready_fd is not None:
        try:
            os.write(_ready_fd, message)
        except BrokenPipeError:
            pass
        finally:
            os.close(_ready_fd)
            _ready_fd = None


def ready():
    """Called from the event loop, once tray initialization has completed."""
    _notify(b"ready")
    return False  # remove the GLib idle source


def finished(code):
    # Also acknowledge the race where another instance started before we acquired D-Bus.
    _notify(b"ready" if code == 0 else b"failed")


def detach():
    """Return an exit code in the parent, or None in the detached child."""
    global _ready_fd
    path = _log_path()
    read_fd = write_fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".log.1"))
        with open(path, "ab", buffering=0) as console, open(os.devnull, "rb") as null:
            read_fd, write_fd = os.pipe()
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                read_fd = None
                _ready_fd = write_fd
                write_fd = None
                os.setsid()
                if os.fork() != 0:
                    os._exit(0)
                os.dup2(null.fileno(), 0)
                os.dup2(console.fileno(), 1)
                os.dup2(console.fileno(), 2)
                return None
            os.close(write_fd)
            write_fd = None
            os.waitpid(pid, 0)
            readable, _, _ = select.select([read_fd], [], [], STARTUP_TIMEOUT)
            if not readable:
                # Only our detached startup and its helper children share this group.
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                print(f"snappie: startup timed out; see {path}", file=sys.stderr)
                return 1
            if os.read(read_fd, 16) == b"ready":
                return 0
            print(f"snappie: startup failed; see {path}", file=sys.stderr)
            return 1
    except OSError as e:
        print(f"snappie: could not start in the background: {e}", file=sys.stderr)
        return 1
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
