#!/usr/bin/env python3
"""Reverse Remote Play — Wayland/PipeWire H.264 screen capture.

Phase 3 counterpart to grab_wayland.py: captures a monitor through the
xdg-desktop-portal ScreenCast API, encodes it as H.264 with GStreamer's
openh264 element, and writes a length-prefixed access-unit stream to stdout:

    [uint32 little-endian length][Annex-B access unit] ...

The length prefix lets send_mjpeg.py split frames without parsing H.264 itself.
Each access unit (AU) becomes one RP4V frame on the wire; send_mjpeg.py tags the
AU containing an IDR slice with VIDEO_FLAG_KEYFRAME so the PS4 decoder knows
where it can resynchronise after packet loss.

The portal/token plumbing is shared with grab_wayland.py.

Examples
--------
    # Continuous H.264 stream on stdout (what send_mjpeg.py uses)
    python3 grab_h264.py --width 1280 --height 720 --fps 30 --bitrate 2500000

    # Validate without a display (synthetic pattern):
    python3 grab_h264.py --test-pattern --test-out /tmp/frame.h264
"""

import argparse
import os
import struct
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

# GstVideo exposes the canonical GstForceKeyUnit constructor. It lives in
# gst-plugins-base; fall back to building the equivalent custom upstream event
# by hand if the typelib is not installed.
try:
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo  # noqa: E402
except Exception:  # pragma: no cover - depends on the host's GI packages
    GstVideo = None

from grab_wayland import _log, first_node_id, open_session, save_token  # noqa: E402


_KEYFRAME_COMMANDS = {"keyframe", "kf", "idr", "k"}


def _force_key_unit_event():
    """Return an upstream GstForceKeyUnit event (ask the encoder for an IDR)."""
    if GstVideo is not None:
        return GstVideo.video_event_new_upstream_force_key_unit(
            Gst.CLOCK_TIME_NONE, True, 0)
    # Explicit GValue types matter: set_value() would overflow on CLOCK_TIME_NONE.
    st = Gst.Structure.from_string(
        "GstForceKeyUnit,running-time=(guint64)18446744073709551615,"
        "all-headers=(boolean)true,count=(uint)0")[0]
    return Gst.Event.new_custom(Gst.EventType.CUSTOM_UPSTREAM, st)


def _send_force_key_unit(pipeline):
    pipeline.send_event(_force_key_unit_event())
    return False  # one-shot GLib idle callback


def _set_encoder_bitrate(enc, bitrate):
    """Apply a live bitrate change (no pipeline restart)."""
    try:
        enc.set_property("bitrate", int(bitrate))
        _log(f"bitrate -> {int(bitrate)}")
    except Exception as e:  # property missing / not writable
        _log(f"bitrate change to {bitrate} failed: {e}")
    return False  # one-shot GLib idle callback


def _start_control_reader(pipeline, loop, enc):
    """Read newline-delimited commands from stdin.

    ``keyframe``/``kf``/``idr``/``k`` force an IDR immediately;
    ``bitrate <bits/s>`` changes the encoder target live;
    ``quit`` stops the pipeline. Runs in a daemon thread and marshals GStreamer
    calls onto the GLib main loop with ``idle_add`` so they never race the
    appsink callback. This is the control channel used by ``send_mjpeg.py``.
    """
    def worker():
        try:
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd in _KEYFRAME_COMMANDS:
                    GLib.idle_add(_send_force_key_unit, pipeline)
                elif cmd.startswith("bitrate"):
                    parts = cmd.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        GLib.idle_add(_set_encoder_bitrate, enc, int(parts[1]))
                elif cmd in ("quit", "exit", "q"):
                    GLib.idle_add(loop.quit)
                    return
        except (ValueError, OSError):
            pass  # stdin closed: the pipeline keeps running normally

    threading.Thread(target=worker, name="h264-control", daemon=True).start()


def build_pipeline(fd, node_id, width, height, fps, bitrate, gop, complexity,
                   profile, test_pattern):
    enc = (
        f"openh264enc name=enc bitrate={bitrate} gop-size={gop} "
        f"complexity={complexity} rate-control=bitrate"
    )
    # profile is a pad capability of openh264enc, not an element property, so it
    # is negotiated through a capsfilter on its src pad.
    tail = (
        f"{enc} ! video/x-h264,profile={profile} ! h264parse config-interval=-1 ! "
        f"video/x-h264,stream-format=byte-stream,alignment=au ! "
        f"appsink name=sink emit-signals=true max-buffers=2 drop=true"
    )
    if test_pattern:
        src = (
            f"videotestsrc is-live=true ! videoconvert ! videoscale ! "
            f"video/x-raw,width={width},height={height},framerate={fps}/1"
        )
    else:
        src = (
            f"pipewiresrc fd={fd} path={node_id} do-timestamp=true ! "
            f"videorate ! video/x-raw,framerate={fps}/1 ! "
            f"videoconvert ! videoscale add-borders=true ! "
            f"video/x-raw,width={width},height={height}"
        )
    desc = src + " ! " + tail
    _log(f"pipeline: {desc}")
    return Gst.parse_launch(desc)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Wayland/PipeWire screen capture -> length-prefixed H.264 AUs on stdout")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--bitrate", type=int, default=8_000_000,
                    help="target bitrate in bits/s")
    ap.add_argument("--gop", type=int, default=0,
                    help="keyframe interval in frames (0 = one per second); "
                         "on-demand IDRs arrive via --control-stdin")
    ap.add_argument("--complexity", type=int, default=0,
                    help="openh264 complexity, low .. high")
    ap.add_argument("--profile", default="constrained-baseline",
                    help="openh264 profile")
    ap.add_argument("--intra-refresh", action="store_true",
                    help="enable encoder intra-refresh if the encoder supports "
                         "it (falls back to periodic IDR otherwise)")
    ap.add_argument("--control-stdin", action="store_true",
                    help="read 'keyframe'/'quit' commands from stdin")
    ap.add_argument("--test-pattern", action="store_true",
                    help="encode a synthetic pattern instead of the screen")
    ap.add_argument("--test-out", help="capture ONE access unit to this path, then exit")
    args = ap.parse_args(argv)

    gop = args.gop if args.gop > 0 else max(1, args.fps)

    Gst.init(None)

    session = None
    fd = node_id = -1
    if not args.test_pattern:
        session = open_session()
        save_token(session)
        node_id = first_node_id(session)
        fd = session.open_pipewire_remote()
        _log(f"portal stream node={node_id} fd={fd}")

    pipeline = build_pipeline(fd, node_id, args.width, args.height, args.fps,
                              args.bitrate, gop, args.complexity, args.profile,
                              args.test_pattern)
    sink = pipeline.get_by_name("sink")
    enc = pipeline.get_by_name("enc")
    if args.intra_refresh:
        if enc is not None and enc.find_property("intra-refresh") is not None:
            enc.set_property("intra-refresh", True)
            _log("intra-refresh: enabled")
        else:
            _log("intra-refresh: encoder has no 'intra-refresh' property; "
                 f"falling back to periodic IDR (gop={gop})")
    out = sys.stdout.buffer
    test_path = args.test_out
    captured = {"n": 0, "done": False}

    def on_sample(_sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(info.data)
        buf.unmap(info)
        try:
            if test_path:
                with open(test_path, "wb") as f:
                    f.write(data)
                captured["n"] += 1
                captured["done"] = True
                loop.quit()
            else:
                out.write(struct.pack("<I", len(data)))
                out.write(data)
                out.flush()
                captured["n"] += 1
        except BrokenPipeError:
            captured["done"] = True
            loop.quit()
        return Gst.FlowReturn.OK

    sink.connect("new-sample", on_sample)
    pipeline.set_state(Gst.State.PLAYING)

    loop = GLib.MainLoop()
    if args.control_stdin:
        _start_control_reader(pipeline, loop, enc)
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            _log(f"pipeline error: {err.message} ({dbg})")
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()

    bus.connect("message", on_msg)

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    if test_path and captured["done"]:
        _log(f"wrote {test_path} ({captured['n']} access unit(s))")
    return 0 if captured["n"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
