"""GStreamer pipeline: PipeWire screen stream -> crop -> VAAPI encode + audio -> MKV.

VP8 (and VP9 where the GPU can't encode it, e.g. AMD) use libvpx on the CPU in
its realtime mode.
"""

import os
import shutil
import subprocess
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

# Capped VBR: a bitrate ceiling keeps fast motion (games, video) from exploding,
# a QP floor keeps static content from wasting bits.
# quality: (bits per pixel per frame at 60 fps, min qp for hevc/h264, min qindex for av1)
QUALITY = {"high": (0.50, 10, 30), "good": (0.30, 14, 50), "small": (0.12, 18, 72)}
CODEC_EFFICIENCY = {"hevc": 1.0, "av1": 0.75, "h264": 1.5, "vp9": 1.0, "vp8": 1.5}

CODECS = {
    # name: (encoder factory, parser, width align, height align)
    "hevc": ("vah265enc", "h265parse", 2, 2),
    "h264": ("vah264enc", "h264parse", 2, 2),
    "av1": ("vaav1enc", "av1parse", 64, 16),  # AMD pads AV1 to 64x16 with black otherwise
    "vp9": ("vavp9enc", "vp9parse", 2, 2),    # Intel only; AMD and NVIDIA can't encode VP9
}
# CPU encoders, used when the GPU has none for the codec
SOFTWARE = {"av1-sw": "svtav1enc", "vp9-sw": "vp9enc", "vp8-sw": "vp8enc"}
WEBM_CODECS = ("vp9", "vp8", "av1")   # what a WebM file may contain


# Screen pixels are sRGB values; video players decode BT.709 and display them
# as-is, so the values must go straight through the BT.709 matrix.  Left alone,
# vapostproc assumes sRGB input and converts the transfer curve (midtones lifted:
# washed out) and uses a BT.601-ish matrix.  Declaring the input as
# full-range RGB with a BT.709 curve makes it a plain, exact matrix conversion.
GPU_CONVERT = ('capssetter caps="video/x-raw,colorimetry=(string)1:1:5:1" replace=false'
               ' ! vapostproc ! video/x-raw(memory:VAMemory),format=NV12,colorimetry=bt709')


def have(factory):
    return Gst.ElementFactory.find(factory) is not None


def _convert(codec):
    if codec not in SOFTWARE:
        return GPU_CONVERT
    # CPU encoders convert on the CPU too (works without VA-API, e.g. NVIDIA);
    # videoconvert applies the same plain BT.709 matrix.
    return f"videoconvert n-threads={min(os.cpu_count() or 4, 16)} ! video/x-raw,format=I420,colorimetry=bt709"


def base_codec(codec):
    """"vp9-sw" -> "vp9"."""
    return codec[:-3] if codec.endswith("-sw") else codec


def pick_codec(wanted, webm=False):
    """The encoder to use: `wanted` on the GPU, else on the CPU, else another codec.
    webm: only codecs a WebM file can hold (anything else asked for becomes VP9)."""
    if webm and wanted not in WEBM_CODECS:
        wanted = "vp9"
    others = [c for c in (WEBM_CODECS if webm else ("hevc", "av1", "h264")) if c != wanted]
    order = [wanted, wanted + "-sw"] + others + [c + "-sw" for c in others] + ["vp9-sw"]  # last resort: libvpx
    for name in order:
        factory = CODECS[name][0] if name in CODECS else SOFTWARE.get(name)
        if factory and have(factory):
            return name
    from . import deps
    raise RuntimeError(f"No usable video encoder ({deps.install_hint(['encoder'])})")


def final_container(codec, wanted):
    """mp4 | mkv | webm for a recording in `codec`; MP4 can't hold VP8, so that becomes WebM."""
    if wanted == "mp4" and base_codec(codec) == "vp8":
        return "webm"
    return wanted


def _encoder_desc(codec, quality, qp, pixels):
    """pixels: approximate encoded width*height (sets the bitrate ceiling)."""
    bpp, min_qp, min_qindex = QUALITY[quality]
    kbps = max(2000, int(pixels * 60 * bpp * CODEC_EFFICIENCY[base_codec(codec)] / 1000))
    if codec == "av1-sw":
        return f"svtav1enc preset=10 crf={qp or 32} ! av1parse"
    if codec in ("vp9-sw", "vp8-sw"):
        # realtime mode: fast enough for 60 fps; libvpx quantizers are 0-63 (AV1/VP9 qindex / 4)
        q = min(max(qp, 0), 63) if qp else None
        rc = (f"end-usage=vbr target-bitrate={kbps * 1000} min-quantizer={q if q is not None else min_qindex // 4}"
              + (f" max-quantizer={q}" if q is not None else ""))
        threads = min(os.cpu_count() or 4, 16)
        common = f"deadline=1 cpu-used=8 threads={threads} lag-in-frames=0 keyframe-max-dist=240 {rc}"
        if codec == "vp9-sw":
            return f"vp9enc {common} row-mt=true tile-columns=3 ! vp9parse"
        return f"vp8enc {common} token-partitions=3"
    enc, parse = CODECS[codec][:2]
    qindex = codec in ("av1", "vp9")   # 0-255 scale instead of 0-51
    extra = " b-frames=0" if codec == "h264" else ""
    if qp:  # explicit constant quantizer
        rc = f"rate-control=cqp qp={qp}" if qindex else \
            f"rate-control=cqp qpi={max(qp - 2, 1)} qpp={qp} qpb={qp}"
    else:
        floor = min_qindex if qindex else min_qp
        rc = f"rate-control=vbr bitrate={kbps} target-percentage=80 cpb-size={kbps * 2} min-qp={floor}"
    # target-usage=1: the encoder's best-quality preset (still >100 fps at 3440x1440 on RDNA3)
    return f"{enc} {rc} target-usage=1 key-int-max=240{extra} ! {parse}"


def _align(v, a, limit):
    if a <= 2:  # just needs even: lose one pixel rather than show a neighbouring one
        return v - v % a
    up = -(-v // a) * a
    return up if up <= limit else (limit // a) * a


class Recorder:
    """Records (a crop of) one KWin stream.

    `ref` is the logical rect the stream shows (window frame or screen region),
    or a list of candidates (the one matching the frame's aspect ratio wins),
    `rect` the logical rect to keep.  The pixel scale comes from the negotiated
    caps, so fractional scaling works.  Window streams keep following the
    window by themselves, even when it is covered or moved.
    """

    def __init__(self, node, ref, rect, path, codec="hevc", quality="good", qp=None, crop=None,
                 fps=None, scale=1.0, system_audio=True, mic=False, overlay=False,
                 on_done=None, on_error=None):
        self.ref, self.rect, self.path = ref, rect, path
        self._last_pts = None
        self.codec = codec
        self.on_done, self.on_error = on_done, on_error
        self._crop = crop   # (x, y, w, h) in stream pixels, fixed after the first frame
        self._stopping = False
        self.source_lost = False  # KWin's stream died; the file was finalized anyway

        rate = f" ! videorate ! video/x-raw,framerate={fps}/1" if fps else ""
        desc = [
            f"pipewiresrc name=src path={node} do-timestamp=true"
            f" keepalive-time=1000 provide-clock=false always-copy=true"
            " ! capsfilter caps=video/x-raw"
            # live drawings / click ripples blended in (window streams don't contain our overlay).
            # Before the crop, in full-frame coordinates: videocrop may only attach crop metadata
            # instead of cutting, and the overlay would then ignore the crop offset.
            + (" ! gdkpixbufoverlay name=ov" if overlay else "")
            + f" ! videocrop name=crop{rate} ! queue max-size-buffers=8 leaky=downstream"
        ]
        enc = _encoder_desc(codec, quality, qp, rect[2] * rect[3] * scale * scale)
        desc.append(f" ! {_convert(codec)} ! {enc} ! queue ! mux.")
        sources = []
        if system_audio:
            sources.append("pulsesrc device=@DEFAULT_MONITOR@ client-name=snappie")
        if mic:
            sources.append("pulsesrc device=@DEFAULT_SOURCE@ client-name=snappie-mic")
        if len(sources) == 1:
            desc.append(f" {sources[0]} ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"
                        " ! queue ! opusenc bitrate=192000 ! queue ! mux.")
        elif sources:
            desc.append(" audiomixer name=amix latency=20000000 ! audioconvert"
                        " ! opusenc bitrate=192000 ! queue ! mux.")
            for s in sources:
                desc.append(f" {s} ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"
                            " ! queue ! amix.")
        desc.append(f" matroskamux name=mux writing-app=snappie ! filesink location=\"{path}\"")

        self.pipeline = Gst.parse_launch("".join(desc))
        self.pipeline.use_clock(Gst.SystemClock.obtain())
        self.crop = self.pipeline.get_by_name("crop")
        self.src = self.pipeline.get_by_name("src")
        self.ov = self.pipeline.get_by_name("ov")
        self._ov_pending = None
        if self.ov:
            self.ov.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._ov_probe)
        self.crop.get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM | Gst.PadProbeType.BUFFER, self._probe)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    # runs in the streaming thread, before videocrop sees new caps
    def _probe(self, pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            # KWin occasionally delivers two frames with the same timestamp. The muxer
            # would drop the second one *after* encoding, and every later frame that
            # references it decodes broken. Dropping it here, before the encoder, is harmless.
            pts = info.get_buffer().pts
            if pts != Gst.CLOCK_TIME_NONE:
                if self._last_pts is not None and pts <= self._last_pts:
                    return Gst.PadProbeReturn.DROP
                self._last_pts = pts
            return Gst.PadProbeReturn.OK
        ev = info.get_event()
        if ev.type == Gst.EventType.CAPS:
            s = ev.parse_caps().get_structure(0)
            self._apply_crop(s.get_value("width"), s.get_value("height"))
        return Gst.PadProbeReturn.OK

    def _apply_crop(self, W, H):
        if self._crop is None:
            refs = self.ref if isinstance(self.ref, list) else [self.ref]
            rx, ry, rw, rh = min(refs, key=lambda r: abs(W / r[2] - H / r[3]))
            x, y, w, h = self.rect
            sx, sy = W / rw, H / rh
            _, _, aw, ah = CODECS.get(self.codec, (0, 0, 2, 2))
            pw = _align(max(16, min(round(w * sx), W)), aw, W)
            ph = _align(max(16, min(round(h * sy), H)), ah, H)
            # alignment may grow the area: grow around its centre
            px = round((x - rx) * sx) - (pw - round(w * sx)) // 2
            py = round((y - ry) * sy) - (ph - round(h * sy)) // 2
            self._crop = (px, py, pw, ph)
        px, py, pw, ph = self._crop
        pw, ph = min(pw, W), min(ph, H)   # window shrank below the area: can't keep the size
        # ...but keep whole encoder blocks: an odd width made AMD's AV1 encoder hang the GPU
        _, _, aw, ah = CODECS.get(self.codec, (0, 0, 2, 2))
        pw, ph = max(pw - pw % aw, min(W, aw)), max(ph - ph % ah, min(H, ah))
        px = min(max(px, 0), W - pw)
        py = min(max(py, 0), H - ph)
        self._crop_origin = (px, py)   # where the kept area starts in the full frame (for the overlay)
        for prop, val in (("left", px), ("top", py), ("right", W - px - pw), ("bottom", H - py - ph)):
            self.crop.set_property(prop, val)

    def set_overlay(self, image, x=0, y=0):
        """Blend a QImage (RGBA8888, stream pixels) at x, y into every frame; None clears it.

        Only hands the image over: the streaming thread applies it (_ov_probe). Setting the
        element's properties from here deadlocks — we'd hold the GIL waiting for the element's
        lock while the streaming thread holds that lock, freeing the previous pixbuf, which
        needs the GIL."""
        if not self.ov:
            return
        if image is None or image.isNull():
            self._ov_pending = ("clear",)
            self._keepalive(1000)
            return
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        from PyQt6.QtGui import QImage
        img = image.convertToFormat(QImage.Format.Format_RGBA8888)
        data = bytes(img.constBits().asarray(img.sizeInBytes()))
        pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB, True, 8,
                                             img.width(), img.height(), img.bytesPerLine())
        self._ov_pending = (pb, int(x), int(y))
        # a window that doesn't repaint sends no frames: repeat the last one so the drawing
        # (and its fading) shows up in the video
        self._keepalive(33)

    def _keepalive(self, ms):
        if getattr(self, "_keepalive_ms", 1000) != ms:
            self._keepalive_ms = ms
            self.src.set_property("keepalive-time", ms)

    def _ov_probe(self, pad, info):
        """Streaming thread, right before the overlay element: apply the newest image."""
        upd, self._ov_pending = self._ov_pending, None
        if upd is not None:
            if upd[0] == "clear":
                self.ov.set_property("alpha", 0.0)
            else:
                pb, x, y = upd
                ox, oy = getattr(self, "_crop_origin", (0, 0))
                x, y = x + ox, y + oy
                if not getattr(self, "_ov_logged", False):
                    self._ov_logged = True
                    from .app_util import log
                    log(f"drawing blended into the video ({pb.get_width()}x{pb.get_height()} at {x},{y})")
                self.ov.set_property("pixbuf", pb)
                self.ov.set_property("offset-x", x)
                self.ov.set_property("offset-y", y)
                self.ov.set_property("alpha", 1.0)
        return Gst.PadProbeReturn.OK

    @property
    def crop_rect(self):
        return self._crop

    @property
    def size(self):
        return self._crop and self._crop[2:]

    # -- lifecycle --

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not start the recording pipeline")

    def stop(self):
        if self._stopping:
            return
        self._stopping = True
        self.pipeline.send_event(Gst.Event.new_eos())
        GLib.timeout_add_seconds(5, self._force_finish)  # EOS should arrive long before this

    def _force_finish(self):
        if self.pipeline:
            self._finish()
        return False

    def _on_message(self, bus, msg):
        if msg.type == Gst.MessageType.EOS:
            self._finish()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if msg.src.get_name() == "src" and not self._stopping:
                # The screen stream died (KWin side). The video branch already got EOS from
                # the source; end the audio too so the file is finalized, then report it.
                self.source_lost = self._stopping = True
                self.pipeline.send_event(Gst.Event.new_eos())
                GLib.timeout_add_seconds(5, self._force_finish)
                return
            self._teardown()
            if self.on_error:
                self.on_error(f"{err.message}\n{dbg or ''}".strip())

    def _teardown(self):
        if self.pipeline:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _finish(self):
        self._teardown()
        if self.on_done:
            self.on_done(self.path)


def join_parts(parts, path):
    """Concatenate recording segments (same format) into `path` (MKV). Returns path."""
    if len(parts) == 1:
        if parts[0] != path:
            os.replace(parts[0], path)
        return path
    listing = path + ".parts.txt"
    with open(listing, "w") as f:
        for p in parts:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    tmp = path[:-4] + ".joined.mkv"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing,
                        "-map", "0", "-c", "copy", tmp], capture_output=True, text=True)
    os.unlink(listing)
    if r.returncode != 0:
        return parts[0]  # keep the segments as they are
    for p in parts:
        os.unlink(p)
    os.replace(tmp, path)
    return path


def to_animation(video, out_path, fps=15, max_width=960):
    """Convert a recording to an optimized GIF or animated WebP (by out_path's extension).
    Returns out_path, or raises RuntimeError."""
    scale = f",scale='min({max_width},iw)':-2:flags=lanczos" if max_width else ""
    if out_path.endswith(".webp"):
        cmd = ["-vf", f"fps={fps}{scale}", "-c:v", "libwebp_anim", "-lossless", "0", "-quality", "80",
               "-compression_level", "4", "-loop", "0", "-an"]
    else:
        # one palette for the whole clip, only changed rectangles re-dithered: small and stable
        cmd = ["-vf", f"fps={fps}{scale},split[a][b];[a]palettegen=stats_mode=diff:max_colors=256[p];"
                      "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
               "-loop", "0", "-an"]
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, *cmd, out_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("converting failed: " + (r.stderr.strip().splitlines() or ["ffmpeg error"])[-1])
    return out_path


def remux(mkv_path, codec, container="mp4"):
    """Fast lossless container change to MP4 (plays everywhere) or WebM (browsers).
    Returns the final path (the MKV if that fails)."""
    if container == "mkv" or not shutil.which("ffmpeg"):
        return mkv_path
    out = mkv_path[:-4] + "." + container
    tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
    # write BT.709 limited range into the bitstream itself too, not only the container
    vui = {"hevc": "hevc_metadata", "h264": "h264_metadata"}.get(codec)
    if vui:
        tag += ["-bsf:v", f"{vui}=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1"
                          ":video_full_range_flag=0"]
    elif codec.startswith("av1"):
        tag += ["-bsf:v", "av1_metadata=color_primaries=1:transfer_characteristics=1:matrix_coefficients=1"
                          ":color_range=tv"]
    elif codec.startswith("vp9"):
        tag += ["-bsf:v", "vp9_metadata=color_space=bt709:color_range=tv"]
    if container == "mp4":
        tag += ["-movflags", "+faststart"]
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mkv_path, "-map", "0", "-c", "copy", *tag, out],
                       capture_output=True, text=True)
    if r.returncode == 0:
        os.unlink(mkv_path)
        return out
    if os.path.exists(out):
        os.unlink(out)
    return mkv_path
