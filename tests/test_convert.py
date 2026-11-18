import os
import shutil
import subprocess

import pytest

from snappie import convert as c

ALL = {"libx264", "libopenh264", "mpeg4", "libvpx-vp9", "libsvtav1", "libvpx", "libwebp_anim", "aac", "libopus",
       "libx265"}


def video(**kw):
    base = dict(path="/v/clip.mkv", width=1920, height=1080, duration=10.0, fps=30.0, vcodec="h264", acodec="aac")
    base.update(kw)
    return c.Info(**base)


def gif(**kw):
    base = dict(path="/v/anim.gif", width=301, height=201, duration=3.0, fps=10.0, vcodec="gif", alpha=True,
                frames=30, image=True)
    base.update(kw)
    return c.Info(**base)


def graph(args):
    return args[args.index("-filter_complex") + 1]


def test_parse_probe_rotated_phone_video():
    data = {"format": {"duration": "4.5"}, "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
         "avg_frame_rate": "30000/1001", "side_data_list": [{"rotation": -90}]},
        {"codec_type": "audio", "codec_name": "aac"}]}
    info = c.parse_probe("/v/phone.mp4", data)
    assert (info.width, info.height) == (1080, 1920)
    assert info.fps == pytest.approx(29.97) and info.duration == 4.5
    assert info.has_audio and not info.alpha and not info.image


def test_parse_probe_webp_without_duration_and_gray_is_not_alpha():
    data = {"format": {}, "streams": [{"codec_type": "video", "codec_name": "webp_anim", "width": 200,
                                        "height": 150, "pix_fmt": "argb", "avg_frame_rate": "10/1",
                                        "nb_read_frames": "30"}]}
    info = c.parse_probe("/v/a.webp", data, image=True)
    assert info.duration == 3.0 and info.alpha and not info.has_audio
    gray = {"streams": [{"codec_type": "video", "width": 8, "height": 8, "pix_fmt": "gray"}]}
    assert not c.parse_probe("/v/g.mkv", gray).alpha
    with pytest.raises(RuntimeError):
        c.parse_probe("/v/sound.mka", {"streams": [{"codec_type": "audio"}]})


def test_kept_clamps_and_drops_empty_pieces():
    info = video(duration=10.0)
    assert c.kept(info, c.Options()) == [(0.0, 10.0)]
    o = c.Options(segments=[(-1, 2), (5, 5), (8, None), (9, 20)])
    assert c.kept(info, o) == [(0.0, 2.0), (8.0, 10.0), (9.0, 10.0)]
    assert c.out_duration(info, c.Options(segments=[(0, 2), (4, 6)], speed=2, repeat=3)) == pytest.approx(6.0)
    assert c.is_whole(video(duration=0), c.Options())     # unknown length: the whole file


def test_segments_from_start_end_and_cuts():
    assert c.segments_from() is None
    assert c.segments_from(start=2) == [(2, None)]
    assert c.segments_from(1, 10, [(3, 4), (6, 12)]) == [(1, 3), (4, 6)]
    assert c.segments_from(cuts=[(0, 2), (5, 6)]) == [(2, 5), (6, None)]
    assert c.kept(video(duration=10.0), c.Options(segments=c.segments_from(cuts=[(5, 6)]))) == [(0, 5), (6, 10)]


def test_out_size_crops_scales_and_keeps_video_sizes_even():
    info = gif()
    assert c.out_size(info, c.Options("gif")) == (301, 201)
    assert c.out_size(info, c.Options("mp4")) == (300, 200)
    assert c.out_size(video(), c.Options("mp4", width=640)) == (640, 360)
    assert c.out_size(video(), c.Options("mp4", width=4000)) == (1920, 1080)       # never scaled up
    assert c.out_size(video(), c.Options("webm", crop=(0, 140, 1920, 800), width=961)) == (960, 400)


def test_out_fps_caps_gifs():
    assert c.out_fps(video(fps=60), c.Options("gif")) == c.GIF_MAX_FPS
    assert c.out_fps(video(fps=60), c.Options("gif", fps=15)) == 15
    assert c.out_fps(video(fps=60), c.Options("mp4")) == 0          # keep the source timing


def test_copy_only_when_nothing_changes():
    assert c.can_copy(video(), c.Options("mp4"))
    assert not c.can_copy(video(), c.Options("mp4", copy=False))
    assert not c.can_copy(video(vcodec="prores"), c.Options("mp4"))
    assert c.can_copy(video(vcodec="vp9"), c.Options("webm"))
    for change in (dict(segments=[(1, 5)]), dict(crop=(0, 0, 100, 100)), dict(width=640), dict(fps=15),
                   dict(speed=2), dict(repeat=2)):
        assert not c.can_copy(video(), c.Options("mp4", **change)), change
    assert not c.can_copy(gif(), c.Options("mp4"))


def test_build_copy_reencodes_only_incompatible_audio():
    args = c.build(video(acodec="pcm_s16le"), "/o.mp4", c.Options("mp4"), ALL)
    assert args[args.index("-c:v") + 1] == "copy" and args[args.index("-c:a") + 1] == "aac"
    args = c.build(video(vcodec="hevc"), "/o.mp4", c.Options("mp4"), ALL)
    assert "hvc1" in args and args[args.index("-c:a") + 1] == "copy"
    assert "-an" in c.build(video(), "/o.mp4", c.Options("mp4", audio=False), ALL)


def test_build_cuts_open_one_input_per_piece_and_concat():
    o = c.Options("mp4", segments=[(1, 3), (6, 7.5)], repeat=2, speed=2)
    args = c.build(video(), "/o.mp4", o, ALL)
    assert args.count("-i") == 4
    i = args.index("-ss")
    assert args[i:i + 4] == ["-ss", "1.000", "-t", "2.000"]
    g = graph(args)
    assert "concat=n=4:v=1:a=1[cv][ca]" in g and "setpts=PTS/2" in g and "atempo=2" in g
    assert args[args.index("-c:v") + 1] == "libx264" and "+faststart" in args


def test_build_gif_from_video_and_whole_file_has_no_seek():
    args = c.build(video(), "/o.gif", c.Options("gif", fps=15, width=960, quality=40), ALL)
    assert "-ss" not in args and args.count("-i") == 1
    g = graph(args)
    assert "fps=15" in g and "scale=960:540" in g and f"max_colors={c.gif_colors(40)}" in g
    assert "-an" in args and args[-3:-1] == ["-f", "gif"]


def test_build_transparent_animation_to_video_gets_a_background():
    args = c.build(gif(), "/o.mp4", c.Options("mp4", background="#336699"), ALL)
    g = graph(args)
    assert "color=c=0x336699:s=300x200:r=10[bg]" in g and "overlay=shortest=1" in g
    assert "out_color_matrix=bt709" in g and "bt709" in args
    assert "-an" in args


def test_build_falls_back_to_the_encoders_there_are():
    have = {"libopenh264", "mpeg4", "libvpx", "aac", "libvorbis", "libwebp_anim"}
    args = c.build(video(), "/o.mp4", c.Options("mp4", copy=False), have)
    assert args[args.index("-c:v") + 1] == "libopenh264" and "-b:v" in args
    args = c.build(video(), "/o.webm", c.Options("webm"), have)
    assert args[args.index("-c:v") + 1] == "libvpx" and args[args.index("-c:a") + 1] == "libvorbis"
    with pytest.raises(RuntimeError, match="no MP4"):
        c.build(video(), "/o.mp4", c.Options("mp4", copy=False), {"aac"})
    with pytest.raises(RuntimeError, match="WebP"):
        c.build(video(), "/o.webp", c.Options("webp"), {"aac"})


def test_build_piped_webp_reads_raw_frames():
    info = gif(path="/v/a.webp", piped=True, width=200, height=150)
    args = c.build(info, "/o.mp4", c.Options("mp4", segments=[(0, 1), (2, 3)], repeat=3), ALL)
    assert args.count("-i") == 1 and "pipe:0" in args and "200x150" in args and "concat" not in graph(args)
    assert "-nostdin" not in args


def test_nothing_left():
    with pytest.raises(RuntimeError, match="Nothing left"):
        c.build(video(), "/o.mp4", c.Options("mp4", segments=[(20, 30)]), ALL)


def test_quality_scales():
    assert c.gif_colors(80) == c.gif_colors(100) == 256 and c.gif_colors(40) == 64 and c.gif_colors(1) == 8
    x264 = lambda q: int(c.video_quality_args("libx264", q, (640, 360), 30)[1])
    assert x264(100) < x264(80) == 23 < x264(20)


def test_atempo_chains_outside_half_to_double():
    assert c._atempo(4) == "atempo=2.0,atempo=2"
    assert c._atempo(0.25) == "atempo=0.5,atempo=0.5"
    assert c._atempo(1.5) == "atempo=1.5"


def test_feed_keeps_segments_at_a_constant_rate():
    frames = lambda: iter([("a", 500), ("b", 500), ("c", 1000)])      # 2 s
    out = []
    n = c.feed(frames, [(0.0, 2.0)], 1, 4, out.append)
    assert n == 8 and "".join(out) == "aabbcccc"
    out.clear()
    c.feed(frames, [(0.25, 0.75), (1.5, 2.0)], 2, 4, out.append)
    assert "".join(out) == "abcc" * 2


def test_union_box_ignores_no_bars():
    assert c.union_box([], 100, 100) is None
    assert c.union_box([(0, 0, 100, 100)], 100, 100) is None
    assert c.union_box([(0, 10, 100, 90), (0, 12, 100, 92)], 100, 100) == (0, 10, 100, 82)


def test_output_path_never_overwrites(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"")
    assert c.output_path(str(src), "gif") == str(tmp_path / "clip.gif")
    (tmp_path / "clip.gif").write_bytes(b"")
    assert c.output_path(str(src), "gif") == str(tmp_path / "clip-1.gif")
    assert c.output_path(str(src), "mp4") == str(tmp_path / "clip-1.mp4")       # not the source itself


@pytest.mark.skipif(not c.available(), reason="needs ffmpeg")
def test_run_gif_to_mp4_with_cuts(tmp_path):
    src = str(tmp_path / "a.gif")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=64x48:r=10:d=2", src], check=True)
    info = c.probe(src)
    assert info.image and info.frames == 20
    if not c.video_encoder("mp4"):
        pytest.skip("no MP4 encoder")
    o = c.Options("mp4", segments=[(0, 0.5), (1.0, 2.0)])
    out = c.run(src, c.output_path(src, "mp4"), o, info)
    assert os.path.exists(out) and not [f for f in os.listdir(tmp_path) if f.endswith(".part")]
    assert c.probe(out).duration == pytest.approx(1.5, abs=0.15)


def vcodec(args):
    return args[args.index("-c:v") + 1]


def test_chosen_codec_picks_its_encoder():
    assert vcodec(c.build(video(), "/o.webm", c.Options("webm", codec="vp8"), ALL)) == "libvpx"
    assert vcodec(c.build(video(), "/o.webm", c.Options("webm", codec="av1"), ALL)) == "libsvtav1"
    assert vcodec(c.build(video(), "/o.webm", c.Options("webm"), ALL)) == "libvpx-vp9"     # automatic
    args = c.build(video(), "/o.mp4", c.Options("mp4", codec="hevc", copy=False), ALL)
    assert vcodec(args) == "libx265" and "hvc1" in args
    assert vcodec(c.build(video(), "/o.mp4", c.Options("mp4", codec="h264", copy=False),
                          ALL - {"libx264"})) == "libopenh264"
    assert c.describe_quality("webm", 80, ALL, "vp8") == "VP8, quality 80"


def test_chosen_codec_copies_only_its_own_streams():
    assert c.can_copy(video(vcodec="vp9"), c.Options("webm"))
    assert not c.can_copy(video(vcodec="vp9"), c.Options("webm", codec="vp8"))
    assert c.can_copy(video(vcodec="vp8"), c.Options("webm", codec="vp8"))
    assert c.can_copy(video(vcodec="hevc"), c.Options("mp4", codec="hevc"))


def test_chosen_codec_missing_or_wrong_for_the_format():
    assert "VP8" in c.missing_encoder("webm", ALL - {"libvpx"}, "vp8")
    assert "can't hold VP8" in c.missing_encoder("mp4", ALL, "vp8")
    with pytest.raises(RuntimeError, match="no HEVC encoder"):
        c.build(video(), "/o.mp4", c.Options("mp4", codec="hevc", copy=False), ALL - {"libx265"})
    assert c.missing_encoder("webm", ALL, "vp8") == ""


def test_target_size_sets_bitrates_and_never_copies():
    o = c.Options("mp4", target_size=8_000_000, two_pass=True)
    video_kbps, audio_kbps = c.target_bitrates(video(), o, ALL)          # 10 s: plenty
    assert audio_kbps == 160 and video_kbps == pytest.approx(8e6 * 8 / 10 / 1000 * 0.97 - 160)
    assert not c.can_copy(video(), o)
    assert c.target_bitrates(video(), c.Options("mp4"), ALL) is None
    assert c.target_bitrates(video(), c.Options("gif", target_size=8_000_000), ALL) is None
    assert c.target_bitrates(video(), c.Options("mp4", target_size=8_000_000, audio=False), ALL)[1] == 0
    args = c.build(video(), "/o.webm", c.Options("webm", target_size=1_000_000), ALL)
    assert "-crf" not in args and args[args.index("-b:a") + 1] == "96k" and "constrained" in args
    with pytest.raises(RuntimeError, match="too small for 10 s.*at least"):
        c.target_bitrates(video(), c.Options("mp4", target_size=20_000), ALL)
    with pytest.raises(RuntimeError, match="unknown"):
        c.target_bitrates(video(duration=0), c.Options("mp4", target_size=8_000_000), ALL)


def test_target_size_picks_frame_rate_and_size():
    long60 = video(duration=60.0, fps=60.0)
    o = c.Options("mp4", target_size=8_000_000, two_pass=True)
    assert c.fit_fps(long60, o, ALL) and c.out_fps(long60, o, ALL) == 30
    assert c.out_size(long60, o, ALL) == (854, 480)
    args = c.build(long60, "/o.mp4", o, ALL)
    assert "fps=30" in graph(args) and "scale=854:480" in graph(args)
    assert c.out_size(long60, c.Options("mp4", target_size=8_000_000, codec="av1", two_pass=True), ALL)[1] > 480
    # what the user chose stays
    assert c.out_size(long60, c.Options("mp4", target_size=8_000_000, width=1280), ALL) == (1280, 720)
    assert c.out_fps(long60, c.Options("mp4", target_size=8_000_000, fps=60), ALL) == 60
    # a size that fits keeps everything
    big = c.Options("mp4", target_size=100_000_000)
    assert c.out_size(long60, big, ALL) == (1920, 1080) and c.out_fps(long60, big, ALL) == 0


def test_two_passes():
    o = c.Options("mp4", target_size=8_000_000, two_pass=True)
    first, second = c.commands(video(), "/o.mp4", o, "/tmp/log", ALL)
    assert first[first.index("-pass") + 1] == "1" and first[-4:] == ["-an", "-f", "null", os.devnull]
    assert "-map" in first and "[aout]" not in first and "[aout]" not in graph(first)
    assert second[second.index("-pass") + 1] == "2" and "/tmp/log" in second and second[-1] == "/o.mp4"
    assert "[aout]" in second
    x265 = c.commands(video(), "/o.mp4", c.Options("mp4", codec="hevc", target_size=8_000_000, two_pass=True),
                      "/tmp/log", ALL)
    assert [a[a.index("-x265-params") + 1] for a in x265] == [
        "log-level=error:pass=1:stats=/tmp/log.x265", "log-level=error:pass=2:stats=/tmp/log.x265"]
    # one pass: not asked, OpenH264 can't, x264 by quality gains nothing, copying needs none
    assert len(c.commands(video(), "/o.mp4", c.Options("mp4", target_size=8_000_000), "/l", ALL)) == 1
    assert len(c.commands(video(), "/o.mp4", c.Options("mp4", target_size=8_000_000, two_pass=True), "/l",
                          ALL - {"libx264"})) == 1
    assert len(c.commands(video(), "/o.mp4", c.Options("mp4", copy=False, two_pass=True), "/l", ALL)) == 1
    assert len(c.commands(video(vcodec="vp9"), "/o.webm", c.Options("webm", two_pass=True), "/l", ALL)) == 1
    assert len(c.commands(video(), "/o.webm", c.Options("webm", two_pass=True), "/l", ALL)) == 2   # VP9 CRF


@pytest.mark.skipif(not c.available(), reason="needs ffmpeg")
def test_run_hits_the_target_size_in_two_passes(tmp_path):
    if "libx264" not in c.encoders():
        pytest.skip("no libx264")
    src = str(tmp_path / "a.mkv")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4", "-f", "lavfi",
                    "-i", "sine=d=4", "-shortest", "-c:v", "libx264", "-crf", "10", src], check=True)
    o = c.Options("mp4", codec="h264", target_size=300_000, two_pass=True)
    out = c.run(src, str(tmp_path / "o.mp4"), o, c.probe(src))
    assert 0.85 * 300_000 < os.path.getsize(out) <= 300_000
    assert not [f for f in os.listdir(tmp_path) if "log" in f or f.endswith(".part")]


def test_run_retries_once_when_the_file_comes_out_too_big(tmp_path, monkeypatch):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"")
    runs = []

    def fake(args, info, opts, progress, cancel):        # 10 % over the bitrate, every time
        runs.append(opts.squeeze)
        if args[-1] != os.devnull:
            kbps = c.target_bitrates(info, opts, ALL)[0] + 160
            with open(args[-1], "wb") as f:
                f.write(b"x" * int(kbps * 1000 / 8 * info.duration * 1.1))
    monkeypatch.setattr(c, "_run_ffmpeg", fake)
    monkeypatch.setattr(c, "available", lambda: True)
    monkeypatch.setattr(c, "encoders", lambda: ALL)
    o = c.Options("mp4", target_size=8_000_000, two_pass=True)
    out = c.run(str(src), str(tmp_path / "o.mp4"), o, video(path=str(src)))
    assert len(runs) == 4 and runs[0] == runs[1] == 1 and runs[2] < 0.95      # 2 passes, twice
    assert os.path.getsize(out) <= 8_000_000 and o.squeeze == 1                 # the caller's opts untouched
