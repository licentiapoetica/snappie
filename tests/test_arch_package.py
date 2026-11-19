"""Native packages must use the release source, its verified hash and valid Arch versions."""

import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

script = Path(__file__).resolve().parents[1] / ".github/scripts/prepare-arch-package.py"
spec = importlib.util.spec_from_file_location("arch_package", script)
arch_package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arch_package)


def archive(tmp_path, version):
    path = tmp_path / f"snappie-{version}.tar.gz"
    with tarfile.open(path, "w:gz") as source:
        metadata = f'[project]\nversion = "{version}"\n'.encode()
        member = tarfile.TarInfo(f"snappie-{version}/pyproject.toml")
        member.size = len(metadata)
        source.addfile(member, io.BytesIO(metadata))
    return path


def test_recipe_has_real_checksum_and_both_packages(tmp_path):
    source = archive(tmp_path, "0.1.0")
    target = tmp_path / "build"
    arch_package.prepare(source, target, "0.1.0")
    recipe = (target / "PKGBUILD").read_text()
    assert hashlib.sha256(source.read_bytes()).hexdigest() in recipe
    assert "SKIP" not in recipe and "@PKGVER@" not in recipe
    assert (target / source.name).read_bytes() == source.read_bytes()
    result = subprocess.run(["bash", "-c", 'source "$1"; printf "%s\\n" "${pkgname[@]}"',
                             "test", str(target / "PKGBUILD")], capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["snappie", "snappie-purpose"]


def test_mismatched_source_version_is_rejected(tmp_path):
    source = archive(tmp_path, "0.1.0")
    source = source.rename(tmp_path / "snappie-0.2.0.tar.gz")
    with pytest.raises(ValueError, match="does not match"):
        arch_package.prepare(source, tmp_path / "build", "0.2.0")


@pytest.mark.parametrize("version,expected,epoch", [
    ("0.1.0", "0.1.0", 0),
    ("0.2.0rc1", "0.2.0rc1", 0),
    ("0.2.0.dev1", "0.2.0Adev1", 0),
    ("0.2.0rc1.dev1", "0.2.0rc1dev1", 0),
    ("1!0.2.0.post1", "0.2.0.post1", 1),
    ("0.2.0+abc.dev1", "0.2.0+abc.dev1", 0),
])
def test_arch_versions(version, expected, epoch):
    assert arch_package.arch_version(version) == (expected, epoch)


@pytest.mark.skipif(not shutil.which("vercmp"), reason="Arch vercmp is not installed")
@pytest.mark.parametrize("older,newer", [("0.2.0.dev1", "0.2.0a0"),
                                        ("0.2.0.dev1", "0.2.0a0.dev1"),
                                        ("0.2.0rc1", "0.2.0"), ("0.2.0", "0.2.0.post1")])
def test_arch_order_matches_prerelease_and_final_order(older, newer):
    values = [arch_package.arch_version(v) for v in (older, newer)]
    versions = [f"{epoch}:{version}" for version, epoch in values]
    result = subprocess.check_output(["vercmp", *versions], text=True)
    assert int(result) < 0
