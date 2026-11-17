import os
import shutil
import subprocess

import pytest

from snappie import convert as c

ALL = {"libx264", "libopenh264", "mpeg4", "libvpx-vp9", "libsvtav1", "libvpx", "libwebp_anim", "aac", "libopus"}


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
