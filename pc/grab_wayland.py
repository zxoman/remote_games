#!/usr/bin/env python3
"""Reverse Remote Play — Wayland (PipeWire portal) screen capture.

Captures a monitor through the xdg-desktop-portal ScreenCast API, encodes each
frame as JPEG with GStreamer, and writes the concatenated JPEGs to stdout.  The
output format matches what FFmpeg's `-f image2pipe -c:v mjpeg` produces, so
send_mjpeg.py can use this as a drop-in capture source on Wayland sessions
(where x11grab only sees the empty XWayland root window).

The first run pops up a GNOME/KDE "Share your screen" dialog.  With
PersistMode.PERSISTENT a restore token is cached in .portal_token so later runs
do not ask again.

Examples
--------
    # One frame to a file (validates portal + pipeline without sending)
    python3 grab_wayland.py --test-out /tmp/frame.jpg

    # Continuous JPEG stream on stdout (what send_mjpeg.py uses)
    python3 grab_wayland.py --width 1280 --height 720 --fps 30 --quality 65
"""

import argparse
import os
import pathlib
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Xdp", "1.0")
from gi.repository import GLib, Gst, Xdp  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".portal_token"


def _log(msg):
    print(f"[grab_wayland] {msg}", file=sys.stderr, flush=True)


def open_session():
    """Create+start a screencast session, return the started Xdp.Session."""
    portal = Xdp.Portal.new()
    token = None
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip() or None

    loop = GLib.MainLoop()
    out = {}

    def on_created(p, res, _data):
        try:
            out["session"] = p.create_screencast_session_finish(res)
        except GLib.Error as e:
            out["error"] = f"create_screencast_session: {e.message}"
            loop.quit()
            return
        streams = out["session"].get_streams()
        if streams is not None and streams.n_children() > 0:
            loop.quit()  # already started by create_screencast_session
        else:
            out["session"].start(None, None, on_started, None)

    def on_started(session, res, _data):
        try:
            session.start_finish(res)
        except GLib.Error as e:
            out["error"] = f"session.start: {e.message}"
        loop.quit()

    portal.create_screencast_session(
        Xdp.OutputType.MONITOR,
        Xdp.ScreencastFlags.NONE,
        Xdp.CursorMode.EMBEDDED,
        Xdp.PersistMode.PERSISTENT,
        token,
        None,
        on_created,
        None,
    )
    loop.run()

    if "error" in out:
        raise RuntimeError(out["error"])
    return out["session"]


def save_token(session):
    try:
        tok = session.get_restore_token()
    except Exception:
        tok = None
    if tok:
        TOKEN_FILE.write_text(tok)
        os.chmod(TOKEN_FILE, 0o600)


def first_node_id(session):
    streams = session.get_streams()
    if streams is None or streams.n_children() == 0:
        raise RuntimeError("portal returned no screencast streams")
    pair = streams.get_child_value(0)          # (u a{sv})
    return pair.get_child_value(0).get_uint32()  # node id


def build_pipeline(fd, node_id, width, height, fps, quality):
    desc = (
        f"pipewiresrc fd={fd} path={node_id} do-timestamp=true ! "
        f"videorate ! video/x-raw,framerate={fps}/1 ! "
        f"videoconvert ! videoscale add-borders=true ! "
        f"video/x-raw,width={width},height={height} ! "
        f"jpegenc quality={quality} ! "
        f"appsink name=sink emit-signals=true max-buffers=2 drop=true"
    )
    _log(f"pipeline: {desc}")
    return Gst.parse_launch(desc)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wayland/PipeWire screen capture -> JPEG on stdout")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--quality", type=int, default=65, help="jpegenc quality 0..100 (100 best)")
    ap.add_argument("--test-out", help="capture ONE frame to this path, then exit")
    args = ap.parse_args(argv)

    Gst.init(None)
    session = open_session()
    save_token(session)
    node_id = first_node_id(session)
    fd = session.open_pipewire_remote()
    _log(f"portal stream node={node_id} fd={fd}")

    pipeline = build_pipeline(fd, node_id, args.width, args.height, args.fps, args.quality)
    sink = pipeline.get_by_name("sink")
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
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            _log(f"pipeline error: {err.message} ({dbg})")
            loop.quit()
        elif msg.type in (Gst.MessageType.EOS,):
            loop.quit()

    bus.connect("message", on_msg)

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)
        try:
            session.close()
        except Exception:
            pass

    if test_path and captured["done"]:
        _log(f"wrote {test_path} ({captured['n']} frame(s))")
    return 0 if captured["n"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
