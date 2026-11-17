import subprocess

import pytest

from snappie import share_plugin


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    monkeypatch.setattr(share_plugin, "PLUGIN_ROOT", root)
    monkeypatch.setattr(share_plugin, "PLUGIN", root / "kf6" / "purpose" / share_plugin.NAME)
    monkeypatch.setattr(share_plugin, "STAMP", tmp_path / "stamp")
    monkeypatch.setattr(share_plugin, "ENV_SCRIPT", tmp_path / "env" / "snappie-plugins.sh")
    monkeypatch.setattr(share_plugin, "DBUS_SERVICE", tmp_path / "dbus" / "io.github.snappie.service")
    monkeypatch.setattr(share_plugin, "system_plugin", lambda: None)
    return tmp_path


def env_after(script, value):
    env = {"PATH": "/usr/bin:/bin"}
    if value is not None:
        env["QT_PLUGIN_PATH"] = value
    r = subprocess.run(["sh", "-c", f'. "{script}"; printf %s "$QT_PLUGIN_PATH"'], env=env,
                       capture_output=True, text=True, check=True)
    return r.stdout


def test_env_script_adds_the_directory_once(home, monkeypatch):
    monkeypatch.setattr(share_plugin, "up_to_date", lambda: True)
    assert share_plugin.ensure("/opt/snappie") == "login"
    root = str(share_plugin.PLUGIN_ROOT)
    script = share_plugin.ENV_SCRIPT
    assert env_after(script, None) == root
    assert env_after(script, "/usr/lib/qt6/plugins") == f"{root}:/usr/lib/qt6/plugins"
    assert env_after(script, f"/a:{root}") == f"/a:{root}"
    monkeypatch.setenv("QT_PLUGIN_PATH", f"/a:{root}/")
    assert share_plugin.on_plugin_path()
    assert share_plugin.ensure("/opt/snappie") == "ready"


def test_dbus_activation_runs_the_service_command(home, monkeypatch):
    monkeypatch.setattr(share_plugin, "up_to_date", lambda: True)
    share_plugin.ensure("/opt/bin/snappie")
    text = share_plugin.DBUS_SERVICE.read_text()
    assert "Name=io.github.snappie\n" in text and "Exec=/opt/bin/snappie service\n" in text
    share_plugin.remove()
    assert not share_plugin.DBUS_SERVICE.exists() and not share_plugin.ENV_SCRIPT.exists()


def test_missing_build_tools_are_reported_not_raised(home, monkeypatch):
    from snappie import deps
    monkeypatch.setattr(deps, "missing", lambda keys=None, optional=True: [deps.BY_KEY["cmake"]])
    assert share_plugin.ensure("/opt/snappie") == "missing"
    assert "CMake" in share_plugin.describe("missing") or "cmake" in share_plugin.describe("missing")
    assert not share_plugin.ENV_SCRIPT.exists()


def test_system_plugin_replaces_the_user_one(home, monkeypatch):
    share_plugin.PLUGIN.parent.mkdir(parents=True)
    share_plugin.PLUGIN.write_bytes(b"old")
    monkeypatch.setattr(share_plugin, "system_plugin", lambda: "/usr/lib/qt6/plugins/kf6/purpose/x.so")
    assert share_plugin.ensure("/opt/snappie") == "system"
    assert not share_plugin.PLUGIN.exists()
