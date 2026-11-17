"""Install and authorize the bundled helper; compile only for source checkouts.

Keep the executable at a stable path for KWin's desktop-file authorization.
This module uses only the standard library so dependency checks can use it too.
"""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

SOURCE = Path(__file__).parent / "helper"
BUNDLED = SOURCE / "snappie-cast"
DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
HELPER = DATA / "snappie" / "snappie-cast"
DESKTOP = DATA / "applications" / "snappie-cast.desktop"
STAMP = HELPER.with_suffix(".source-hash")


def _source_hash():
    digest = hashlib.sha256()
    for path in sorted(SOURCE.iterdir()):
        if path.suffix in (".c", ".xml", ".sh"):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def needs_build():
    """Whether this installation needs a compiler to prepare the helper."""
    if BUNDLED.is_file():
        return False
    return not (HELPER.is_file() and STAMP.is_file() and STAMP.read_text() == _source_hash())


def _write(path, content, mode=0o644):
    """Replace atomically, including when the helper is running or starts race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _register():
    # Desktop Entry strings escape backslashes separately from Exec's quoting.
    exe = str(HELPER).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    exe = exe.replace("\\", "\\\\").replace("%", "%%")
    text = ("[Desktop Entry]\nType=Application\nName=snappie capture helper\n"
            f'Exec="{exe}"\nNoDisplay=true\n'
            "X-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1,org_kde_kwin_fake_input\n")
    if DESKTOP.is_file() and DESKTOP.read_text() == text:
        return
    _write(DESKTOP, text.encode())
    try:
        subprocess.run(["kbuildsycoca6"], capture_output=True, check=False)
    except FileNotFoundError:
        pass


def ensure_helper():
    """Refresh the stable executable and repair its KWin registration when needed."""
    try:
        if BUNDLED.is_file():
            content = BUNDLED.read_bytes()
            if not HELPER.is_file() or HELPER.read_bytes() != content:
                _write(HELPER, content, 0o755)
            STAMP.unlink(missing_ok=True)
        elif needs_build():
            source_hash = _source_hash()
            with tempfile.TemporaryDirectory() as build:
                output = Path(build) / "snappie-cast"
                subprocess.run(["sh", str(SOURCE / "build.sh"), str(output)],
                               capture_output=True, text=True, check=True)
                _write(HELPER, output.read_bytes(), 0o755)
            _write(STAMP, source_hash.encode())
        HELPER.chmod(0o755)
        _register()
    except subprocess.CalledProcessError as e:
        from . import deps
        gone = deps.missing(["compiler", "pkg-config", "wayland"])
        need = f" ({deps.install_hint(gone)})" if gone else ""
        raise RuntimeError(f"building the capture helper failed{need}:\n{e.stderr}") from e
    except OSError as e:
        raise RuntimeError(f"preparing the capture helper failed: {e}") from e


if __name__ == "__main__":
    ensure_helper()
    print(HELPER)
