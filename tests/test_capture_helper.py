"""Helper upgrades and authorization must work without compilers in wheel installs."""

from pathlib import Path
import subprocess

import pytest

from snappie import capture_helper as helper, deps


@pytest.fixture
def installation(tmp_path, monkeypatch):
    source = tmp_path / "package" / "helper"
    source.mkdir(parents=True)
    for name in ("snappie-cast.c", "fake-input.xml", "zkde-screencast-unstable-v1.xml", "build.sh"):
        (source / name).write_text(name)
    executable = tmp_path / "user data" / "snappie" / "snappie-cast"
    monkeypatch.setattr(helper, "SOURCE", source)
    monkeypatch.setattr(helper, "BUNDLED", source / "snappie-cast")
    monkeypatch.setattr(helper, "HELPER", executable)
    monkeypatch.setattr(helper, "STAMP", executable.with_suffix(".source-hash"))
    monkeypatch.setattr(helper, "DESKTOP", tmp_path / "user data" / "applications" / "snappie-cast.desktop")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "sh":
            Path(command[-1]).write_bytes(b"compiled helper")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(helper.subprocess, "run", run)
    return calls


def test_bundled_helper_installs_without_compiling(installation):
    helper.BUNDLED.write_bytes(b"bundled helper")
    assert not helper.needs_build()
    helper.ensure_helper()
    assert helper.HELPER.read_bytes() == b"bundled helper"
    assert helper.HELPER.stat().st_mode & 0o777 == 0o755
    assert f'Exec="{helper.HELPER}"' in helper.DESKTOP.read_text()
    assert installation == [["kbuildsycoca6"]]


def test_upgrade_uses_contents_and_repairs_missing_desktop_entry(installation):
    helper.BUNDLED.write_bytes(b"first version")
    helper.ensure_helper()
    helper.BUNDLED.write_bytes(b"newer version")
    helper.ensure_helper()
    assert helper.HELPER.read_bytes() == b"newer version"
    modified = helper.HELPER.stat().st_mtime_ns
    helper.DESKTOP.unlink()
    helper.ensure_helper()
    assert helper.DESKTOP.is_file()
    assert helper.HELPER.stat().st_mtime_ns == modified
    assert all(command == ["kbuildsycoca6"] for command in installation)


@pytest.mark.parametrize("name", ["snappie-cast.c", "fake-input.xml", "build.sh"])
def test_checkout_rebuilds_when_sources_change(installation, name):
    assert helper.needs_build()
    helper.ensure_helper()
    assert not helper.needs_build()
    helper.ensure_helper()
    assert len([cmd for cmd in installation if cmd[0] == "sh"]) == 1
    (helper.SOURCE / name).write_text("changed source")
    assert helper.needs_build()
    helper.ensure_helper()
    assert len([cmd for cmd in installation if cmd[0] == "sh"]) == 2


def test_failed_build_preserves_previous_binary(installation, monkeypatch):
    helper.ensure_helper()
    stamp = helper.STAMP.read_text()
    (helper.SOURCE / "snappie-cast.c").write_text("new version")

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="compiler failed")

    monkeypatch.setattr(helper.subprocess, "run", fail)
    monkeypatch.setattr(deps, "missing", lambda keys: [])
    with pytest.raises(RuntimeError, match="compiler failed"):
        helper.ensure_helper()
    assert helper.HELPER.read_bytes() == b"compiled helper"
    assert helper.STAMP.read_text() == stamp


def test_switching_from_wheel_to_checkout_rebuilds(installation):
    helper.ensure_helper()
    helper.BUNDLED.write_bytes(b"wheel version")
    helper.ensure_helper()
    assert not helper.STAMP.exists()
    helper.BUNDLED.unlink()
    assert helper.needs_build()
    helper.ensure_helper()
    assert helper.HELPER.read_bytes() == b"compiled helper"


def test_no_build_tools_required_for_wheel(installation, monkeypatch):
    helper.BUNDLED.write_bytes(b"bundled helper")
    monkeypatch.setattr(deps, "is_missing", lambda dep: True)
    keys = {dep.key for dep in deps.missing()}
    assert not keys.intersection({"compiler", "pkg-config", "wayland"})
    assert "wayland-runtime" in keys
    # Explicit checks can still diagnose a failed source build.
    assert [dep.key for dep in deps.missing(["compiler"])] == ["compiler"]


def test_build_tools_required_for_unbuilt_checkout(installation, monkeypatch):
    monkeypatch.setattr(deps, "is_missing", lambda dep: True)
    assert {"compiler", "pkg-config", "wayland"} <= {dep.key for dep in deps.missing()}
