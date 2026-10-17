"""Scrolling capture: scroll a window step by step and glue the frames into one long image.

The window's own KWin stream stays open the whole time (so other windows, like
our "Stop" bar, don't get in the way), and KWin's fake input moves the pointer
over the area and turns the wheel.  Without fake input the user scrolls and we
stitch whatever comes in.

The stitching itself (`Stitcher`) is plain numpy, so it can be tested offline.
"""

import select
import subprocess
import threading
import time

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

TOL = 0.1           # share of overlapping rows allowed to differ (hover effects, a blinking caret, GIFs)
MIN_INFO = 3.0      # matched rows must carry at least this much detail (see _weights)
STATIC_COLS = 0.05  # columns changing in fewer rows than this are fixed sidebars: ignored for matching


def row_hashes(a):
    """One hash per row; equal rows give equal hashes (within this process)."""
    a = np.ascontiguousarray(a)
    return np.fromiter((hash(r.tobytes()) for r in a), np.int64, len(a))


def _run(mask, from_end=False):
    """Length of the leading (or trailing) run of True values."""
    m = mask[::-1] if from_end else mask
    off = np.flatnonzero(~m)
    return len(m) if off.size == 0 else int(off[0])


def _flat(a):
    """Rows of a single colour (checked on every 4th pixel, plenty for telling text from blank)."""
    a = a[:, ::4]
    return (a == a[:, :1]).all(axis=(1, 2))


def _weights(a, hashes, lo, hi):
    """How much a matching row says about the offset: 0 for flat rows (blank lines match
    anywhere), 1/n for a row that occurs n times between lo and hi."""
    flat = _flat(a)
    _, inv, counts = np.unique(hashes[lo:hi], return_inverse=True, return_counts=True)
    w = np.zeros(len(a))
    w[lo:hi] = 1.0 / counts[inv]
    w[flat] = 0.0
    return w


class Stitcher:
    """Glues viewport frames of a page that scrolls down into one image.

    Each new frame is compared with the previous one: rows that are identical at
    the same place at the top and bottom are sticky header / footer and don't
    take part in matching; neither do columns that barely change (sticky
    sidebars) or the `edge` columns at the right (scrollbars).  The vertical
    offset is found by matching row hashes (exact, so fast and unambiguous on
    rendered text); if no offset matches exactly enough, the smallest
    difference over a coarse grey version of the frames is used.

    The canvas always ends with the latest frame's footer, so the header appears
    once at the top (from the first frame) and the footer once at the bottom.
    """

    def __init__(self, first, edge=0):
        self.prev = first
        self.H, self.W = first.shape[:2]
        self.edge = min(edge, self.W // 4)
        self.blocks = [first]
        self.height = self.H
        self.a = 0               # the canvas ends with rows a..H of self.prev
        self.crop_right = 0      # right-edge columns that don't scroll with the page (scrollbar)
        self.header = self.footer = 0   # sticky bar heights (detailed rows only)
        self._h = self._f = None

    def add(self, frame, expected=None):
        """Add the next frame. Returns how far the content moved in px (0 = not at all),
        or None when it couldn't be matched (the frame is ignored). `expected`: the
        offset we roughly expect, to pick between equally good matches (repeating content)."""
        if frame.shape != self.prev.shape:
            raise ValueError("frame size changed")
        m = self.match(self.prev, frame, expected)
        if m is None:
            return None
        d, h, f, hm, fm = m
        if d <= 0:
            return 0
        # The bars are between hm..h and fm..f: unchanged rows may also be blank page that is
        # blank in both frames.  The smallest unchanged run seen so far is the best guess.
        self._h = h if self._h is None else min(self._h, h)
        self._f = f if self._f is None else min(self._f, f)
        self.header, self.footer = hm, fm
        H = self.H
        # Cut the previous frame's footer and append the new frame from cut + d rows above
        # its bottom.  Blank rows counted into the cut don't hurt (they moved too, just
        # invisibly), as long as the appended rows start below the header.
        cut = max(fm, min(f, self._f))
        cut = min(cut, H - max(hm, self._h) - d, H - self.a)   # rows before a are in the canvas
        cut = max(cut, 0)
        self._trim(cut)
        self.blocks.append(frame[H - cut - d:])
        self.height += d
        self.a = H - cut - d
        self.prev = frame
        return d

    def _trim(self, n):
        while n > 0:
            last = self.blocks[-1]
            if len(last) <= n:
                self.blocks.pop()
                n -= len(last)
            else:
                self.blocks[-1] = last[:len(last) - n]
                n = 0

    def image(self):
        out = np.concatenate(self.blocks) if len(self.blocks) > 1 else self.blocks[0]
        if self.crop_right:
            out = out[:, :self.W - self.crop_right]
        return np.ascontiguousarray(out)

    # -- matching --

    def match(self, P, N, expected=None):
        """(offset, h, f, hm, fm), or None when nothing fits. Offset 0: nothing scrolled.
        h, f: rows unchanged at the top / bottom; hm, fm: the same up to the last detailed
        row (the sticky bars are somewhere in between)."""
        H, W = self.H, self.W
        ce = W - self.edge
        hp, hn = row_hashes(P[:, :ce]), row_hashes(N[:, :ce])
        same = hp == hn
        if same.all():
            return 0, 0, 0, 0, 0
        h, f = _run(same), _run(same, from_end=True)   # unchanged rows at the top / bottom
        if H - h - f < max(12, H // 10):
            return 0, h, f, h, f     # a small change (hover, caret), no scrolling

        # Unchanged rows are sticky bars, or blank page that happens to be blank in both
        # frames.  Only up to the last detailed row counts as a bar for the matching range
        # (else a blank stretch would limit how far we can see the page move); the flat
        # rest takes no part in the comparison.
        flat = _flat(N[:, :ce])
        hm = int(np.flatnonzero(~flat[:h])[-1]) + 1 if (~flat[:h]).any() else 0
        fm = int(np.flatnonzero(~flat[H - f:][::-1])[-1]) + 1 if (~flat[H - f:]).any() else 0
        lo, hi = hm, H - fm
        Hm = hi - lo
        care = np.ones(H, bool)
        care[hm:h] = False
        care[H - f:H - fm] = False

        diff = (P[h:H - f, :ce] != N[h:H - f, :ce]).any(axis=2).mean(axis=0)
        cols = np.flatnonzero(diff >= STATIC_COLS)
        if cols.size == 0:
            return 0, h, f, h, f
        if cols.size == ce:      # usually: no fixed columns, the hashes above will do
            Pc, Nc = P[:, :ce], N[:, :ce]
        else:
            Pc, Nc = P[:, cols], N[:, cols]
            hp, hn = row_hashes(Pc), row_hashes(Nc)
        w = _weights(Pc, hp, lo, hi)

        min_ov = max(8, Hm // 10)
        best, ambiguous = [], []
        for d in range(1, Hm - min_ov + 1):
            L = Hm - d
            c = care[lo:lo + L] & care[lo + d:lo + d + L]
            n = c.sum()
            m = (hn[lo:lo + L] == hp[lo + d:lo + d + L]) & c
            if n and m.sum() < (1 - TOL) * n:
                continue
            if n and w[lo + d:lo + d + L][m].sum() >= MIN_INFO:
                best.append((m.sum() / n, d))
            else:
                ambiguous.append(d)
        d = None
        if best:
            top = max(b[0] for b in best)   # fewest differing rows wins; ties: repeating content
            ds = [b[1] for b in best if b[0] >= top - 1e-9]
            d = min(ds, key=lambda v: abs(v - expected)) if expected else ds[0]
        elif ambiguous:
            # only flat rows overlap (a big blank area), so any of these fits: take the
            # expected offset if we have one (else the smallest, it's as good a guess as any)
            d = min(ambiguous, key=lambda v: abs(v - expected)) if expected else ambiguous[0]
        if d is None:
            d = self._coarse(Pc, Nc, lo, hi, min_ov)
        if d is None:
            return None
        if d:
            self._edge_check(P, N, d, lo, hi, care)
        return d, h, f, hm, fm

    def _coarse(self, Pc, Nc, lo, hi, min_ov):
        """Fallback when rows don't match exactly (e.g. text re-rendered at a fractional
        offset): smallest mean difference of a coarse grey version."""
        def coarse(a):
            g = a[lo:hi].astype(np.float32).mean(axis=2)
            bw = max(1, g.shape[1] // 64)
            n = g.shape[1] // bw
            return g[:, :n * bw].reshape(len(g), n, bw).mean(axis=2)
        cp, cn = coarse(Pc), coarse(Nc)
        Hm = hi - lo

        def cost(d):   # ignore the worst fifth of the rows (hover effects, animations)
            rows = np.abs(cn[:Hm - d] - cp[d:]).mean(axis=1)
            k = max(1, len(rows) * 4 // 5)
            return np.partition(rows, k - 1)[:k].mean()
        costs = np.array([cost(d) for d in range(Hm - min_ov + 1)])
        d = int(np.argmin(costs[1:])) + 1
        if costs[0] <= costs[d]:
            return 0        # it changed, but not by scrolling
        if costs[d] < 4.0 and costs[d] < 0.3 * np.median(costs[1:]) and costs[d] < 0.5 * costs[0]:
            return d
        return None

    def _edge_check(self, P, N, d, lo, hi, care):
        """Right-edge columns that differ where the page itself matched: a scrollbar.
        They're cut from the result, since every piece would show the thumb elsewhere."""
        if not self.edge:
            return
        L = hi - lo - d
        c = care[lo:lo + L] & care[lo + d:lo + d + L]
        a = N[lo:lo + L, self.W - self.edge:][c]
        b = P[lo + d:lo + d + L, self.W - self.edge:][c]
        bad = np.flatnonzero((a != b).any(axis=(0, 2)))
        if bad.size:
            self.crop_right = max(self.crop_right, self.edge - int(bad[0]))


class _Resized(Exception):
    pass


class FakeInput:
    """Pointer moves and wheel steps through KWin's fake input (`snappie-cast input`)."""

    def __init__(self):
        from .cast import HELPER
        self.proc = subprocess.Popen([HELPER, "input"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        line = ""
        if select.select([self.proc.stdout], [], [], 3)[0]:
            line = self.proc.stdout.readline().strip()
        if line != "ready":
            self.close()
            raise RuntimeError(line.partition(" ")[2] or "fake input is not available")

    def _send(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def move(self, x, y):
        self._send(f"move {x:.2f} {y:.2f}")

    def scroll(self, notches):
        """+ = down; one notch is one wheel click."""
        self._send(f"scroll {notches:.3f}")

    def close(self):
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.stdin.close()
                proc.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()


class ScrollCapture(QObject):
    """Scrolls sel.win under sel.rect and stitches the frames (see the module docstring).

    Runs on the GUI main loop (timers), never blocks it for long.  `manual` is set
    by start(): True when fake input isn't available and the user has to scroll.
    finished(None, "") means stopped before anything was captured.
    """

    progress = pyqtSignal(int)             # stitched height in px so far
    finished = pyqtSignal(object, str)     # (QImage | None, error text)

    MIN_WAIT, QUIET, SETTLE_TIMEOUT = 0.25, 0.15, 3.0   # seconds, see _settled

    def __init__(self, kwin, sel):
        super().__init__()
        from . import settings
        self.kwin, self.sel, self.win = kwin, sel, sel.win
        self.from_top = settings.get("scroll.from_top")
        self.max_height = int(settings.get("scroll.max_height") or 32000)
        self.step = min(max(float(settings.get("scroll.step") or 0.6), 0.2), 0.85)
        self.manual = False
        self.stitcher = None
        self.input = None
        self.cast = self.pipeline = None
        self.scale = 1.0           # stream px per logical px
        self.edge = 24             # right-edge strip that may hold a scrollbar (stream px)
        self._lock = threading.Lock()
        self._frame = None         # latest cropped frame (H, W, 3)
        self._seq = 0
        self._changed_at = 0.0     # monotonic time of the last frame that differed from its predecessor
        self._sig = None
        self._crop = None          # (size, (px, py, pw, ph))
        self._error = None
        self._done = False
        self._gen = self._cond = None
        self._job = None           # stitching thread
        self.stop_on_mouse = True  # moving the mouse finishes (auto mode); uses kwin.track_cursor
        self._tracking = self._user_moved = False
        self._prev_on_cursor = None
        self._aimed_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)

    # -- public --

    def start(self):
        from .cast import CastStream, POINTER_HIDDEN, ensure_helper, window_args
        if not self.win:
            QTimer.singleShot(0, lambda: self._finish("Scrolling capture needs a window"))
            return
        try:
            ensure_helper()
            try:
                self.input = FakeInput()
            except (OSError, RuntimeError) as e:
                print("snappie: no fake input, scroll yourself:", e)
                self.manual = True
            self.cast = CastStream(window_args(self.win["id"], POINTER_HIDDEN),
                                   self._on_node, self._on_closed, self._on_error)
        except (OSError, RuntimeError) as e:
            QTimer.singleShot(0, lambda: self._finish(str(e)))
            return
        if not self.manual and self.stop_on_mouse:
            self._prev_on_cursor = self.kwin.on_cursor
            self.kwin.on_cursor = self._on_cursor
            self.kwin.track_cursor()
            self._tracking = True
        self._gen = self._manual() if self.manual else self._auto()
        self._timer.start()

    def stop(self):
        """Finish with what was captured so far."""
        self._finish()

    # -- stream --

    def _on_node(self, node):
        from .cast import Gst
        try:
            self.pipeline = Gst.parse_launch(
                f"pipewiresrc path={node} always-copy=true ! capsfilter caps=video/x-raw ! videoconvert"
                " ! video/x-raw,format=RGBx ! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false")
            self.pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
            self.pipeline.set_state(Gst.State.PLAYING)
        except Exception as e:  # GLib.Error from parse_launch
            self._error = f"Could not open the window stream: {e}"

    def _on_closed(self):
        self._error = self._error or "closed"

    def _on_error(self, msg):
        self._error = msg or "capture failed"

    def _crop_for(self, W, H):
        """sel.rect (global logical) -> stream pixels; window streams match either the
        frame or the buffer geometry (pick by aspect ratio, like Recorder._apply_crop)."""
        refs = [self.win["frame"], self.win["buffer"]]
        rx, ry, rw, rh = min(refs, key=lambda r: abs(W / r[2] - H / r[3]))
        r = self.sel.rect
        sx, sy = W / rw, H / rh
        px, py = max(0, round((r.x() - rx) * sx)), max(0, round((r.y() - ry) * sy))
        pw, ph = min(round(r.width() * sx), W - px), min(round(r.height() * sy), H - py)
        self.scale = sx
        self.edge = round(24 * sx)
        return px, py, max(pw, 1), max(ph, 1)

    # streaming thread
    def _on_sample(self, sink):
        from .cast import Gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        s = sample.get_caps().get_structure(0)
        W, H = s.get_value("width"), s.get_value("height")
        if not self._crop or self._crop[0] != (W, H):
            self._crop = ((W, H), self._crop_for(W, H))
        px, py, pw, ph = self._crop[1]
        buf = sample.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            stride = len(m.data) // H
            full = np.ndarray((H, W, 4), np.uint8, m.data, strides=(stride, 4, 1))
            frame = full[py:py + ph, px:px + pw, :3].copy()
        finally:
            buf.unmap(m)
        # "settled" = no frame that differs (more than a few rows) from the one before it;
        # the scrollbar strip doesn't count (overlay scrollbars fade in and out)
        sig = frame[::4, :max(4, pw - self.edge):4, 1]
        now = time.monotonic()
        changed = (self._sig is None or sig.shape != self._sig.shape
                   or (sig != self._sig).any(axis=1).mean() > 0.02)
        with self._lock:
            self._frame, self._seq, self._sig = frame, self._seq + 1, sig
            if changed:
                self._changed_at = now
        return Gst.FlowReturn.OK

    def _latest(self):
        with self._lock:
            return self._frame, self._seq

    # -- the loop: generators yield wait conditions, _tick resumes them --

    def _tick(self):
        if self._done:
            return
        if self._user_moved:
            self._finish()
            return
        if self._error:
            err = self._error
            # the window went away: keep what we have
            self._finish("" if err == "closed" and self.stitcher else
                         "The window was closed" if err == "closed" else err)
            return
        try:
            while self._cond is None or self._cond():
                self._cond = next(self._gen)
        except (StopIteration, _Resized):
            self._finish()     # done, or the window changed size: keep what we have
        except Exception as e:  # noqa: BLE001 - report instead of leaving the caller hanging
            import traceback
            traceback.print_exc()
            self._finish(str(e))

    def _sleep(self, secs):
        end = time.monotonic() + secs
        return lambda: time.monotonic() >= end

    def _first_frame(self):
        end = time.monotonic() + 5
        def cond():
            if self._latest()[0] is not None:
                return True
            if time.monotonic() > end:
                raise RuntimeError("The window stream sent no picture")
            return False
        return cond

    def _settled(self):
        """True once the content stopped changing (smooth scrolling animates) or after a timeout."""
        t0 = time.monotonic()
        def cond():
            now = time.monotonic()
            if now - t0 >= self.SETTLE_TIMEOUT:
                return True
            return now - t0 >= self.MIN_WAIT and now - self._changed_at >= self.QUIET
        return cond

    def _centre(self):
        r = self.sel.rect
        return r.x() + r.width() / 2, r.y() + r.height() / 2

    def _aim(self):
        """Window on top + focused, pointer over the area."""
        self.kwin.activate(self.win["id"])
        self.input.move(*self._centre())
        self._aimed_at = time.monotonic()

    def _on_cursor(self, x, y, fx, fy):
        """The pointer left the spot we put it on: the user wants it back (e.g. to click
        Stop), so finish.  Our own moves land exactly on the centre."""
        if self._prev_on_cursor:
            self._prev_on_cursor(x, y, fx, fy)
        cx, cy = self._centre()
        if self._aimed_at and time.monotonic() - self._aimed_at > 0.15 and abs(x - cx) + abs(y - cy) > 40:
            self._user_moved = True

    def _auto(self):
        yield self._first_frame()
        self._aim()
        yield self._sleep(0.2)
        if self.from_top:
            prev = self._latest()[0]
            for _ in range(40):
                self._aim()
                self.input.scroll(-30)
                yield self._settled()
                cur = self._latest()[0]
                e = max(4, cur.shape[1] - self.edge)
                if cur.shape == prev.shape and (cur[:, :e] != prev[:, :e]).any(axis=(1, 2)).mean() < 0.02:
                    break   # didn't move any more: at the top
                prev = cur
        frame = self._latest()[0]
        self.stitcher = Stitcher(frame, edge=self.edge)
        self.progress.emit(self.stitcher.height)
        notches, per_notch = 1.0, None   # first step calibrates px per wheel notch
        zeros = fails = 0
        while self.stitcher.height < self.max_height:
            target = self.step * (self.stitcher.H - self.stitcher.header - self.stitcher.footer)
            self._aim()
            self.input.scroll(notches)
            yield self._settled()
            expected = notches * per_notch if per_notch else None
            d = yield from self._add(expected)
            if d is None:
                # moved too far to find the overlap (or something odd): go back, smaller steps
                fails += 1
                if fails > 3:
                    print("snappie: scrolling capture lost track of the page, stopping")
                    break
                self.input.scroll(-notches)
                yield self._settled()
                notches = max(notches / 2, 0.25)
                if per_notch:
                    per_notch *= 2
                continue
            fails = 0
            if d == 0:
                zeros += 1          # twice in a row, to be sure it's the end and not a slow page
                if zeros >= 2:
                    break
                continue
            zeros = 0
            if expected is None or d >= 0.8 * expected:   # a full step (not the last, short one)
                pn = d / notches
                per_notch = pn if per_notch is None else 0.5 * (per_notch + pn)
            notches = min(max(target / per_notch, 0.25), 200)
            self.progress.emit(self.stitcher.height)

    def _add(self, expected=None):
        """Stitch the latest frame in a thread (a big window takes ~0.1 s). Use with
        `yield from`; returns the offset like Stitcher.add."""
        frame, box = self._latest()[0], {}

        def run():
            try:
                box["d"] = self.stitcher.add(frame, expected)
            except Exception as e:  # noqa: BLE001 - re-raised below, on the GUI thread
                box["e"] = e
        self._job = threading.Thread(target=run, daemon=True)
        self._job.start()
        yield lambda: not self._job.is_alive()
        if "e" in box:
            raise _Resized() if isinstance(box["e"], ValueError) else box["e"]
        return box["d"]

    def _manual(self):
        yield self._first_frame()
        frame, seen = self._latest()
        self.stitcher = Stitcher(frame, edge=self.edge)
        self.progress.emit(self.stitcher.height)
        while self.stitcher.height < self.max_height:
            yield self._sleep(0.1)
            frame, seq = self._latest()
            if seq == seen:
                continue
            seen = seq
            if (yield from self._add()):
                self.progress.emit(self.stitcher.height)

    # -- end --

    def _finish(self, error=""):
        if self._done:
            return
        self._done = True
        self._timer.stop()
        if self._tracking:
            self.kwin.untrack_cursor()
            self.kwin.on_cursor = self._prev_on_cursor
            self._tracking = False
        if self._job:
            self._job.join()   # a stitch step still running
        if self.pipeline:
            from .cast import Gst
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.cast:
            self.cast.close()
            self.cast = None
        if self.input:
            self.input.close()
            self.input = None
        img = None
        if not error:
            arr = self.stitcher.image() if self.stitcher else None
            if arr is not None:
                arr = arr[:self.max_height]
            img = self._qimage(arr) if arr is not None else None
        self.finished.emit(img, error)

    def _qimage(self, arr):
        from .cast import to_qimage
        img = to_qimage(arr)
        img.setDevicePixelRatio(self.scale)
        return img
