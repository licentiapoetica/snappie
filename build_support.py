"""Build the standalone Wayland helper into Linux wheels, without touching user data."""

import subprocess
import sys
from pathlib import Path

from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py
from setuptools.errors import PlatformError


class BuildPy(build_py):
    def run(self):
        if sys.platform != "linux":
            raise PlatformError("Snappie requires Linux and KDE Plasma on Wayland")
        super().run()
        output = Path(self.build_lib).resolve() / "snappie" / "helper" / "snappie-cast"
        script = Path(__file__).parent / "snappie" / "helper" / "build.sh"
        subprocess.run(["sh", str(script), str(output)], check=True)


class LinuxWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self):
        # The helper is native code, but has no dependency on Python's C ABI.
        _, _, platform = super().get_tag()
        return "py3", "none", platform
