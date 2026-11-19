import os

import pytest

from snappie import cli


@pytest.mark.parametrize("argv,foreground", [
    ([], False), (["tray"], False), (["--foreground"], True), (["tray", "--foreground"], True),
])
def test_tray_startup_mode(argv, foreground):
    ns = cli.parse(argv)
    assert ns.cmd == "tray" and ns.foreground is foreground


def test_background_option_keeps_legacy_recording_options():
    assert cli.parse(["--fps", "60"]).cmd == "record"
    assert cli.parse(["--stop"]).cmd == "stop"
    assert cli.parse(["service"]).cmd == "service"
    with pytest.raises(cli.ArgError):
        cli.parse(["shot", "--foreground"])


def test_upload_takes_several_files():
    ns = cli.parse(["upload", "a.png", "b.mp4"])
    assert ns.cmd == "upload" and ns.files == ["a.png", "b.mp4"]


def test_absolute_paths_for_forwarded_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.absolute_paths(["upload", "a.png", "/x/b.png"]) == ["upload", str(tmp_path / "a.png"), "/x/b.png"]
    assert cli.absolute_paths(["edit", "a.png"]) == ["edit", str(tmp_path / "a.png")]
    assert cli.absolute_paths(["shot", "--then", "save", "-o", "s.png"]) == \
        ["shot", "--then", "save", "-o", str(tmp_path / "s.png")]
    assert cli.absolute_paths(["record", "--output=v.mp4", "--gif"]) == \
        ["record", "--output=" + os.path.join(str(tmp_path), "v.mp4"), "--gif"]
    assert cli.absolute_paths(["shot", "--screen"]) == ["shot", "--screen"]


def test_absolute_paths_keep_uploader_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = str(tmp_path / "a.png")
    assert cli.absolute_paths(["upload", "-u", "catbox.sxcu", "a.png"]) == ["upload", "-u", "catbox.sxcu", a]
    assert cli.absolute_paths(["upload", "--uploader", "x/my.sxcu", "a.png"]) == \
        ["upload", "--uploader", str(tmp_path / "x/my.sxcu"), a]
    assert cli.absolute_paths(["upload", "a.png", "--uploader=./my.sxcu"]) == \
        ["upload", a, "--uploader=" + str(tmp_path / "my.sxcu")]
    assert cli.parse(["upload", "-u", "c.sxcu", "a", "b"]).uploader == "c.sxcu"


def test_ocr_file_and_convert():
    assert cli.parse(["ocr"]).file is None
    assert cli.parse(["ocr", "a.png"]).file == "a.png"
    ns = cli.parse(["convert", "--webp", "v.mp4"])
    assert ns.to == "webp" and ns.file == "v.mp4"
    assert cli.parse(["convert", "v.mp4"]).to is None          # no format: the convert window
    ns = cli.parse(["convert", "--to", "mp4", "--start", "1:02.5", "--cut", "70-75", "--cut", "1:20-1:30",
                    "--crop", "1920:800:0:140", "--speed", "2", "a.gif"])
    assert ns.start == 62.5 and ns.cut == [(70, 75), (80, 90)] and ns.crop == (0, 140, 1920, 800)
    assert ns.speed == 2 and ns.repeat == 1 and ns.end is None
    assert cli.parse(["convert", "--mp4", "--crop", "auto", "a.mkv"]).crop == "auto"
    for bad in (["--gif", "--mp4"], ["--quality", "0"], ["--cut", "5-2"], ["--crop", "10:10"], ["--to", "avi"],
                ["--start", "x"], ["-c", "prores"]):
        with pytest.raises(cli.ArgError):
            cli.parse(["convert", *bad, "v.mp4"])


def test_absolute_paths_keep_convert_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.absolute_paths(["convert", "--to", "mp4", "--background", "#000", "--cut", "1-2", "-o", "o.mp4",
                               "a.gif"]) == ["convert", "--to", "mp4", "--background", "#000", "--cut", "1-2",
                                             "-o", str(tmp_path / "o.mp4"), str(tmp_path / "a.gif")]
    assert cli.absolute_paths(["convert", "--webm", "-c", "vp8", "a.mp4"]) == [
        "convert", "--webm", "-c", "vp8", str(tmp_path / "a.mp4")]


def test_convert_codec():
    assert cli.parse(["convert", "--webm", "-c", "vp8", "v.mp4"]).codec == "vp8"
    assert cli.parse(["convert", "--mp4", "v.mkv"]).codec is None


def test_convert_codecs_match_the_converter():
    from snappie import convert
    assert sorted(cli.CONVERT_CODECS) == sorted({k for codecs in convert.CODECS.values() for k in codecs})


def test_convert_size():
    ns = cli.parse(["convert", "--mp4", "--size", "8M", "--two-pass", "v.mkv"])
    assert ns.size == 8_000_000 and ns.two_pass
    assert cli.parse(["convert", "--mp4", "--size", "8", "v.mkv"]).size == 8_000_000
    assert cli.parse(["convert", "--mp4", "--size", "1.5GB", "v.mkv"]).size == 1_500_000_000
    assert cli.parse(["convert", "--mp4", "--size", "500k", "v.mkv"]).size == 500_000
    for bad in ("8X", "1k", "big"):
        with pytest.raises(cli.ArgError):
            cli.parse(["convert", "--mp4", "--size", bad, "v.mkv"])


@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_version(flag, capsys):
    import snappie
    assert cli.normalize([flag]) == [flag]      # not "record -v"
    with pytest.raises(SystemExit) as e:
        cli.parse([flag])
    assert e.value.code == 0
    assert capsys.readouterr().out == f"snappie {snappie.version()}\n"


def test_version_outside_a_checkout(monkeypatch):
    import snappie
    monkeypatch.setattr(snappie, "_described", None)
    monkeypatch.setattr(snappie, "_git_describe", lambda top: None)
    assert snappie.version() == snappie.__version__
    monkeypatch.setattr(snappie, "_described", None)
    monkeypatch.setattr(snappie, "_git_describe", lambda top: f"v{snappie.__version__}")
    assert snappie.version() == snappie.__version__          # exactly the release tag
    monkeypatch.setattr(snappie, "_described", None)
    monkeypatch.setattr(snappie, "_git_describe", lambda top: "v0.1.0-3-gefd4133-dirty")
    assert snappie.version() == f"{snappie.__version__} (git v0.1.0-3-gefd4133-dirty)"
