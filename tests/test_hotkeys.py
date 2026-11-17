"""Desktop entries must keep working when an installed venv isn't on PATH."""

import os
import configparser

from snappie import hotkeys


def test_desktop_launcher_stays_attached_to_session_manager():
    desktop = configparser.ConfigParser(interpolation=None)
    desktop.read_string(hotkeys.desktop_entry("/opt/snappie"))
    assert desktop["Desktop Entry"]["Exec"] == "/opt/snappie tray --foreground"
    assert desktop["Desktop Action Screenshot"]["Exec"] == "/opt/snappie shot"


def test_launcher_uses_invoked_entry_point(tmp_path, monkeypatch):
    entry = tmp_path / "bin" / "snappie"
    entry.parent.mkdir()
    entry.touch()
    monkeypatch.setattr(hotkeys.sys, "argv", [str(entry), "shot"])
    monkeypatch.setattr(hotkeys.shutil, "which", lambda _: "/other/bin/snappie")
    assert hotkeys.launcher() == str(entry)


def test_launcher_finds_venv_entry_point_outside_path(tmp_path, monkeypatch):
    entry = tmp_path / "bin" / "snappie"
    entry.parent.mkdir()
    entry.touch()
    monkeypatch.setattr(hotkeys.sys, "argv", ["-c"])
    monkeypatch.setattr(hotkeys.sys, "executable", str(entry.parent / "python"))
    monkeypatch.setattr(hotkeys.shutil, "which", lambda _: None)
    assert hotkeys.launcher() == str(entry)
    assert f"Exec={entry}" in hotkeys.desktop_entry(hotkeys.launcher())


def test_launcher_falls_back_to_checkout(monkeypatch):
    monkeypatch.setattr(hotkeys.sys, "argv", ["-c"])
    monkeypatch.setattr(hotkeys.sys, "executable", "/nonexistent/bin/python")
    monkeypatch.setattr(hotkeys.shutil, "which", lambda _: None)
    assert os.path.isfile(hotkeys.launcher())
    assert hotkeys.launcher().endswith("/snappie.sh")
