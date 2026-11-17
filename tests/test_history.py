import os

import pytest
from PyQt6.QtGui import QColor, QGuiApplication, QImage

from snappie import history, settings


@pytest.fixture
def data(tmp_path, monkeypatch):
    QGuiApplication.instance() or QGuiApplication([])
    monkeypatch.setattr(settings, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(settings, "THUMBS_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(settings, "CLIPBOARD_DIR", str(tmp_path / "clipboard"))
    monkeypatch.setattr(settings, "get", lambda key, default=None: settings.DEFAULTS.get(key, default))
    return tmp_path


def image():
    img = QImage(40, 30, QImage.Format.Format_ARGB32)
    img.fill(QColor("red"))
    return img


def test_copied_screenshot_lands_in_the_history(data):
    e = history.add_copied(image())
    assert e["copied"] and (e["width"], e["height"]) == (40, 30)
    assert os.path.dirname(e["path"]) == str(data / "clipboard") and e["path"].endswith(".png")
    assert QImage(e["path"]).size().width() == 40
    assert history.entries()[0]["id"] == e["id"]
    second = history.add_copied(image())          # same second: its own file
    assert second["path"] != e["path"]


def test_forgetting_removes_only_our_own_files(data):
    copied = history.add_copied(image())
    saved_path = data / "saved.png"
    image().save(str(saved_path))
    saved = history.add("image", str(saved_path))
    history.remove(copied)
    history.remove(saved)
    assert not os.path.exists(copied["path"])
    assert saved_path.exists()                    # the user's own file stays
    assert history.entries() == []


def test_trimming_the_history_deletes_old_copies(data, monkeypatch):
    monkeypatch.setattr(history, "MAX_ENTRIES", 2)
    first = history.add_copied(image())
    for _ in range(102):
        history.add("image", str(data / "x.png"))
    assert not os.path.exists(first["path"])
    assert len(history.entries()) == 2
