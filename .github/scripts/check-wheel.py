"""Check release contents and helper setup with no build tools on PATH."""

from importlib.metadata import version
from importlib.resources import files
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

assert version("snappie") == os.environ["EXPECTED_VERSION"]
assert platform.machine() == os.environ["EXPECTED_ARCH"]
root = files("snappie")
assert Path(str(root)).is_relative_to(Path(sys.prefix)), root
for asset in ("icons/snappie.svg", "icons/snappie-tray.svg", "purpose/CMakeLists.txt",
              "purpose/snappie_purpose.cpp", "purpose/snappie_purpose.json"):
    assert root.joinpath(asset).is_file(), asset
binary = root / "helper" / "snappie-cast"
header = binary.read_bytes()[:20]
assert header[:4] == b"\x7fELF"
assert header[4:6] == b"\x02\x01"  # ELF64, little endian
assert int.from_bytes(header[18:20], "little") == {"x86_64": 62, "aarch64": 183}[platform.machine()]
with tempfile.TemporaryDirectory(prefix="snappie smoke ") as data:
    env = dict(os.environ, XDG_DATA_HOME=data, XDG_RUNTIME_DIR=data, PATH="", WAYLAND_DISPLAY="missing")
    command = [sys.executable, "-m", "snappie.capture_helper"]
    subprocess.run(command, env=env, check=True)
    installed = Path(data, "snappie", "snappie-cast")
    assert os.access(installed, os.X_OK)
    assert installed.read_bytes() == binary.read_bytes()
    # Exercise the dynamic linker without needing a desktop or capture permission.
    result = subprocess.run([str(installed)], env=env, capture_output=True, text=True)
    assert "usage:" in result.stderr.lower(), result.stderr
    desktop = Path(data, "applications", "snappie-cast.desktop")
    desktop.unlink()
    subprocess.run(command, env=env, check=True)
    assert desktop.is_file()
print("Installed release wheel verified")
