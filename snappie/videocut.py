"""What the video editor changes, without any Qt: the timeline split into pieces that are kept
or removed, and a crop box.  Every change is undoable.  convert.Options takes the result."""

MIN_PIECE = 0.05     # seconds; no splits closer than this to a piece's ends


class VideoCut:
    def __init__(self, duration, width, height):
        self.duration, self.width, self.height = float(duration), int(width), int(height)
        self.pieces = [[0.0, self.duration, True]]     # [start, end, keep], in order, covering it all
        self.crop = None                               # (x, y, w, h) in video pixels
        self._undo, self._redo = [], []

    # -- undo

    def _state(self):
        return [list(p) for p in self.pieces], self.crop

    def _push(self):
        self._undo.append(self._state())
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(self._state())
        self.pieces, self.crop = self._undo.pop()
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self._state())
        self.pieces, self.crop = self._redo.pop()
        return True

    # -- pieces

    def piece_at(self, t):
        for i, (s, e, _) in enumerate(self.pieces):
            if s <= t < e:
                return i
        return len(self.pieces) - 1 if t >= self.duration else 0

    def split(self, t):
        """Split the piece under t in two.  Returns False if t is too close to an edge."""
        i = self.piece_at(t)
        s, e, keep = self.pieces[i]
        if t - s < MIN_PIECE or e - t < MIN_PIECE:
            return False
        self._push()
        self.pieces[i:i + 1] = [[s, t, keep], [t, e, keep]]
        return True

    def set_kept(self, i, keep):
        if self.pieces[i][2] == keep:
            return False
        if keep is False and sum(1 for p in self.pieces if p[2]) == 1:
            return False                  # keep at least one piece
        self._push()
        self.pieces[i][2] = keep
        return True

    def toggle(self, i):
        return self.set_kept(i, not self.pieces[i][2])

    def trim_start(self, t):
        """Remove everything before t."""
        return self._trim(t, before=True)

    def trim_end(self, t):
        """Remove everything after t."""
        return self._trim(t, before=False)

    def _trim(self, t, before):
        if not MIN_PIECE <= t <= self.duration - MIN_PIECE:
            return False
        pieces = [list(p) for p in self.pieces]
        i = self.piece_at(t)
        s, e, keep = pieces[i]
        if t - s >= MIN_PIECE and e - t >= MIN_PIECE:
            pieces[i:i + 1] = [[s, t, keep], [t, e, keep]]
        for p in pieces:
            if (p[1] <= t + 1e-9) if before else (p[0] >= t - 1e-9):
                p[2] = False
        if not any(p[2] for p in pieces) or pieces == self.pieces:
            return False
        self._push()
        self.pieces = pieces
        return True

    def snap(self, t):
        """t, or the split point / end it's within MIN_PIECE of (a range can't leave a sliver)."""
        t = min(max(0.0, t), self.duration)
        edges = [0.0] + [p[1] for p in self.pieces]
        near = min(edges, key=lambda e: abs(e - t))
        return near if abs(near - t) < MIN_PIECE else t

    def set_range(self, a, b, keep):
        """Keep or remove everything between a and b (a range dragged on the timeline): one
        piece afterwards, split from its neighbors.  False if nothing changes, the range is too
        short, or nothing would be left."""
        a, b = sorted((self.snap(a), self.snap(b)))
        if b - a < MIN_PIECE:
            return False
        if all(k == keep for s, e, k in self.pieces if s < b - 1e-9 and e > a + 1e-9):
            return False                            # already that way
        before = [list(p) for p in self.pieces if p[1] <= a + 1e-9]
        after = [list(p) for p in self.pieces if p[0] >= b - 1e-9]
        for s, e, k in self.pieces:
            if s < a - 1e-9 and e > a + 1e-9:
                before.append([s, a, k])            # the piece the range starts in
            if s < b - 1e-9 and e > b + 1e-9:
                after.insert(0, [b, e, k])          # the piece it ends in
        pieces = before + [[a, b, keep]] + after
        if not any(p[2] for p in pieces):
            return False
        self._push()
        self.pieces = pieces
        return True

    def range_kept(self, a, b):
        """Whether anything between a and b is kept (so the action on it is "remove")."""
        a, b = sorted((a, b))
        return any(k for s, e, k in self.pieces if s < b - 1e-9 and e > a + 1e-9)

    def kept(self):
        """The kept pieces, neighbors merged: [(start, end)]."""
        out = []
        for s, e, keep in self.pieces:
            if not keep:
                continue
            if out and abs(out[-1][1] - s) < 1e-9:
                out[-1] = (out[-1][0], e)
            else:
                out.append((s, e))
        return out

    def segments(self, within=None):
        """For convert.Options: None when the whole video is kept.  With within=(start, end) (a
        selected range), only the kept parts inside it; [] when none are."""
        k = self.kept()
        if within:
            a, b = sorted((self.snap(within[0]), self.snap(within[1])))
            k = [(max(s, a), min(e, b)) for s, e in k if min(e, b) - max(s, a) > 1e-9]
        return None if k == [(0.0, self.duration)] else k

    def kept_duration(self):
        return sum(e - s for s, e in self.kept())

    def skip_to(self, t):
        """While playing: where to jump if t is in a removed piece (None = keep going; the
        duration = nothing kept after it)."""
        i = self.piece_at(t)
        if self.pieces[i][2]:
            return None
        nxt = next((p[0] for p in self.pieces[i:] if p[2]), None)
        return self.duration if nxt is None else nxt

    # -- crop

    def set_crop(self, box):
        box = self.clamp_crop(box) if box else None
        if box == (0, 0, self.width, self.height):
            box = None
        if box == self.crop:
            return False
        self._push()
        self.crop = box
        return True

    def clamp_crop(self, box):
        """Inside the video, at least 16 px, even sizes (the video encoders need them)."""
        x, y, w, h = (int(round(v)) for v in box)
        w, h = max(16, min(w, self.width)), max(16, min(h, self.height))
        x, y = max(0, min(x, self.width - w)), max(0, min(y, self.height - h))
        return x, y, w // 2 * 2, h // 2 * 2

    def aspect_crop(self, ratio):
        """The largest centered box with this width/height ratio."""
        w, h = self.width, self.height
        if w / h > ratio:
            w = h * ratio
        else:
            h = w / ratio
        return self.clamp_crop(((self.width - w) / 2, (self.height - h) / 2, w, h))
