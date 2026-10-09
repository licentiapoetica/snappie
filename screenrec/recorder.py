"""GStreamer pipeline: PipeWire screen stream -> crop -> VAAPI encode + audio -> MKV."""

import os
import shutil
import subprocess

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

# Capped VBR: a bitrate ceiling keeps fast motion (games, video) from exploding,
# a QP floor keeps static content from wasting bits.
# quality: (bits per pixel per frame at 60 fps, min qp for hevc/h264, min qindex for av1)
QUALITY = {"high": (0.50, 10, 30), "good": (0.30, 14, 50), "small": (0.12, 18, 72)}
CODEC_EFFICIENCY = {"hevc": 1.0, "av1": 0.75, "h264": 1.5}

CODECS = {
    # name: (encoder factory, parser, width align, height align)
    "hevc": ("vah265enc", "h265parse", 2, 2),
    "h264": ("vah264enc", "h264parse", 2, 2),
    "av1": ("vaav1enc", "av1parse", 64, 16),  # AMD pads AV1 to 64x16 with black otherwise
}


# Screen pixels are sRGB values; video players decode BT.709 and display them
# as-is, so the values must go straight through the BT.709 matrix.  Left alone,
# vapostproc assumes sRGB input and converts the transfer curve (midtones lifted:
# washed out) and uses a BT.601-ish matrix.  Declaring the input as
# full-range RGB with a BT.709 curve makes it a plain, exact matrix conversion.
GPU_CONVERT = ('capssetter caps="video/x-raw,colorimetry=(string)1:1:5:1" replace=false'
               ' ! vapostproc ! video/x-raw(memory:VAMemory),format=NV12,colorimetry=bt709')


def have(factory):
    return Gst.ElementFactory.find(factory) is not None


def pick_codec(wanted):
    order = [wanted] + [c for c in ("hevc", "av1", "h264") if c != wanted]
    for name in order:
        if have(CODECS[name][0]):
            return name
    if have("svtav1enc"):
        return "av1-sw"
    raise RuntimeError("No usable video encoder (install gstreamer-plugins-bad with VA support)")


def _encoder_desc(codec, quality, qp, pixels):
    """pixels: approximate encoded width*height (sets the bitrate ceiling)."""
    bpp, min_qp, min_qindex = QUALITY[quality]
    if codec == "av1-sw":
        return f"videoconvert ! video/x-raw,colorimetry=bt709 ! svtav1enc preset=10 crf={qp or 32} ! av1parse"
    enc, parse = CODECS[codec][:2]
    extra = " b-frames=0" if codec == "h264" else ""
    if qp:  # explicit constant quantizer
        rc = f"rate-control=cqp qp={qp}" if codec == "av1" else \
            f"rate-control=cqp qpi={max(qp - 2, 1)} qpp={qp} qpb={qp}"
    else:
        kbps = max(2000, int(pixels * 60 * bpp * CODEC_EFFICIENCY[codec] / 1000))
        floor = min_qindex if codec == "av1" else min_qp
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

    def __init__(self, node, ref, rect, path, codec="hevc", quality="good", qp=None,
                 fps=None, scale=1.0, system_audio=True, mic=False, on_done=None, on_error=None):
        self.ref, self.rect, self.path = ref, rect, path
        self._last_pts = None
        self.codec = codec
        self.on_done, self.on_error = on_done, on_error
        self._crop = None   # (x, y, w, h) in stream pixels, fixed after the first frame
        self._stopping = False

        rate = f" ! videorate ! video/x-raw,framerate={fps}/1" if fps else ""
        desc = [
            f"pipewiresrc name=src path={node} do-timestamp=true"
            f" keepalive-time=1000 provide-clock=false always-copy=true"
            f" ! capsfilter caps=video/x-raw ! videocrop name=crop{rate} ! queue max-size-buffers=8 leaky=downstream"
        ]
        enc = _encoder_desc(codec, quality, qp, rect[2] * rect[3] * scale * scale)
        if codec == "av1-sw":
            desc.append(f" ! {enc} ! queue ! mux.")
        else:
            desc.append(f" ! {GPU_CONVERT} ! {enc} ! queue ! mux.")
        sources = []
        if system_audio:
            sources.append("pulsesrc device=@DEFAULT_MONITOR@ client-name=screenrec")
        if mic:
            sources.append("pulsesrc device=@DEFAULT_SOURCE@ client-name=screenrec-mic")
        if len(sources) == 1:
            desc.append(f" {sources[0]} ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"
                        " ! queue ! opusenc bitrate=192000 ! queue ! mux.")
        elif sources:
            desc.append(" audiomixer name=amix latency=20000000 ! audioconvert"
                        " ! opusenc bitrate=192000 ! queue ! mux.")
            for s in sources:
                desc.append(f" {s} ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2"
                            " ! queue ! amix.")
        desc.append(f" matroskamux name=mux writing-app=screenrec ! filesink location=\"{path}\"")

        self.pipeline = Gst.parse_launch("".join(desc))
        self.pipeline.use_clock(Gst.SystemClock.obtain())
        self.crop = self.pipeline.get_by_name("crop")
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
        px = min(max(px, 0), W - pw)
        py = min(max(py, 0), H - ph)
        for prop, val in (("left", px), ("top", py), ("right", W - px - pw), ("bottom", H - py - ph)):
            self.crop.set_property(prop, val)

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


def remux_mp4(mkv_path, codec):
    """Fast lossless container change to MP4 (plays everywhere). Returns final path."""
    if not shutil.which("ffmpeg"):
        return mkv_path
    mp4 = mkv_path[:-4] + ".mp4"
    tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
    # write BT.709 limited range into the bitstream itself too, not only the container
    vui = {"hevc": "hevc_metadata", "h264": "h264_metadata"}.get(codec)
    if vui:
        tag += ["-bsf:v", f"{vui}=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1"
                          ":video_full_range_flag=0"]
    elif codec.startswith("av1"):
        tag += ["-bsf:v", "av1_metadata=color_primaries=1:transfer_characteristics=1:matrix_coefficients=1"
                          ":color_range=tv"]
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mkv_path, "-map", "0", "-c", "copy", *tag,
                        "-movflags", "+faststart", mp4], capture_output=True, text=True)
    if r.returncode == 0:
        os.unlink(mkv_path)
        return mp4
    return mkv_path
