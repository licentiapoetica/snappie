"""Text recognition with the tesseract command-line tool (optional dependency)."""

import os
import shutil
import subprocess
import tempfile
import threading

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter

from . import settings

# tesseract language code -> openSUSE package suffix (tesseract-ocr-traineddata-<name>)
_LANG_PACKAGES = {
    "eng": "english", "deu": "german", "fra": "french", "spa": "spanish", "ita": "italian",
    "por": "portuguese", "nld": "dutch", "pol": "polish", "rus": "russian", "ukr": "ukrainian",
    "ces": "czech", "swe": "swedish", "jpn": "japanese", "chi_sim": "chinese_simplified",
    "chi_tra": "chinese_traditional", "kor": "korean", "osd": "orientation_and_script_detection",
}


class OcrError(Exception):
    pass


def available():
    return shutil.which("tesseract") is not None


def _data_packages(lang):
    return [f"tesseract-ocr-traineddata-{_LANG_PACKAGES.get(code, code)}"
            for code in (lang or settings.get("ocr.lang") or "eng").split("+")]


def install_hint(lang=None):
    pkgs = ["tesseract-ocr"] + _data_packages(lang)
    return "Text recognition needs tesseract: sudo zypper install " + " ".join(pkgs)


def _prepare(img):
    """Flatten onto white and upscale small text: tesseract is most accurate with
    dark text on a light background at ~20-30 px letter height."""
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    # screenshots at 1x have ~10-13 px text; HiDPI shots are big enough already
    scale = 2 if img.devicePixelRatio() < 1.5 and max(img.width(), img.height()) <= 5000 else 1
    if img.height() < 40:
        scale = max(scale, 3)            # single short line, e.g. a label
    out = QImage(img.width() * scale, img.height() * scale, QImage.Format.Format_RGB32)
    out.fill(QColor("white"))
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    img.setDevicePixelRatio(1)
    p.drawImage(out.rect(), img)
    p.end()
    out.setDotsPerMeterX(round(300 / 0.0254))   # stops "Invalid resolution 0 dpi" guessing
    out.setDotsPerMeterY(round(300 / 0.0254))
    return out


def recognize(img, lang=None):
    """Return the text in `img` (QImage). Blocking; raises OcrError."""
    lang = lang or settings.get("ocr.lang") or "eng"
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrError(install_hint(lang))
    if img is None or img.isNull():
        raise OcrError("Nothing to recognize (empty image)")
    with tempfile.TemporaryDirectory(prefix="snappie-ocr-") as tmp:
        src = os.path.join(tmp, "in.png")
        if not _prepare(img).save(src, "PNG"):
            raise OcrError("Can't write the temporary image")
        try:
            r = subprocess.run([exe, src, "stdout", "-l", lang, "--psm", "3"],
                               capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise OcrError("tesseract took too long") from None
    if r.returncode != 0:
        err = r.stderr.strip()
        if "Failed loading language" in err or "Error opening data file" in err:
            raise OcrError(f"tesseract has no data for “{lang}”: sudo zypper install "
                           + " ".join(_data_packages(lang)))
        raise OcrError("tesseract failed: " + (err.splitlines()[-1] if err else f"exit {r.returncode}"))
    lines = [ln.rstrip() for ln in r.stdout.replace("\f", "").splitlines()]
    return "\n".join(lines).strip()


class Ocr(QObject):
    """Runs recognize() in a thread; `finished` (text, error) arrives on the GUI thread."""

    finished = pyqtSignal(str, str)
    _done = pyqtSignal(str, str)
    _running = set()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._done.connect(self._finish, Qt.ConnectionType.QueuedConnection)

    def start(self, img, lang=None):
        Ocr._running.add(self)
        img = img.copy()   # detach from the caller's image before crossing threads
        threading.Thread(target=self._work, args=(img, lang), daemon=True).start()

    def _work(self, img, lang):
        try:
            text, err = recognize(img, lang), ""
        except OcrError as e:
            text, err = "", str(e)
        except Exception as e:  # noqa: BLE001
            text, err = "", f"OCR failed: {e}"
        if not err and not text:
            err = "No text found"
        self._done.emit(text, err)

    def _finish(self, text, err):
        Ocr._running.discard(self)
        self.finished.emit(text, err)
