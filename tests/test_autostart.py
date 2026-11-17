"""The autostart entry keeps the tray in the foreground, also for entries from older versions."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from snappie import app  # noqa: E402


@pytest.fixture
def entry(tmp_path, monkeypatch):
    path = tmp_path / "autostart" / "snappie.desktop"
    monkeypatch.setattr(app, "AUTOSTART", str(path))
    monkeypatch.setattr("snappie.hotkeys.launcher", lambda: "/opt/snappie")
    return path


def test_old_entry_is_rewritten(entry):
    entry.parent.mkdir()
    entry.write_text("[Desktop Entry]\nType=Application\nName=snappie\nExec=/opt/snappie tray\n")
    app._refresh_autostart()
    assert "Exec=/opt/snappie tray --foreground\n" in entry.read_text()


def test_disabled_autostart_stays_off(entry):
    app._refresh_autostart()
    assert not entry.exists()
