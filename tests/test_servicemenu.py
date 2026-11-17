import configparser
import os

from snappie import servicemenu


def test_install_writes_executable_menus_once(tmp_path, monkeypatch):
    monkeypatch.setattr(servicemenu, "DIR", str(tmp_path))
    assert servicemenu.install("/opt/bin/snappie") is True
    assert servicemenu.install("/opt/bin/snappie") is False      # unchanged: nothing written
    names = sorted(os.listdir(tmp_path))
    assert names == sorted(f"snappie-{n}.desktop" for n in servicemenu.MENUS)
    for n in names:
        assert os.access(tmp_path / n, os.X_OK)
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read(tmp_path / n)
        assert cp["Desktop Entry"]["X-KDE-Submenu"] == "Snappie"
        for aid in cp["Desktop Entry"]["Actions"].strip(";").split(";"):
            assert cp[f"Desktop Action {aid}"]["Exec"].startswith("/opt/bin/snappie ")
    up = (tmp_path / "snappie-upload.desktop").read_text()
    assert "MimeType=all/allfiles;" in up and "Exec=/opt/bin/snappie upload %F" in up
    assert servicemenu.install("/other/snappie") is True          # a moved launcher is rewritten
    (tmp_path / "snappie-old.desktop").write_text("")                # a menu an older version wrote
    (tmp_path / "other.desktop").write_text("")                      # not ours
    assert servicemenu.install("/other/snappie") is True
    assert "snappie-old.desktop" not in os.listdir(tmp_path)
    servicemenu.uninstall()
    assert os.listdir(tmp_path) == ["other.desktop"]
    servicemenu.uninstall()                                       # nothing there: fine
