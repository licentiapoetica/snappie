"""The AUR development package must advance its version with upstream Git."""

from pathlib import Path
import shutil
import subprocess

import pytest

RECIPE = Path(__file__).resolve().parents[1] / "packaging/aur/snappie-git/PKGBUILD"


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "snappie"
    subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", "--initial-branch=master", str(repo)],
                   check=True)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=Package Test",
                                        "-c", "user.email=package@example.invalid", "-c", "commit.gpgsign=false",
                                        "-c", "tag.gpgsign=false",
                                        *args], text=True).strip()

    git("commit", "--allow-empty", "-qm", "initial")
    return tmp_path, git


def pkgver(directory):
    return subprocess.check_output(["bash", "-c", 'source "$1"; srcdir="$2"; pkgver',
                                    "pkgver-test", str(RECIPE), str(directory)], text=True).strip()


def test_git_version_advances_after_a_tag(upstream):
    directory, git = upstream
    git("tag", "v1.2.3")
    assert pkgver(directory) == f"1.2.3.r0.g{git('rev-parse', '--short=7', 'HEAD')}"
    git("commit", "--allow-empty", "-qm", "next commit")
    assert pkgver(directory) == f"1.2.3.r1.g{git('rev-parse', '--short=7', 'HEAD')}"
    git("tag", "v1.3.0")
    assert pkgver(directory).startswith("1.3.0.r0.g")


def test_git_version_without_release_tags(upstream):
    directory, git = upstream
    git("tag", "unrelated-tag")
    assert pkgver(directory) == f"0.r1.g{git('rev-parse', '--short=7', 'HEAD')}"


@pytest.mark.skipif(not shutil.which("makepkg"), reason="Arch makepkg is not installed")
def test_aur_metadata_is_current():
    generated = subprocess.check_output(["makepkg", "--printsrcinfo"], cwd=RECIPE.parent, text=True)
    assert generated == RECIPE.with_name(".SRCINFO").read_text()
