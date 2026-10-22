import json
from types import SimpleNamespace

import pytest

from snappie import cli, recorder, settings


@pytest.fixture
def encoders(monkeypatch):
    """Pretend only the given GStreamer elements exist."""
    def use(*names):
        monkeypatch.setattr(recorder, "have", lambda f: f in names)
    return use


# -- picking an encoder --

def test_hardware_first(encoders):
    encoders("vah265enc", "vaav1enc", "vp9enc")
    assert recorder.pick_codec("hevc") == "hevc"
    assert recorder.pick_codec("av1") == "av1"


def test_vp9_on_the_cpu_when_the_gpu_has_none(encoders):
    encoders("vah265enc", "vaav1enc", "vp9enc", "vp8enc")   # AMD
    assert recorder.pick_codec("vp9") == "vp9-sw"
    assert recorder.pick_codec("vp8") == "vp8-sw"
    encoders("vavp9enc", "vp9enc")                           # Intel
    assert recorder.pick_codec("vp9") == "vp9"


def test_webm_only_takes_webm_codecs(encoders):
    encoders("vah265enc", "vah264enc", "vaav1enc", "vp9enc", "vp8enc")
    assert recorder.pick_codec("hevc", webm=True) == "vp9-sw"
    assert recorder.pick_codec("av1", webm=True) == "av1"
    assert recorder.pick_codec("vp8", webm=True) == "vp8-sw"


def test_libvpx_is_the_last_resort(encoders):
    encoders("vp9enc")   # e.g. NVIDIA without VA-API
    assert recorder.pick_codec("hevc") == "vp9-sw"
    encoders()
    with pytest.raises(RuntimeError):
        recorder.pick_codec("hevc")


def test_vp8_cannot_go_in_mp4():
    assert recorder.final_container("vp8-sw", "mp4") == "webm"
    assert recorder.final_container("vp8-sw", "mkv") == "mkv"
    assert recorder.final_container("vp9-sw", "mp4") == "mp4"


# -- encoder settings --

def test_libvpx_realtime_with_a_quality_floor():
    d = recorder._encoder_desc("vp9-sw", "good", None, 1920 * 1080)
    assert d.startswith("vp9enc ") and d.endswith("! vp9parse")
    assert "deadline=1" in d and "end-usage=vbr" in d and "min-quantizer=12" in d
    assert "max-quantizer" not in d


def test_libvpx_constant_quantizer():
    d = recorder._encoder_desc("vp8-sw", "good", 99, 1920 * 1080)
    assert d.startswith("vp8enc ") and "min-quantizer=63 max-quantizer=63" in d


def test_cpu_encoders_convert_on_the_cpu():
    assert recorder._convert("vp9-sw").startswith("videoconvert")
    assert "vapostproc" in recorder._convert("hevc")


# -- crop stays on encoder blocks --

class _Crop:
    def __init__(self):
        self.props = {}

    def set_property(self, k, v):
        self.props[k] = v


def _crop_for(codec, W, H, crop):
    rec = SimpleNamespace(codec=codec, _crop=crop, crop=_Crop())
    recorder.Recorder._apply_crop(rec, W, H)
    p = rec.crop.props
    return W - p["left"] - p["right"], H - p["top"] - p["bottom"]


def test_window_shrinking_keeps_av1_blocks():
    # a 1280x720 AV1 area in a window that shrinks to an odd 1203x701
    assert _crop_for("av1", 1203, 701, (0, 0, 1280, 720)) == (1152, 688)


def test_window_shrinking_keeps_even_sizes():
    assert _crop_for("hevc", 1203, 701, (0, 0, 1280, 720)) == (1202, 700)
    assert _crop_for("vp9-sw", 1203, 701, (0, 0, 1280, 720)) == (1202, 700)


def test_unchanged_window_keeps_the_crop():
    assert _crop_for("av1", 1920, 1080, (64, 32, 1280, 720)) == (1280, 720)


# -- settings and command line --

def test_container_falls_back_to_the_old_mp4_switch(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS", str(path))
    assert settings.get("rec.container") == "mp4"
    path.write_text(json.dumps({"rec.mp4": False}))
    assert settings.get("rec.container") == "mkv"
    path.write_text(json.dumps({"rec.mp4": False, "rec.container": "webm"}))
    assert settings.get("rec.container") == "webm"


def test_cli_container_and_codecs():
    ns = cli.build_parser().parse_args(["record", "-c", "vp9", "--container", "webm"])
    assert ns.codec == "vp9" and ns.container == "webm"
