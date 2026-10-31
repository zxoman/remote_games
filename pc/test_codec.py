"""Tests for the codec selector (``codec = auto``) in pc/send_mjpeg.py.

The point of ``auto`` is that the same committed config works on both machines:
Linux without a GStreamer H.264 encoder must silently use MJPEG instead of
failing to start, and a missing typelib must not crash the sender.
"""

import pytest

import send_mjpeg as s


def test_auto_picks_h264_when_openh264_is_available(monkeypatch):
    monkeypatch.setattr(s, "h264_encoder_available", lambda: True)
    assert s.resolve_codec({"codec": "auto"}) == "h264"


def test_auto_falls_back_to_mjpeg_without_gstreamer(monkeypatch):
    monkeypatch.setattr(s, "h264_encoder_available", lambda: False)
    assert s.resolve_codec({"codec": "auto"}) == "mjpeg"


@pytest.mark.parametrize("raw,expected", [
    ("h264", "h264"),
    (" H264 ", "h264"),
    ("mjpeg", "mjpeg"),
    ("MJPEG", "mjpeg"),
])
def test_explicit_values_pass_through(raw, expected):
    assert s.resolve_codec({"codec": raw}) == expected


def test_unknown_codec_is_a_clear_error():
    with pytest.raises(SystemExit) as e:
        s.resolve_codec({"codec": "vp9"})
    assert "auto, h264 or mjpeg" in str(e.value)


@pytest.mark.filterwarnings("ignore:.*unix_signal_add_full.*")
def test_encoder_probe_never_raises():
    # Whatever this host has, the probe must return a bool (no GStreamer here
    # is a valid, non-fatal answer).
    assert isinstance(s.h264_encoder_available(), bool)
