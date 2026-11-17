"""Release gates must reject mismatched tags and keep incomplete uploads private."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
spec = importlib.util.spec_from_file_location("release_version", SCRIPTS / "release-version.py")
release_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_version)


@pytest.mark.parametrize("version,tag,prerelease", [
    ("0.1.0", "v0.1.0", False),
    ("0.2.0rc1", "v0.2.0rc1", True),
    ("0.2.0.dev1", "v0.2.0.dev1", True),
    ("0.1.0", "", False),  # manual/PR build
])
def test_release_version(version, tag, prerelease):
    assert release_version.release_version({"project": {"version": version}}, tag) == (version, prerelease)


def test_release_tag_must_match_package():
    with pytest.raises(ValueError, match="must match"):
        release_version.release_version({"project": {"version": "0.1.0"}}, "v0.2.0")


def test_release_version_must_be_normalized():
    with pytest.raises(ValueError, match="normalized"):
        release_version.release_version({"project": {"version": "0.2.0-rc1"}}, "v0.2.0-rc1")


@pytest.fixture
def publisher(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''
import json
import os
import sys
with open(os.environ["GH_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
action = sys.argv[2]
if action == "view":
    state = os.environ["FAKE_GH_STATE"]
    if state == "missing":
        sys.exit(1)
    print("true" if state == "draft" else "false")
if action in ("create", "upload") and os.environ.get("FAKE_UPLOAD_FAIL"):
    sys.exit(2)
''')
    gh.chmod(0o755)
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("test-x86_64.whl", "test-aarch64.whl", "test.tar.gz", "SHA256SUMS"):
        (dist / name).touch()
    log = tmp_path / "calls"

    def run(state="missing", prerelease=False, fail_upload=False):
        env = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}", GH_CALLS=str(log),
                   FAKE_GH_STATE=state, RELEASE_TAG="v0.1.0", RELEASE_PRERELEASE=str(prerelease).lower())
        if fail_upload:
            env["FAKE_UPLOAD_FAIL"] = "1"
        result = subprocess.run(["bash", str(SCRIPTS / "publish-release.sh")], cwd=tmp_path,
                                env=env, capture_output=True, text=True)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    return run


def test_publishes_only_after_assets_are_uploaded(publisher):
    result, calls = publisher()
    assert result.returncode == 0, result.stderr
    assert [call[1] for call in calls] == ["view", "create", "edit"]
    assert "--draft" in calls[1] and "--verify-tag" in calls[1]
    assert "dist/test-x86_64.whl" in calls[1] and "dist/test-aarch64.whl" in calls[1]
    assert "dist/SHA256SUMS" in calls[1]
    assert "--draft=false" in calls[2] and "--latest=true" in calls[2]


def test_draft_upload_can_be_retried_without_marking_prerelease_latest(publisher):
    result, calls = publisher(state="draft", prerelease=True)
    assert result.returncode == 0, result.stderr
    assert [call[1] for call in calls] == ["view", "upload", "edit"]
    assert "--prerelease=true" in calls[-1] and "--latest=false" in calls[-1]


def test_published_releases_are_preserved(publisher):
    result, calls = publisher(state="published")
    assert result.returncode != 0
    assert "already published" in result.stderr
    assert [call[1] for call in calls] == ["view"]


@pytest.mark.parametrize("state", ["missing", "draft"])
def test_failed_upload_does_not_publish(publisher, state):
    result, calls = publisher(state=state, fail_upload=True)
    assert result.returncode != 0
    assert "edit" not in [call[1] for call in calls]
