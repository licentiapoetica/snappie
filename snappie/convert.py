"""Convert between videos and animated images (GIF, WebP, MP4, WebM), cut and crop, with ffmpeg.

One code path for every direction; the output format decides the encoder.  build() only
makes the ffmpeg command line (pure, tested offline); run() runs it with progress and cancel.

- Cuts and repeats: the source is opened once per kept piece (input -ss/-t, frame-exact when
  re-encoding) and the pieces are joined with the concat filter.  Unlike the loop/split filters
  this never holds decoded frames in memory, so it's fine for long videos.
- Transparency: MP4/WebM have no alpha, so GIF/WebP frames are laid over a solid background
  (overlay on a color source) instead of turning black.
- Animated WebP: ffmpeg only decodes it since 8.0 (the webp_anim decoder).  With an older ffmpeg
  the frames come from Qt's WebP plugin and are piped in as raw video at a constant rate.
- Encoders differ per distro (Fedora's ffmpeg has no libx264, openSUSE's main repo neither), so
  without a chosen codec the first available one is used, down to ffmpeg's built-in mpeg4.
- When nothing about the picture changes (MKV → MP4, say), streams are copied: lossless and fast.
- A target file size turns into a bitrate (what's left after the sound, minus a margin for the
  container and the encoder missing a little).  Without a chosen frame rate, above 30 fps goes
  to 30 when the full size doesn't fit; without a chosen width, the largest common size that
  still gets enough bits per pixel for the codec is used: a small file looks better smaller than
  blocky.  If the file still comes out too big, it's encoded once more with less.
- Two passes: the first only analyses (to /dev/null), the second encodes with what it learned.  It
  hits a bitrate much more exactly, and gives libvpx/libaom better quality at a given CRF too.
"""

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

FORMATS = {"gif": "GIF", "webp": "WebP (animated)", "mp4": "MP4", "webm": "WebM"}
ANIMATIONS = ("gif", "webp")
VIDEO_FORMATS = ("mp4", "webm")
IMAGE_EXTS = (".gif", ".webp")
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpg", ".mpeg", ".ogv", ".wmv", ".flv")
ALPHA_FORMATS = ("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya8", "ya16")
MUXER = {"gif": "gif", "webp": "webp", "mp4": "mp4", "webm": "webm"}

VIDEO_ENCODERS = {"mp4": ["libx264", "libopenh264", "mpeg4"],
                  "webm": ["libvpx-vp9", "libsvtav1", "libaom-av1", "libvpx"]}
# codecs to choose from per format (names as ffprobe reports them) → encoders, best first
CODECS = {"mp4": {"h264": ["libx264", "libopenh264"], "hevc": ["libx265"], "av1": ["libsvtav1", "libaom-av1"],
                  "mpeg4": ["mpeg4"]},
          "webm": {"vp9": ["libvpx-vp9"], "vp8": ["libvpx"], "av1": ["libsvtav1", "libaom-av1"]}}
CODEC_LABEL = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1", "mpeg4": "MPEG-4", "vp9": "VP9", "vp8": "VP8"}
AUDIO_ENCODERS = {"mp4": ["aac"], "webm": ["libopus", "libvorbis"]}
COPY_VIDEO = {"mp4": {"h264", "hevc", "av1", "mpeg4"}, "webm": {"vp8", "vp9", "av1"}}
COPY_AUDIO = {"mp4": {"aac", "mp3", "opus", "ac3", "eac3", "flac", "alac"}, "webm": {"opus", "vorbis"}}
ENCODER_LABEL = {"libx264": "H.264", "libopenh264": "H.264", "mpeg4": "MPEG-4", "libvpx-vp9": "VP9",
                 "libsvtav1": "AV1", "libaom-av1": "AV1", "libvpx": "VP8", "libx265": "HEVC"}

# encoders ffmpeg can run in two passes (-pass; x265 through -x265-params), and those for which
# it's worth it without a target size too (x264/x265 CRF already does what a 2nd pass would)
TWO_PASS = {"libx264", "libx265", "libvpx", "libvpx-vp9", "libaom-av1", "libsvtav1", "mpeg4"}
CRF_TWO_PASS = {"libvpx", "libvpx-vp9", "libaom-av1"}
# bits per pixel per frame below which a size is too small for its bitrate (screen recordings and
# most video still look fine there); newer codecs need fewer
GOOD_BPP = {"libx264": 0.06, "libopenh264": 0.08, "mpeg4": 0.1, "libx265": 0.04, "libvpx": 0.07,
            "libvpx-vp9": 0.04, "libsvtav1": 0.035, "libaom-av1": 0.035}
FIT_FPS = 30            # a target size drops higher rates to this before it shrinks the picture
SHORT_SIDES = (2160, 1440, 1080, 900, 720, 576, 540, 480, 360, 288, 240, 180, 144)
MIN_VIDEO_KBPS = 40
GIF_MAX_FPS = 50        # browsers slow down GIF frames shorter than 2/100 s
MAX_PIECES = 100        # kept pieces × repeats: each is an ffmpeg input


class Options:
    """What to make.  segments: [(start, end)] seconds of the source to keep (None = all);
    crop: (x, y, w, h) in source pixels (None = all); fps/width 0 = as the source;
    codec: a key of CODECS[fmt] ("" = the first one this ffmpeg can encode);
    target_size: bytes the video should come out at (0 = go by quality; width 0 then = fit it)."""

    def __init__(self, fmt="mp4", fps=0, width=0, quality=80, segments=None, crop=None, speed=1.0,
                 repeat=1, background="#ffffff", audio=True, copy=True, codec="", target_size=0,
                 two_pass=False):
        self.squeeze = 1.0      # < 1: the last try came out too big
        self.fmt, self.fps, self.width, self.quality = fmt, fps, width, quality
        self.segments, self.crop, self.speed, self.repeat = segments, crop, speed, repeat
        self.background, self.audio, self.copy, self.codec = background, audio, copy, codec
        self.target_size, self.two_pass = target_size, two_pass

    @classmethod
    def defaults(cls, fmt):
        """The saved defaults for this output format (GIF/WebP and videos have their own)."""
        from . import settings
        if fmt in ANIMATIONS:
            return cls(fmt, settings.get("gif.fps"), settings.get("gif.max_width"), settings.get("gif.quality"),
                       background=settings.get("convert.background"))
        return cls(fmt, settings.get("convert.video_fps"), settings.get("convert.video_max_width"),
                   settings.get("convert.video_quality"), background=settings.get("convert.background"),
                   codec=settings.get(f"convert.{fmt}_codec") or "",
                   two_pass=settings.get("convert.two_pass"))

    def save_defaults(self):
        from . import settings
        if self.fmt in ANIMATIONS:
            settings.save(**{"gif.fps": self.fps, "gif.max_width": self.width, "gif.quality": self.quality,
                           "convert.background": self.background})
        else:
            settings.save(**{"convert.video_fps": self.fps, "convert.video_max_width": self.width,
                           "convert.video_quality": self.quality, "convert.background": self.background,
                           "convert.two_pass": self.two_pass})


class Info:
    """What the source is.  Sizes are as displayed (phone videos' rotation applied)."""

    def __init__(self, path, width=0, height=0, duration=0.0, fps=0.0, vcodec="", acodec="", alpha=False,
                 frames=0, image=False, piped=False):
        self.path, self.width, self.height, self.duration, self.fps = path, width, height, duration, fps
        self.vcodec, self.acodec, self.alpha, self.frames = vcodec, acodec, alpha, frames
        self.image = image      # a GIF / WebP (no audio, maybe transparent)
        self.piped = piped      # decoded by Qt, fed to ffmpeg as raw frames

    @property
    def has_audio(self):
        return bool(self.acodec)

    def describe(self):
        parts = [f"{self.width}×{self.height}"]
        if self.fps:
            parts.append(f"{self.fps:g} fps")
        if self.duration:
            parts.append(f"{self.duration:.1f} s")
        if self.image:
            parts.append(f"{self.frames} frames" if self.frames else "")
        else:
            sound = " + " + self.acodec.upper() if self.acodec else ", no sound"
            parts.append((self.vcodec or "?").upper() + sound)
        return " · ".join(p for p in parts if p)


# -- what ffmpeg can do

_caps = {}


def _ffmpeg_list(kind):
    if kind not in _caps:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", f"-{kind}"], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _caps[kind] = {m.group(1) for m in re.finditer(r"^\s*[A-Z.]{6}\s+(\S+)", out, re.M)}
    return _caps[kind]


def available():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def encoders():
    return _ffmpeg_list("encoders")


def can_decode_animated_webp():
    return "webp_anim" in _ffmpeg_list("decoders")


def video_encoder(fmt, have=None, codec=""):
    have = encoders() if have is None else have
    choices = CODECS.get(fmt, {}).get(codec, []) if codec else VIDEO_ENCODERS.get(fmt, [])
    return next((e for e in choices if e in have), None)


def audio_encoder(fmt, have=None):
    have = encoders() if have is None else have
    return next((e for e in AUDIO_ENCODERS.get(fmt, []) if e in have), None)


def missing_encoder(fmt, have=None, codec=""):
    """Why this format (and codec) can't be made with this ffmpeg, or ""."""
    have = encoders() if have is None else have
    if fmt == "webp" and "libwebp_anim" not in have:
        return "Your ffmpeg has no WebP encoder (libwebp)."
    if fmt in VIDEO_FORMATS and codec and codec not in CODECS[fmt]:
        return f"{fmt.upper()} can't hold {CODEC_LABEL.get(codec, codec)}; choose one of " \
               f"{', '.join(CODEC_LABEL[k] for k in CODECS[fmt])}."
    if fmt in VIDEO_FORMATS and not video_encoder(fmt, have, codec):
        if codec:
            return f"Your ffmpeg has no {CODEC_LABEL[codec]} encoder ({' or '.join(CODECS[fmt][codec])})."
        return f"Your ffmpeg has no {fmt.upper()} video encoder."
    return ""


# -- probing

def probe(path):
    """Info about a video or animated image, or RuntimeError."""
    if not os.path.isfile(path):
        raise RuntimeError(f"Not a file: {path}")
    image = path.lower().endswith(IMAGE_EXTS)
    if path.lower().endswith(".webp") and not can_decode_animated_webp():
        return _qt_info(path)
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,"
           "nb_frames,nb_read_frames:stream_side_data=rotation", "-of", "json"]
    if image:
        cmd.insert(3, "-count_frames")     # cheap for images; GIFs don't store a frame count
    try:
        r = subprocess.run(cmd + [path], capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise RuntimeError(f"Can't read {os.path.basename(path)}: {e}")
    return parse_probe(path, data, image)


def parse_probe(path, data, image=False):
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v or not v.get("width"):
        raise RuntimeError(f"{os.path.basename(path)} has no video in it.")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    w, h = int(v["width"]), int(v["height"])
    rot = next((sd.get("rotation") for sd in v.get("side_data_list") or [] if "rotation" in sd), 0)
    if abs(int(rot or 0)) % 180 == 90:
        w, h = h, w
    fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate"))
    frames = int(v.get("nb_read_frames") or v.get("nb_frames") or 0)
    duration = float((data.get("format") or {}).get("duration") or 0)
    if not duration and frames and fps:
        duration = frames / fps
    alpha = image or (v.get("pix_fmt") or "").startswith(ALPHA_FORMATS)
    return Info(path, w, h, duration, round(fps, 3), v.get("codec_name", ""),
                "" if image or not a else a.get("codec_name", "?"), alpha, frames, image)


def _rate(s):
    try:
        n, d = (s or "0/0").split("/")
        return float(n) / float(d) if float(d) else 0.0
    except ValueError:
        return 0.0


def _qt_info(path):
    """Animated WebP when ffmpeg can't read it: count frames and time with Qt."""
    from PyQt6.QtGui import QImageReader
    if b"webp" not in [bytes(f) for f in QImageReader.supportedImageFormats()]:
        raise RuntimeError("Your ffmpeg can't read animated WebP (that needs ffmpeg 8 or newer) and Qt's "
                           "WebP plugin isn't installed either (qt6-imageformats).")
    frames, total, size = 0, 0, None
    for img, delay in qt_frames(path):
        frames, total, size = frames + 1, total + delay, img.size()
    if not frames:
        raise RuntimeError(f"Can't read {os.path.basename(path)}.")
    duration = total / 1000
    return Info(path, size.width(), size.height(), duration, round(frames / duration, 3) if duration else 10,
                "webp", "", True, frames, image=True, piped=True)


def qt_frames(path):
    """(QImage RGBA, delay ms) for every frame, composited to the full canvas."""
    from PyQt6.QtGui import QImage, QImageReader
    r = QImageReader(path)
    while r.canRead():
        img = r.read()
        if img.isNull():
            break
        delay = r.nextImageDelay()
        yield img.convertToFormat(QImage.Format.Format_RGBA8888), (delay if delay > 0 else 100)


# -- geometry and timing

def kept(info, opts):
    """The pieces of the source to keep, clamped to it: [(start, end)].  Without cuts that's
    [(0, duration)], where a duration of 0 means unknown (the whole file)."""
    dur = info.duration or 0
    if not opts.segments:
        return [(0.0, dur)]
    out = []
    for s, e in opts.segments:
        s = max(0.0, float(s))
        e = float(e) if e is not None else dur
        if dur:
            e = min(e, dur)
        if e - s > 0.001:
            out.append((s, e))
    return out


def segments_from(start=None, end=None, cuts=()):
    """The pieces to keep from a start, an end (None = the end) and parts to remove, or None
    when nothing is cut."""
    if start is None and end is None and not cuts:
        return None
    segs = [(start or 0.0, end)]
    for a, b in sorted(cuts):
        out = []
        for s, e in segs:
            if (e is not None and e <= a) or b <= s:
                out.append((s, e))
                continue
            if s < a:
                out.append((s, a))
            if e is None or b < e:
                out.append((b, e))
        segs = out
    return segs


def is_whole(info, opts):
    return kept(info, opts) == [(0.0, info.duration or 0)]


def out_duration(info, opts):
    return sum(e - s for s, e in kept(info, opts)) * max(1, opts.repeat) / (opts.speed or 1)


def out_size(info, opts, have=None):
    """Output width × height: crop, then scale down to opts.width (or, with a target size and no
    width, to what fits it); even for the video encoders."""
    w, h = (opts.crop[2], opts.crop[3]) if opts.crop else (info.width, info.height)
    width = opts.width or fit_width(info, opts, have)
    if width and width < w:
        w, h = width, h * width / w
    w, h = max(2, int(round(w))), max(2, int(round(h)))
    if opts.fmt in VIDEO_FORMATS:
        w, h = max(2, w // 2 * 2), max(2, h // 2 * 2)
    return w, h


def out_fps(info, opts, have=None):
    """The fps filter's rate, or 0 to keep the source timing."""
    cap = GIF_MAX_FPS if opts.fmt == "gif" else 120
    if opts.fps:
        return min(opts.fps, cap)
    if fit_fps(info, opts, have):
        return FIT_FPS
    if info.fps > cap:
        return cap
    return 0


def can_copy(info, opts):
    """True if the streams can be copied as they are (a container change only)."""
    if not opts.copy or info.image or opts.fmt not in VIDEO_FORMATS or opts.target_size:
        return False
    if info.vcodec not in COPY_VIDEO[opts.fmt] or (opts.codec and info.vcodec != opts.codec):
        return False
    whole = is_whole(info, opts)
    same_size = not opts.crop and (not opts.width or opts.width >= info.width)
    same_rate = not opts.fps or abs(opts.fps - info.fps) < 0.01
    return whole and same_size and same_rate and opts.speed == 1 and opts.repeat <= 1


# -- target file size

def frame_rate(info, opts, have=None):
    """Frames per second of the output (for bitrate math)."""
    return out_fps(info, opts, have) or _source_rate(info, opts)


def _source_rate(info, opts):
    return min(120, (info.fps or 30) * (opts.speed or 1))


def _pixel_budget(info, opts, have, fps):
    """Pixels per frame the target size pays for at this rate, or None without a target."""
    try:
        rates = target_bitrates(info, opts, have)
    except RuntimeError:
        return None
    if not rates:
        return None
    video = rates[0] / opts.squeeze     # a retry squeezes the bitrate, not the size
    return video * 1000 / fps / GOOD_BPP.get(video_encoder(opts.fmt, have, opts.codec), 0.06)


def fit_fps(info, opts, have=None):
    """True if a target size should drop the frame rate to FIT_FPS: no rate chosen, a faster
    source, and the full size doesn't fit at its own rate."""
    if not opts.target_size or opts.fps or opts.fmt not in VIDEO_FORMATS:
        return False
    rate = _source_rate(info, opts)
    budget = _pixel_budget(info, opts, have, rate)
    w, h = (opts.crop[2], opts.crop[3]) if opts.crop else (info.width, info.height)
    return rate > FIT_FPS + 0.5 and budget is not None and w * h > budget


def two_passes(info, opts, have=None):
    """True if this conversion runs in two passes."""
    if not opts.two_pass or opts.fmt not in VIDEO_FORMATS or can_copy(info, opts):
        return False
    enc = video_encoder(opts.fmt, have, opts.codec)
    return enc in TWO_PASS and (bool(opts.target_size) or enc in CRF_TWO_PASS)


def audio_kbps(total_kbps):
    """Sound bitrate for a file of this total bitrate: less for small files, where every bit counts."""
    for floor, kbps in ((2000, 160), (1000, 128), (500, 96), (200, 64)):
        if total_kbps >= floor:
            return kbps
    return 48


def target_bitrates(info, opts, have=None):
    """(video, audio) kbit/s that make opts.target_size, or None without one.  RuntimeError if
    the length is unknown or the size too small for it."""
    if not opts.target_size or opts.fmt not in VIDEO_FORMATS:
        return None
    dur = out_duration(info, opts)
    if not dur:
        raise RuntimeError("Can't aim for a file size: the length of this video is unknown.")
    margin = 0.97 if two_passes(info, opts, have) else 0.93    # container, and the encoder missing a little
    total = opts.target_size * 8 / dur / 1000 * margin
    sound = audio_kbps(total) if _keeps_audio(info, opts) else 0
    video = (total - sound) * opts.squeeze
    if video < MIN_VIDEO_KBPS:
        need = (MIN_VIDEO_KBPS + audio_kbps(MIN_VIDEO_KBPS) * bool(sound)) * 1000 * dur / 8 / margin
        raise RuntimeError(f"{format_size(opts.target_size)} is too small for {dur:.0f} s of video; "
                           f"it needs at least {format_size(need)}.")
    return video, sound


def fit_width(info, opts, have=None):
    """The width that suits the target size: the largest common size (by the short side) with
    enough bits per pixel for the codec.  0 without a target size or when the full size fits."""
    budget = _pixel_budget(info, opts, have, frame_rate(info, opts, have))
    w, h = (opts.crop[2], opts.crop[3]) if opts.crop else (info.width, info.height)
    if budget is None or w * h <= budget:
        return 0
    short = min(w, h)
    sides = [s for s in SHORT_SIDES if s < short] or [short]
    side = next((s for s in sides if w * h * (s / short) ** 2 <= budget), sides[-1])
    return max(2, int(round(w * side / short / 2)) * 2)


def format_size(n):
    if n >= 1e6:
        return f"{n / 1e6:.{2 if n < 1e7 else 1}f} MB"
    return f"{n / 1e3:.0f} kB"


# -- quality: one 1–100 scale for every encoder (80 is the default everywhere)

def gif_colors(q):
    return 256 if q >= 80 else max(8, int(round(256 * (q / 80) ** 2)))


def video_quality_args(enc, q, size, fps):
    bitrate = f"{max(100, int(size[0] * size[1] * (fps or 30) * (0.02 + 0.13 * q / 100) / 1000))}k"
    if enc == "libx264":
        return ["-crf", str(round(51 - q * 0.35)), "-preset", "medium"]
    if enc == "libx265":        # its CRF scale sits ~5 above x264's for the same look
        return ["-crf", str(round(55 - q * 0.35)), "-preset", "medium"]
    if enc == "libvpx-vp9":
        return ["-crf", str(round(63 - q * 0.4)), "-b:v", "0", "-row-mt", "1", "-deadline", "good",
                "-cpu-used", "4"]
    if enc == "libsvtav1":
        return ["-crf", str(round(63 - q * 0.4)), "-preset", "8"]
    if enc == "libaom-av1":
        return ["-crf", str(round(63 - q * 0.4)), "-b:v", "0", "-cpu-used", "6", "-row-mt", "1"]
    if enc == "libvpx":
        return ["-crf", str(max(4, round(63 - q * 0.55))), "-b:v", bitrate]
    if enc == "mpeg4":
        return ["-q:v", str(max(2, round(31 - q * 0.29)))]
    return ["-b:v", bitrate]       # libopenh264: bitrate only


def video_bitrate_args(enc, kbps):
    """Average bitrate instead of a quality: for a target size."""
    b = ["-b:v", f"{max(1, int(kbps))}k"]
    if enc in ("libx264", "libx265"):
        return b + ["-preset", "medium"]
    if enc == "libvpx-vp9":
        return b + ["-row-mt", "1", "-deadline", "good", "-cpu-used", "4"]
    if enc == "libsvtav1":
        return b + ["-preset", "8"]
    if enc == "libaom-av1":
        return b + ["-cpu-used", "6", "-row-mt", "1"]
    return b


def pass_args(enc, n, log):
    if enc == "libx265":
        return ["-x265-params", f"log-level=error:pass={n}:stats={log}.x265"]
    return ["-pass", str(n), "-passlogfile", log]


def describe_quality(fmt, q, have=None, codec=""):
    if fmt == "gif":
        return f"{gif_colors(q)} colors"
    if fmt == "webp":
        return "lossless" if q >= 100 else f"quality {q}"
    enc = video_encoder(fmt, have, codec)
    return f"{ENCODER_LABEL.get(enc, enc or '?')}, quality {q}"


# -- the command line

def _atempo(speed):
    out = []
    while speed > 2.0:
        out.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        out.append("atempo=0.5")
        speed /= 0.5
    out.append(f"atempo={speed:.6g}")
    return ",".join(out)


def commands(info, out_path, opts, log, have=None):
    """The ffmpeg argvs to run in turn: one, or two for two passes (`log`: the pass log prefix)."""
    if two_passes(info, opts, have):
        return [build(info, out_path, opts, have, 1, log), build(info, out_path, opts, have, 2, log)]
    return [build(info, out_path, opts, have)]


def _keeps_audio(info, opts):
    return opts.fmt in VIDEO_FORMATS and info.has_audio and opts.audio and not info.piped


def build(info, out_path, opts, have=None, pass_no=0, log=None):
    """ffmpeg argv (without "ffmpeg") for this conversion.  With info.piped the caller writes raw
    RGBA frames (feed()) to stdin.  pass_no 1/2 with `log` make one of two passes; the first
    writes nothing but the log."""
    have = encoders() if have is None else have
    fmt = opts.fmt
    if fmt not in FORMATS:
        raise RuntimeError(f"Unknown format: {fmt}")
    why = missing_encoder(fmt, have, opts.codec if fmt in VIDEO_FORMATS else "")
    if why:
        raise RuntimeError(why)
    audio = _keeps_audio(info, opts) and pass_no != 1
    rates = target_bitrates(info, opts, have)
    args = ["-y", "-v", "error", "-nostdin"] if not info.piped else ["-y", "-v", "error"]

    if can_copy(info, opts):
        args += ["-i", info.path, "-map", "0:v:0", "-c:v", "copy"]
        if audio:
            args += ["-map", "0:a:0"]
            if info.acodec in COPY_AUDIO[fmt]:
                args += ["-c:a", "copy"]
            else:
                args += _audio_args(fmt, have)
        else:
            args += ["-an"]
        if fmt == "mp4":
            args += ["-movflags", "+faststart"] + (["-tag:v", "hvc1"] if info.vcodec == "hevc" else [])
        return args + ["-f", MUXER[fmt], out_path]

    pieces = kept(info, opts) * max(1, opts.repeat)
    if not pieces:
        raise RuntimeError("Nothing left to convert: every part was cut.")
    if info.piped:
        rate = feed_rate(info, opts, have)
        args += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{info.width}x{info.height}", "-r", f"{rate:g}",
                 "-i", "pipe:0"]
        pieces = [None]
    elif len(pieces) > MAX_PIECES:
        raise RuntimeError(f"Too many pieces ({len(pieces)}); keep fewer parts or repeat less.")
    else:
        whole = is_whole(info, opts)
        for s, e in pieces:
            if not whole:
                args += ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}"]
            args += ["-i", info.path]

    graph = []
    if len(pieces) > 1:
        ins = "".join(f"[{i}:v:0]" + (f"[{i}:a:0]" if audio else "") for i in range(len(pieces)))
        graph.append(f"{ins}concat=n={len(pieces)}:v=1:a={int(audio)}[cv]" + ("[ca]" if audio else ""))
        v, a = "[cv]", "[ca]"
    else:
        v, a = "[0:v:0]", "[0:a:0]"

    chain = []
    if opts.speed != 1:
        chain.append(f"setpts=PTS/{opts.speed:.6g}")
    fps = out_fps(info, opts, have)
    if fps:
        chain.append(f"fps={fps:g}")
    if opts.crop:
        x, y, w, h = (int(round(c)) for c in opts.crop)
        chain.append(f"crop={w}:{h}:{x}:{y}")
    w, h = out_size(info, opts, have)
    cw, ch = (int(round(opts.crop[2])), int(round(opts.crop[3]))) if opts.crop else (info.width, info.height)
    if (w, h) != (cw, ch):
        chain.append(f"scale={w}:{h}:flags=lanczos")

    if fmt == "gif":
        chain.append(f"split[a][b];[a]palettegen=stats_mode=diff:max_colors={gif_colors(opts.quality)}[p];"
                     "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
        graph.append(f"{v}{','.join(chain)}[vout]")
    elif fmt == "webp":
        graph.append(f"{v}{','.join(chain or ['null'])}[vout]")
    else:
        rgb = info.image or info.piped
        if info.alpha:
            rate = fps or min(60, max(1, round(info.fps or 30)))
            chain.append("format=rgba")
            graph.append(f"{v}{','.join(chain)}[fg]")
            graph.append(f"color=c=0x{opts.background.lstrip('#')[:6]}:s={w}x{h}:r={rate:g}[bg]")
            graph.append("[bg][fg]overlay=shortest=1:format=auto,"
                         + ("scale=out_color_matrix=bt709:out_range=tv," if rgb else "") + "format=yuv420p[vout]")
        else:
            if rgb:
                chain.append("scale=out_color_matrix=bt709:out_range=tv")
            chain.append("format=yuv420p")
            graph.append(f"{v}{','.join(chain)}[vout]")
    if audio:
        graph.append(f"{a}{_atempo(opts.speed) if opts.speed != 1 else 'anull'}[aout]")

    args += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
    if fmt == "gif":
        args += ["-loop", "0", "-an"]
    elif fmt == "webp":
        lossless = opts.quality >= 100
        args += ["-c:v", "libwebp_anim", "-lossless", str(int(lossless)), "-quality",
                 str(100 if lossless else opts.quality), "-compression_level", "4", "-loop", "0", "-an"]
    else:
        enc = video_encoder(fmt, have, opts.codec)
        if rates:
            args += ["-c:v", enc] + video_bitrate_args(enc, rates[0])
        else:
            args += ["-c:v", enc] + video_quality_args(enc, opts.quality, (w, h), fps or info.fps)
        if pass_no:
            args += pass_args(enc, pass_no, log)
        elif enc == "libx265":
            args += ["-x265-params", "log-level=error"]
        if enc == "libx265" and fmt == "mp4":
            args += ["-tag:v", "hvc1"]      # Apple players want this tag for HEVC in MP4
        if info.image or info.piped:
            args += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
        if pass_no == 1:
            return args + ["-an", "-f", "null", os.devnull]
        if audio:
            args += ["-map", "[aout]"] + _audio_args(fmt, have, rates[1] if rates else 160, bool(rates))
        else:
            args += ["-an"]
        if fmt == "mp4":
            args += ["-movflags", "+faststart"]
    return args + ["-f", MUXER[fmt], out_path]


def _audio_args(fmt, have, kbps=160, exact=False):
    enc = audio_encoder(fmt, have)
    if not enc:
        return ["-an"]
    exact = exact and enc == "libopus"      # Opus' default VBR can run 20 % over (on a plain tone)
    return ["-c:a", enc, "-b:a", f"{kbps}k"] + (["-vbr", "constrained"] if exact else [])


# -- raw frames for the Qt fallback

def feed_rate(info, opts, have=None):
    return out_fps(info, opts, have) or min(GIF_MAX_FPS, max(1, round(info.fps or 10)))


def feed(frames, segments, repeat, rate, write):
    """Write frames at a constant `rate`, keeping only source times inside `segments`.
    frames: a callable returning (frame, delay ms) pairs; write(frame) is called per output frame."""
    step = 1.0 / rate
    n = 0
    for _ in range(max(1, repeat)):
        t, k = 0.0, 0
        for frame, delay in frames():
            end = t + delay / 1000
            while k * step < end - 1e-9:
                tick = k * step
                if any(s <= tick < e for s, e in segments):
                    write(frame)
                    n += 1
                k += 1
            t = end
    return n


# -- black bars

def detect_bars(info, samples=5):
    """The picture without black bars around it, (x, y, w, h), or None if there are none.
    Looks at a few points through the video and keeps the largest box, so dark scenes don't
    cut away real picture."""
    if info.piped:
        return None
    dur = info.duration or 0
    boxes = []
    for i in range(samples):
        t = dur * (i + 0.5) / samples if dur else 0
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "info"]
        if t and not info.image:
            cmd += ["-ss", f"{t:.2f}"]
        cmd += ["-i", info.path, "-vf", "cropdetect=limit=0.094:round=2:reset=0", "-frames:v", "5",
                "-an", "-f", "null", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        if found:
            w, h, x, y = map(int, found[-1])
            if w > 0 and h > 0:
                boxes.append((x, y, x + w, y + h))
        if info.image:
            break
    return union_box(boxes, info.width, info.height)


def union_box(boxes, width, height):
    if not boxes:
        return None
    x0, y0 = max(0, min(b[0] for b in boxes)), max(0, min(b[1] for b in boxes))
    x1, y1 = min(width, max(b[2] for b in boxes)), min(height, max(b[3] for b in boxes))
    if x1 - x0 < 16 or y1 - y0 < 16 or (x0, y0, x1, y1) == (0, 0, width, height):
        return None
    return x0, y0, x1 - x0, y1 - y0


# -- running

def output_path(src, fmt, folder=None):
    """src's name with the new extension in its folder (or `folder`), never overwriting."""
    base = os.path.splitext(os.path.basename(src))[0]
    folder = folder or os.path.dirname(os.path.abspath(src))
    out, n = os.path.join(folder, f"{base}.{fmt}"), 1
    while os.path.exists(out) or os.path.abspath(out) == os.path.abspath(src):
        out, n = os.path.join(folder, f"{base}-{n}.{fmt}"), n + 1
    return out


class Cancelled(Exception):
    pass


def run(src, out_path, opts, info=None, progress=None, cancel=None):
    """Convert (blocking; call it from a worker thread).  progress(fraction) is called now and
    then; set the threading.Event `cancel` to stop.  Writes to a temporary name and renames at
    the end, so a failed or cancelled run leaves nothing behind.  Returns out_path."""
    if not available():
        raise RuntimeError("Converting needs ffmpeg.")
    info = info or probe(src)
    folder = os.path.dirname(os.path.abspath(out_path))
    tmp = os.path.join(folder, "." + os.path.basename(out_path) + ".part")
    logs = tempfile.mkdtemp(prefix="snappie-passes-")
    try:
        for attempt in range(2):
            cmds = commands(info, tmp, opts, os.path.join(logs, "pass"))
            for n, args in enumerate(cmds):
                part = (lambda f, n=n: progress((n + f) / len(cmds))) if progress else None
                _run_ffmpeg(args, info, opts, part, cancel)
            size = os.path.getsize(tmp)
            if not opts.target_size or size <= opts.target_size or attempt:
                break
            opts = copy.copy(opts)      # too big: once more, with that much less for the video
            opts.squeeze *= opts.target_size / size * 0.97
        os.replace(tmp, out_path)
        return out_path
    finally:
        shutil.rmtree(logs, ignore_errors=True)
        if os.path.exists(tmp):
            os.unlink(tmp)


def _run_ffmpeg(args, info, opts, progress, cancel):
    total = out_duration(info, opts) or 0
    proc = subprocess.Popen(["ffmpeg", "-progress", "pipe:1", "-nostats", *args],
                            stdin=subprocess.PIPE if info.piped else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
    err = []
    threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True).start()
    if info.piped:
        threading.Thread(target=_pipe_frames, args=(proc, info, opts), daemon=True).start()
    try:
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                break
            m = re.match(rb"out_time_us=(\d+)", line)
            if m and progress and total:
                progress(min(1.0, int(m.group(1)) / 1e6 / total))
        code = proc.wait()
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if code != 0:
            text = (err[0] if err else b"").decode(errors="replace").strip().splitlines()
            raise RuntimeError("ffmpeg failed: " + (text[-1] if text else f"exit code {code}"))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _pipe_frames(proc, info, opts):
    def write(img):
        bits = img.constBits()
        bits.setsize(img.sizeInBytes())
        proc.stdin.write(bytes(bits))
    try:
        feed(lambda: qt_frames(info.path), kept(info, opts), opts.repeat, feed_rate(info, opts), write)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
