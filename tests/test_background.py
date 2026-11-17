"""Exercise detachment in separate processes, without a desktop or D-Bus session."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from snappie import __main__ as entry, background


def test_startup_routes_to_background_before_dbus(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["snappie"])
    monkeypatch.setattr(background, "detach", lambda: 17)

    def forbidden(argv):
        pytest.fail("D-Bus must not be touched before detaching")

    monkeypatch.setattr(entry, "_forward", forbidden)
    assert entry.main() == 17


@pytest.mark.parametrize("argv", [["--foreground"], ["tray", "--foreground"], ["shot"], ["service"]])
def test_foreground_and_oneshot_commands_do_not_detach(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["snappie", *argv])
    monkeypatch.setattr(entry, "_forward", lambda args: 0)
    monkeypatch.setattr(background, "detach", lambda: pytest.fail("unexpected fork"))
    assert entry.main() == 0


def run_detached(tmp_path, child, *, timeout=30):
    script = f"""
import json, os, sys, time
from pathlib import Path
from snappie import background
background.STARTUP_TIMEOUT = {timeout}
code = background.detach()
if code is None:
{child}
sys.exit(code)
"""
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    return subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=10)


def test_detached_child_has_no_terminal_and_releases_parent(tmp_path):
    child = '''
    pidfile = Path(os.environ["XDG_STATE_HOME"]) / "child.json"
    pidfile.write_text(json.dumps({"pid": os.getpid(), "session": os.getsid(0),
                                   "terminal": os.isatty(0) or os.isatty(1) or os.isatty(2),
                                   "stdin": os.read(0, 1).decode()}))
    print("background output", flush=True)
    background.ready()
    time.sleep(5)
    os._exit(0)
'''
    try:
        result = run_detached(tmp_path, child)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "" and result.stderr == ""
        details = json.loads((tmp_path / "child.json").read_text())
        assert details["session"] != details["pid"]
        assert not details["terminal"] and details["stdin"] == ""
        assert "background output" in (tmp_path / "snappie" / "console.log").read_text()
        os.kill(details["pid"], 0)  # parent returned while the detached child is still running
    finally:
        pidfile = tmp_path / "child.json"
        if pidfile.exists():
            try:
                os.kill(json.loads(pidfile.read_text())["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_failed_startup_is_reported_to_parent(tmp_path):
    child = '''
    print("missing dependency", file=sys.stderr, flush=True)
    background.finished(1)
    os._exit(1)
'''
    result = run_detached(tmp_path, child)
    assert result.returncode == 1
    assert "startup failed" in result.stderr and "console.log" in result.stderr
    assert "missing dependency" in (tmp_path / "snappie" / "console.log").read_text()


def test_crash_before_readiness_is_not_reported_as_success(tmp_path):
    result = run_detached(tmp_path, "    os._exit(23)")
    assert result.returncode == 1 and "startup failed" in result.stderr


def test_forwarding_to_another_instance_reports_success(tmp_path):
    result = run_detached(tmp_path, "    background.finished(0)\n    os._exit(0)")
    assert result.returncode == 0


def test_slow_startup_is_left_running(tmp_path):
    child = '''
    time.sleep(1)
    (Path(os.environ["XDG_STATE_HOME"]) / "survived").touch()
    os._exit(0)
'''
    result = run_detached(tmp_path, child, timeout=0.1)
    assert result.returncode == 0 and "still starting" in result.stderr
    for _ in range(50):
        if (tmp_path / "survived").exists():
            break
        time.sleep(0.1)
    assert (tmp_path / "survived").exists()


def test_console_log_rotates_while_running(tmp_path):
    child = '''
    background.CONSOLE_LIMIT = 10
    print("x" * 20, flush=True)
    background.trim_console()
    print("after", flush=True)
    background.finished(0)
    os._exit(0)
'''
    assert run_detached(tmp_path, child).returncode == 0
    assert (tmp_path / "snappie" / "console.log.1").read_text().startswith("x" * 20)
    assert (tmp_path / "snappie" / "console.log").read_text() == "after\n"


def test_log_lines_reach_console_only_until_started(monkeypatch, capsys):
    from snappie import app_util
    monkeypatch.setattr(app_util, "LOG_FILE", os.devnull)
    monkeypatch.setattr(background, "started", False)
    app_util.log("during startup")
    monkeypatch.setattr(background, "started", True)
    app_util.log("after startup")
    err = capsys.readouterr().err
    assert "during startup" in err and "after startup" not in err


def test_log_directory_failure_is_reported(tmp_path):
    (tmp_path / "snappie").write_text("a file blocks the directory")
    result = run_detached(tmp_path, "    os._exit(0)")
    assert result.returncode == 1 and "could not start in the background" in result.stderr
