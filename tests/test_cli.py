import os

from snappie import cli


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
    assert ns.webp and not ns.gif and ns.file == "v.mp4"
    import pytest
    with pytest.raises(cli.ArgError):
        cli.parse(["convert", "v.mp4"])          # --gif or --webp is required
