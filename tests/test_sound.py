import pytest

from snappie import sound


@pytest.fixture
def themes(tmp_path, monkeypatch):
    """Two data dirs with a few themed sounds, a KDE config choosing the "mytheme" theme."""
    home, usr, cfg = tmp_path / "home", tmp_path / "usr", tmp_path / "cfg"
    for d, theme, name in [(usr, "freedesktop", "camera-shutter.oga"), (usr, "ocean", "completion-success.oga"),
                           (usr, "freedesktop", "complete.oga"), (home, "mytheme", "complete.ogg")]:
        (d / "sounds" / theme / "stereo").mkdir(parents=True, exist_ok=True)
        (d / "sounds" / theme / "stereo" / name).write_bytes(b"x")
    cfg.mkdir()
    (cfg / "kdeglobals").write_text("[General]\nfoo=1\n[Sounds]\nTheme=mytheme\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setenv("XDG_DATA_DIRS", str(usr))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    return tmp_path


def test_theme_order(themes):
    assert sound.kde_theme() == "mytheme"
    # the KDE theme wins, even over an earlier name found in a later theme
    assert sound.theme_file(["completion-success", "complete"]).endswith("/home/sounds/mytheme/stereo/complete.ogg")
    # not in the KDE theme or ocean: freedesktop
    assert sound.theme_file(["camera-shutter"]).endswith("/usr/sounds/freedesktop/stereo/camera-shutter.oga")
    assert sound.theme_file(["nope"]) is None


def test_custom_file_wins_if_it_exists(themes, monkeypatch):
    custom = themes / "mine.wav"
    custom.write_bytes(b"x")
    monkeypatch.setattr(sound.settings, "get", lambda key: str(custom) if key == "sound.shot.file" else None)
    assert sound.file_for("shot") == str(custom)
    assert sound.file_for("shot", str(themes / "gone.wav")).endswith("camera-shutter.oga")


def test_play_off_still_runs_then(monkeypatch):
    monkeypatch.setattr(sound.settings, "get", lambda key: False)
    ran = []
    sound.play("shot", then=lambda: ran.append(1))
    assert ran == [1] and not sound.playing()


def test_volume_is_perceptual():
    assert sound._linear(100) == 1 and sound._linear(0) == 0
    assert sound._linear(50) == pytest.approx(0.125)
    assert sound._linear(None) == pytest.approx(0.343)
