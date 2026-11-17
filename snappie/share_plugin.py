"""KDE's Share menu (Spectacle, Gwenview, Okular, Dolphin → Share): "Upload with Snappie".

The Share menu comes from KF6 Purpose, whose plugins are C++ libraries loaded by the app that
shows the menu.  KF6's ABI differs between distros, so the plugin (snappie/purpose/) is built
on this machine with CMake when the KF6 Purpose headers are there; distro packages can build
and install it system-wide with the same CMakeLists.txt instead.

The plugin only calls Snappie's D-Bus method `upload` and waits for the links, so the app can
show / copy the link like with its own upload plugins.  A D-Bus service file starts Snappie
for that call when it isn't running (`snappie service`: a one-shot that quits when done).

Qt loads plugins only from QT_PLUGIN_PATH and its own plugin directory, so the per-user
plugin in ~/.local/lib/qt6/plugins needs that directory on QT_PLUGIN_PATH: a Plasma env
script adds it at login.  The menu entry appears after logging in again (once).

Standard library only: `snappie deps` uses this too.
"""

import glob
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SOURCE = Path(__file__).parent / "purpose"
NAME = "snappie_purpose.so"
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
PLUGIN_ROOT = Path.home() / ".local/lib/qt6/plugins"
PLUGIN = PLUGIN_ROOT / "kf6" / "purpose" / NAME
STAMP = DATA / "snappie" / "share-plugin.source-hash"
ENV_SCRIPT = CONFIG / "plasma-workspace" / "env" / "snappie-plugins.sh"
DBUS_SERVICE = DATA / "dbus-1" / "services" / "io.github.snappie.service"

def env_text():
    root = PLUGIN_ROOT
    return f"""# Snappie: lets KDE apps load its Share menu plugin from {root}
case ":$QT_PLUGIN_PATH:" in
    *":{root}:"*) ;;
    *) export QT_PLUGIN_PATH="{root}${{QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}}" ;;
esac
"""


def system_plugin():
    """The plugin installed by a distro package, if any."""
    for d in ["/usr/lib", "/usr/lib64", *glob.glob("/usr/lib/*-linux-gnu*")]:
        p = Path(d) / "qt6" / "plugins" / "kf6" / "purpose" / NAME
        if p.is_file():
            return p
    return None


def _source_hash():
    digest = hashlib.sha256()
    for path in sorted(SOURCE.iterdir()):
        if path.suffix in (".cpp", ".json", ".txt"):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def up_to_date():
    return PLUGIN.is_file() and STAMP.is_file() and STAMP.read_text() == _source_hash()


def needs_build():
    return system_plugin() is None and not up_to_date()


def on_plugin_path(env=None):
    """Whether apps of this session look in PLUGIN_ROOT (else: log in again)."""
    paths = (env if env is not None else os.environ).get("QT_PLUGIN_PATH", "").split(":")
    return any(p and Path(p).expanduser().resolve() == PLUGIN_ROOT.resolve() for p in paths)


def build(output):
    """Compile into `output`; raises RuntimeError with the reason."""
    if not shutil.which("cmake"):
        raise RuntimeError("cmake is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        for args in (["cmake", "-S", str(SOURCE), "-B", tmp, "-DCMAKE_BUILD_TYPE=Release"],
                     ["cmake", "--build", tmp]):
            r = subprocess.run(args, capture_output=True, text=True)
            if r.returncode != 0:
                lines = (r.stderr.strip() or r.stdout.strip()).splitlines()
                raise RuntimeError("building the Share menu plugin failed: "
                                   + (lines[-1] if lines else f"{args[1]} exited with {r.returncode}"))
        os.makedirs(os.path.dirname(output), exist_ok=True)
        tmp_out = f"{output}.new"
        shutil.copyfile(os.path.join(tmp, NAME), tmp_out)
        os.chmod(tmp_out, 0o755)
        os.replace(tmp_out, output)    # apps that loaded the old one keep their copy


def install_dbus_service(exe):
    text = f"[D-BUS Service]\nName=io.github.snappie\nExec={exe} service\n"
    if DBUS_SERVICE.is_file() and DBUS_SERVICE.read_text() == text:
        return
    DBUS_SERVICE.parent.mkdir(parents=True, exist_ok=True)
    DBUS_SERVICE.write_text(text)
    _reload_bus()


def _reload_bus():
    # dbus-broker (most distros now) only notices added / removed service files when told to
    try:
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                        "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ReloadConfig"],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def ensure(exe):
    """Build / refresh the plugin if needed and possible, and set up what it needs.
    Returns a status: "system", "ready", "login" (works after logging in again), or
    "missing" / "failed: …" when it can't be built."""
    install_dbus_service(exe)
    if system_plugin():
        _remove_user_plugin()
        return "system"
    if not up_to_date():
        from . import deps
        if deps.missing(list(deps.SHARE_PLUGIN_DEPS)):
            return "missing"
        try:
            build(str(PLUGIN))
        except (RuntimeError, OSError) as e:
            return f"failed: {e}"
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(_source_hash())
    text = env_text()
    if not ENV_SCRIPT.is_file() or ENV_SCRIPT.read_text() != text:
        ENV_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
        ENV_SCRIPT.write_text(text)
    return "ready" if on_plugin_path() else "login"


def _remove_user_plugin():
    for p in (PLUGIN, STAMP, ENV_SCRIPT):
        p.unlink(missing_ok=True)


def remove():
    """Turn the Share menu entry off (a system-wide plugin stays: the package owns it)."""
    _remove_user_plugin()
    if DBUS_SERVICE.is_file():
        DBUS_SERVICE.unlink()
        _reload_bus()


def status():
    """Like ensure() without building anything ("pending": it will be built)."""
    if system_plugin():
        return "system"
    if up_to_date():
        return "ready" if on_plugin_path() else "login"
    from . import deps
    return "missing" if deps.missing(list(deps.SHARE_PLUGIN_DEPS)) else "pending"


def describe(status):
    """A sentence for the settings dialog and install.sh."""
    if status in ("system", "ready"):
        return "“Upload with Snappie” is in the Share menus."
    if status == "pending":
        return "“Upload with Snappie” is added to the Share menus when you press OK (takes a few seconds)."
    if status == "login":
        return "“Upload with Snappie” shows up in the Share menus after you log in again."
    if status == "missing":
        from . import deps
        return ("The Share menu entry is built on this computer and needs CMake, a C++ compiler and the "
                "KF6 Purpose headers: " + deps.install_hint(deps.missing(list(deps.SHARE_PLUGIN_DEPS))))
    return status[0].upper() + status[1:] if status else ""


if __name__ == "__main__":
    import sys
    from .hotkeys import launcher
    print(describe(ensure(sys.argv[1] if len(sys.argv) > 1 else launcher())))
