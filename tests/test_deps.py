"""Distro detection and package names for `snappie deps`."""

import pytest

from snappie import deps


@pytest.mark.parametrize("text, fam", [
    ('ID=arch\n', "arch"),
    ('ID=endeavouros\nID_LIKE=arch\n', "arch"),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "suse"),
    ('ID=debian\n', "debian"),
    ('ID=ubuntu\nID_LIKE=debian\n', "debian"),
    ('ID=neon\nID_LIKE="ubuntu debian"\n', "debian"),
    ('ID=fedora\n', None),
])
def test_family(tmp_path, text, fam):
    path = tmp_path / "os-release"
    path.write_text('NAME="Some Linux"\n' + text)
    assert deps.family(path) == fam


def test_family_without_os_release(tmp_path):
    assert deps.family(tmp_path / "missing") is None


def test_every_dep_has_packages_for_every_family():
    for dep in deps.DEPS:
        assert set(dep.packages) == set(deps.INSTALL), dep.key
        assert all(dep.packages.values()), dep.key


def test_debian_packages():
    pkgs = deps.packages(["pyqt6", "qtsvg", "gst", "wayland"], fam="debian")
    assert pkgs == ["python3-pyqt6", "python3-pyqt6.qtsvg", "gir1.2-gstreamer-1.0",
                    "libwayland-dev", "libwayland-bin"]


def test_debian_tesseract_data():
    assert deps.packages(["tesseract"], "eng+chi_sim", "debian") == [
        "tesseract-ocr", "tesseract-ocr-eng", "tesseract-ocr-chi-sim"]


def test_install_hint_on_debian(monkeypatch):
    monkeypatch.setattr(deps, "family", lambda: "debian")
    assert deps.install_hint(["ffmpeg", "openssh"]) == "sudo apt install ffmpeg openssh-client"
    assert deps.tesseract_data_hint("deu") == "sudo apt install tesseract-ocr-deu"
