"""Check the installed Arch packages without starting a real desktop session."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from snappie import capture_helper, share_plugin

metadata = subprocess.check_output(["bsdtar", "-xOf", sys.argv[1], ".PKGINFO"], text=True)
dependencies = [line.removeprefix("depend = ") for line in metadata.splitlines() if line.startswith("depend = ")]
python_dependencies = [dep for dep in dependencies
                       if dep == "python" or dep.startswith(("python<", "python>", "python="))]
assert python_dependencies == ["python"], python_dependencies

assert capture_helper.BUNDLED.read_bytes().startswith(b"\x7fELF")
plugin = share_plugin.system_plugin()
assert plugin is not None, "KDE Share plugin is not installed"
linked = subprocess.run(["ldd", str(plugin)], capture_output=True, text=True, check=True)
assert "not found" not in linked.stdout, linked.stdout
for path in ("/usr/share/applications/snappie.desktop",
             "/usr/share/icons/hicolor/scalable/apps/snappie.svg",
             "/usr/share/dbus-1/services/io.github.snappie.service"):
    assert Path(path).is_file(), path
with tempfile.TemporaryDirectory(prefix="snappie-arch-check-") as data:
    env = dict(os.environ, XDG_DATA_HOME=data, XDG_STATE_HOME=data, XDG_CONFIG_HOME=data, PATH="")
    subprocess.run(["/usr/bin/snappie", "--help"], env=env, check=True)
    subprocess.run([sys.executable, "-m", "snappie.capture_helper"], env=env, check=True)
    installed = Path(data, "snappie", "snappie-cast")
    assert os.access(installed, os.X_OK)
    assert installed.read_bytes() == capture_helper.BUNDLED.read_bytes()
print("Installed Arch packages verified")
