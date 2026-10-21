"""Text recognition with the tesseract command-line tool (optional dependency)."""

import bisect
import os
import shutil
import statistics
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter

from . import settings

class OcrError(Exception):
    pass


def available():
    return shutil.which("tesseract") is not None


def install_hint(lang=None):
    from . import deps
    return "Text recognition needs tesseract: " + deps.install_hint(
        ["tesseract"], lang or settings.get("ocr.lang") or "eng")


# Pipeline: grayscale + upscale, then tesseract reads the image both as-is and inverted
# (dark-mode panels, colored buttons) with two page layouts. Every word is kept from the
# reading whose polarity matches the pixels under it, the most confident reading wins,
# and lines/columns are rebuilt from the word boxes so aligned columns (terminals, tables)
# stay on one row while separate panels stay apart.

_PSMS = ("4", "6")          # single column of mixed sizes / one uniform block
_MAX_PIXELS = 40e6          # cap on the upscaled image tesseract has to chew through


def _gray(img):
    """QImage -> uint8 luminance array, transparency flattened onto white."""
    rgb = QImage(img.width(), img.height(), QImage.Format.Format_RGB32)
    rgb.fill(QColor("white"))
    p = QPainter(rgb)
    src = QImage(img)
    src.setDevicePixelRatio(1)
    p.drawImage(0, 0, src)
    p.end()
    a = np.frombuffer(rgb.constBits().asstring(rgb.sizeInBytes()), np.uint8)
    a = a.reshape(rgb.height(), rgb.bytesPerLine())[:, :rgb.width() * 4].reshape(rgb.height(), rgb.width(), 4)
    return (a[..., 2] * .299 + a[..., 1] * .587 + a[..., 0] * .114).astype(np.uint8)


def _qgray(g):
    g = np.ascontiguousarray(g)
    return QImage(g.data, g.shape[1], g.shape[0], g.strides[0], QImage.Format.Format_Grayscale8).copy()


def _median3(g):
    """3x3 median: wipes out thin noise (photo grain, scribbles) around large text."""
    p = np.pad(g, 1, mode="edge")
    h, w = g.shape
    stack = np.stack([p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)])
    return np.partition(stack, 4, axis=0)[4]


def _scale(img):
    """Tesseract reads best at ~25-35 px capital height; 1x screen text is ~9-10 px."""
    w, h = img.width(), img.height()
    if img.devicePixelRatio() >= 1.5:
        s = 1.0                         # HiDPI capture: text is already big enough
    elif h < 40:
        s = 3.0                         # a single short line, e.g. a label
    else:
        s = 2.0
    return max(1.0, min(s, (_MAX_PIXELS / max(w * h, 1)) ** .5))


def _tesseract(exe, path, lang, psm):
    env = dict(os.environ, OMP_THREAD_LIMIT="1")    # we already run several in parallel
    return subprocess.run([exe, path, "stdout", "-l", lang, "--psm", psm, "--dpi", "300",
                           "-c", "tessedit_do_invert=0", "-c", "tessedit_create_tsv=1",
                           "-c", "tessedit_create_txt=0"],
                          capture_output=True, text=True, timeout=120, env=env)


def _tsv_words(tsv, dark_text, s):
    """Word boxes from tesseract's TSV, mapped back to source-image pixels."""
    out = []
    for line in tsv.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        x, y, w, h = (int(v) / s for v in f[6:10])
        out.append(_Box(x, y, w, h, text=f[11].strip(), conf=float(f[10]), dark=dark_text))
    return out


def _polarity(g, b):
    """True: dark text on light, False: light on dark, None: unclear, "": nothing there."""
    m = .3 * b.h
    p = g[max(int(b.y - m), 0):int(b.y + b.h + m) + 1, max(int(b.x - m), 0):int(b.x + b.w + m) + 1]
    if p.size == 0:
        return None
    p = p.astype(np.int16)
    med = int(np.median(p))
    t = max(16, (int(p.max()) - int(p.min())) // 4)
    dark, light = int((p < med - t).sum()), int((p > med + t).sum())
    if dark + light < 3:
        return ""
    if dark > 2 * light:
        return True
    if light > 2 * dark:
        return False
    return None


def _read(exe, g, s, lang, tmp, tag):
    """Run every (polarity, layout) combination in parallel; returns (words, errors)."""
    q = _qgray(g)
    if s != 1:
        q = q.scaled(round(q.width() * s), round(q.height() * s), Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    jobs = []
    for dark_text in (True, False):
        if not dark_text:
            q.invertPixels()
        path = os.path.join(tmp, f"{tag}-{'n' if dark_text else 'i'}.png")
        if not q.save(path, "PNG"):
            raise OcrError("Can't write the temporary image")
        jobs += [(path, psm, dark_text) for psm in _PSMS]
    with ThreadPoolExecutor(len(jobs)) as pool:
        results = list(pool.map(lambda j: (j[2], _tesseract(exe, j[0], lang, j[1])), jobs))
    words, errors = [], []
    for dark_text, r in results:
        if r.returncode != 0:
            errors.append(r.stderr.strip())
            continue
        for b in _tsv_words(r.stdout, dark_text, s):
            pol = _polarity(g, b)
            if pol == "" or pol is (not dark_text):
                continue                # wrong polarity for this spot: a misread of the background
            if b.conf < 30 and not any(c.isalnum() for c in b.text):
                continue                # stray specks read as punctuation
            words.append(b)
    return words, errors


def recognize(img, lang=None):
    """Return the text in `img` (QImage). Blocking; raises OcrError."""
    lang = lang or settings.get("ocr.lang") or "eng"
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrError(install_hint(lang))
    if img is None or img.isNull():
        raise OcrError("Nothing to recognize (empty image)")
    g = _gray(img)
    s = _scale(img)
    try:
        with tempfile.TemporaryDirectory(prefix="snappie-ocr-") as tmp:
            words, errors = _read(exe, g, s, lang, tmp, "plain")
            # big text that still reads poorly is usually sitting on a busy photo: denoise and retry
            big = [w for w in words if w.h >= 16]
            if big and sum(w.conf for w in big) / len(big) < 80:
                try:
                    more, _ = _read(exe, _median3(g), s, lang, tmp, "smooth")
                    words += [w for w in more if w.h >= 16 and w.conf >= 60]
                except subprocess.TimeoutExpired:
                    pass                # keep what the first round found
    except subprocess.TimeoutExpired:
        raise OcrError("tesseract took too long") from None
    if errors and not words:
        err = errors[0]
        if "Failed loading language" in err or "Error opening data file" in err:
            from . import deps
            raise OcrError(f"tesseract has no data for “{lang}”: " + deps.tesseract_data_hint(lang))
        raise OcrError("tesseract failed: " + (err.splitlines()[-1] if err else "unknown error"))
    return _layout(words)


# ------------------------------------------------------------------ layout

class _Box:
    __slots__ = ("x", "y", "w", "h", "lh", "text", "conf", "dark", "parts")

    def __init__(self, x, y, w, h, text="", conf=0.0, dark=True, parts=None, lh=None):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.lh = h if lh is None else lh   # typical letter height, ignoring stray tall/short glyphs
        self.text, self.conf, self.dark, self.parts = text, conf, dark, parts

    @property
    def right(self):
        return self.x + self.w

    @property
    def bottom(self):
        return self.y + self.h

    @property
    def words(self):
        return [self] if self.parts is None else [w for p in self.parts for w in p.words]

    @classmethod
    def around(cls, parts, lh=None):
        x, y = min(p.x for p in parts), min(p.y for p in parts)
        r, b = max(p.right for p in parts), max(p.bottom for p in parts)
        return cls(x, y, r - x, b - y, parts=parts,
                   lh=statistics.median(p.lh for p in parts) if lh is None else lh)


def _hov(a, b):
    return min(a.right, b.right) - max(a.x, b.x)


def _vov(a, b):
    return min(a.bottom, b.bottom) - max(a.y, b.y)


class _Groups:
    """Union-find over list indexes."""

    def __init__(self, n):
        self.p = list(range(n))

    def find(self, i):
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def join(self, a, b):
        self.p[self.find(a)] = self.find(b)

    def collect(self, items):
        out = {}
        for i, it in enumerate(items):
            out.setdefault(self.find(i), []).append(it)
        return list(out.values())


def _best(words):
    """Several readings of the same spot: keep the most confident, drop what overlaps it."""
    cell = 64
    grid, kept = {}, []

    def cells(w):
        return [(cx, cy) for cx in range(int(w.x // cell), int(w.right // cell) + 1)
                for cy in range(int(w.y // cell), int(w.bottom // cell) + 1)]

    for w in sorted(words, key=lambda w: -w.conf * len(w.text) ** .5):
        area = w.w * w.h
        near = {id(k): k for c in cells(w) for k in grid.get(c, ())}.values()
        if any(_hov(w, k) > 0 and _vov(w, k) > 0 and
               _hov(w, k) * _vov(w, k) > .3 * min(area, k.w * k.h) for k in near):
            continue
        kept.append(w)
        for c in cells(w):
            grid.setdefault(c, []).append(w)
    return kept


def _chain(items, ok, same_size=False):
    """Link each item to its nearest right-hand neighbour on the same text line when ok()."""
    items = sorted(items, key=lambda i: i.y + i.h / 2)
    mids = [i.y + i.h / 2 for i in items]
    reach = max(i.h for i in items)
    groups = _Groups(len(items))
    for k, a in enumerate(items):
        best = None
        for j in range(bisect.bisect_left(mids, mids[k] - reach), bisect.bisect_right(mids, mids[k] + reach)):
            b = items[j]
            if b is a or b.x < a.x or _vov(a, b) < .7 * min(a.h, b.h):
                continue
            if same_size and max(a.lh, b.lh) > 2 * min(a.lh, b.lh):
                continue                # a heading next to body text
            gap = b.x - a.right
            if gap < -.5 * min(a.lh, b.lh):
                continue
            if best is None or gap < best[0]:
                best = (gap, j)
        if best and ok(a, items[best[1]], best[0]):
            groups.join(k, best[1])
    return [_Box.around(sorted(g, key=lambda i: i.x)) for g in groups.collect(items)]


def _row_gap_ok(a, b, gap):
    h = max(min(a.lh, b.lh), .5 * max(a.lh, b.lh))      # a lone "=" doesn't set the scale
    # close by, or both short like table cells / terminal columns (long lines = separate columns)
    return gap <= 2.5 * h or (gap <= 25 * h and a.w <= 14 * h and b.w <= 14 * h)


def _layout(words):
    words = _best(words)
    if not words:
        return ""
    # words -> phrases (word spacing) -> rows (also cells further apart)
    phrases = _chain(words, lambda a, b, gap: gap <= 1.2 * max(a.lh, b.lh))
    rows = _chain(phrases, _row_gap_ok, same_size=True)
    # rows stacked in the same column form a block
    rows.sort(key=lambda r: r.y)
    groups = _Groups(len(rows))
    for i, a in enumerate(rows):
        for j in range(i + 1, len(rows)):
            b = rows[j]
            lh = max(a.lh, b.lh)
            if b.y - a.bottom > 4 * lh:
                break
            if (b.y - a.bottom <= 1.5 * lh and _hov(a, b) > 0
                    and lh <= 1.8 * min(a.lh, b.lh)):
                groups.join(i, j)
    blocks = [_Box.around(sorted(g, key=lambda r: r.y)) for g in groups.collect(rows)]
    return _join_blocks(_reading_order(blocks))


def _reading_order(blocks):
    """Top to bottom; finish a column before moving to the one beside it."""
    # blockers[i]: blocks above block i in an overlapping column, still to be emitted
    above = [[j for j, o in enumerate(blocks) if o is not b and o.bottom <= b.y + 2 and _hov(o, b) > 0]
             for b in blocks]
    below = [[] for _ in blocks]
    for i, js in enumerate(above):
        for j in js:
            below[j].append(i)
    blockers = [len(js) for js in above]
    todo, out, prev = set(range(len(blocks))), [], None
    while todo:
        free = [i for i in todo if not blockers[i]] or list(todo)
        nxt = None
        if prev is not None:
            under = [i for i in free if _hov(prev, blocks[i]) > 0 and blocks[i].y >= prev.bottom - 2]
            if under:
                nxt = min(under, key=lambda i: blocks[i].y)
        if nxt is None:
            top = min((blocks[i] for i in free), key=lambda b: b.y)
            nxt = min((i for i in free if blocks[i].y < top.bottom), key=lambda i: blocks[i].x)
        todo.discard(nxt)
        for i in below[nxt]:
            blockers[i] -= 1
        prev = blocks[nxt]
        out.append(prev)
    return out


def _join_blocks(blocks):
    text, prev = "", None
    for b in blocks:
        if prev is not None:
            close = _hov(prev, b) > 0 and 0 <= b.y - prev.bottom <= 2.5 * max(prev.lh, b.lh)
            text += "\n" if close else "\n\n"
        text += _block_text(b)
        prev = b
    return text


def _block_text(block):
    lines = []
    for row in block.parts:
        line, end = "", None
        for seg in row.parts:
            if end is not None:
                line += "\t" if seg.x - end > 4 * row.lh else " "   # tabs paste into spreadsheets
            line += " ".join(w.text for w in seg.words)
            end = seg.right
        lines.append((row.x, line))
    return "\n".join(_indent(block, lines))


def _indent(block, lines):
    """Keep code indentation: when line starts sit on a regular grid, turn it into spaces."""
    if len(lines) < 3:
        return [t for _, t in lines]
    widths = [w.w / len(w.text) for w in block.words if len(w.text) >= 3]
    if not widths:
        return [t for _, t in lines]
    cw = statistics.median(widths)
    x0 = min(x for x, _ in lines)
    steps = sorted(x - x0 for x, _ in lines if x - x0 > cw)
    if not steps:
        return [t for _, t in lines]
    unit = steps[0]
    if unit < 1.5 * cw or any(abs(d / unit - round(d / unit)) > .2 for d in steps):
        return [t for _, t in lines]
    spaces = max(1, min(8, round(unit / cw)))
    return [" " * (spaces * round((x - x0) / unit)) + t for x, t in lines]


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
