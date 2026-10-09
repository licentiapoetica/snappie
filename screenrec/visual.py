"""Find inner areas of a window from its pixels (for apps without an accessibility tree).

An edge is a row (or column) where much of the line changes sharply versus
the previous one, e.g. the separator between Chromium's toolbar and the page.
Its strength is the share of the line that changes.

Edges are nested by strength: dropping the weakest edges first merges small
areas into their parents (a page section into the whole page, the page into
the window content).  Under the cursor, the chain of areas goes from big to
small, each step taking the largest area that is still smaller, across both
directions.
"""

import numpy as np

EDGE_FRACTION = 0.5    # share of the line that must change to count as an edge
EDGE_DELTA = 24        # minimum change of R+G+B per pixel
MIN_AREA = 12          # thinner bands are separator lines, not areas
MAX_DEPTH = 10


class Segmenter:
    def __init__(self, frame, frame_rect):
        """frame: (H, W, 3) uint8 image of the window; frame_rect: its logical (x, y, w, h)."""
        self.img = frame.astype(np.int16).sum(axis=2)
        self.fx, self.fy, fw, fh = frame_rect
        self.sx = self.img.shape[1] / fw
        self.sy = self.img.shape[0] / fh
        self._cache = {}

    def _edges(self, x0, y0, x1, y1, axis):
        """Edge clusters [(first, last, strength)] along `axis`, positions relative to the region."""
        key = (x0, y0, x1, y1, axis)
        if key not in self._cache:
            sub = self.img[y0:y1, x0:x1]
            if axis == 1:
                sub = sub.T
            out = []
            if sub.shape[0] >= 2 * MIN_AREA:
                frac = (np.abs(sub[1:] - sub[:-1]) > EDGE_DELTA).mean(axis=1)
                for pos in (np.nonzero(frac > EDGE_FRACTION)[0] + 1).tolist():
                    strength = float(frac[pos - 1])
                    if out and pos - out[-1][1] <= 2:  # neighbouring rows: one separator
                        a, _, s = out[-1]
                        out[-1] = (a, pos, max(s, strength))
                    else:
                        out.append((pos, pos, strength))
            self._cache[key] = out
        return self._cache[key]

    def _groups(self, x0, y0, x1, y1, axis, px, py):
        """Nested spans around the cursor along one axis, as region rects (small → big)."""
        span = (y1 - y0) if axis == 0 else (x1 - x0)
        pos = (py - y0) if axis == 0 else (px - x0)
        edges = self._edges(x0, y0, x1, y1, axis)
        if any(a <= pos < b for a, b, _ in edges):
            return []  # on a separator line
        groups = []
        for t in [-1.0] + sorted({s for _, _, s in edges}):
            active = [e for e in edges if e[2] > t]
            start = max([b for a, b, _ in active if b <= pos], default=0)
            end = min([a for a, b, _ in active if a > pos], default=span)
            if end - start < MIN_AREA or end - start >= span:
                continue
            g = (x0, y0 + start, x1, y0 + end) if axis == 0 else (x0 + start, y0, x0 + end, y1)
            if g not in groups:
                groups.append(g)
        return groups

    def chain_at(self, gx, gy, start):
        """Nested areas (global logical rects, big → small) under a point, inside `start` (x, y, w, h)."""
        H, W = self.img.shape
        px, py = int((gx - self.fx) * self.sx), int((gy - self.fy) * self.sy)
        x0 = max(0, int((start[0] - self.fx) * self.sx))
        y0 = max(0, int((start[1] - self.fy) * self.sy))
        x1 = min(W, int((start[0] + start[2] - self.fx) * self.sx))
        y1 = min(H, int((start[1] + start[3] - self.fy) * self.sy))
        if not (x0 <= px < x1 and y0 <= py < y1):
            return []
        out = []
        while len(out) < MAX_DEPTH:
            cands = self._groups(x0, y0, x1, y1, 0, px, py) + self._groups(x0, y0, x1, y1, 1, px, py)
            if not cands:
                break
            x0, y0, x1, y1 = max(cands, key=lambda g: (g[2] - g[0]) * (g[3] - g[1]))
            out.append(self._to_logical(x0, y0, x1, y1))
        return out

    def _to_logical(self, x0, y0, x1, y1):
        lx, ly = self.fx + x0 / self.sx, self.fy + y0 / self.sy
        return (round(lx), round(ly), round((x1 - x0) / self.sx), round((y1 - y0) / self.sy))
