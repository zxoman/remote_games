#!/usr/bin/env python3
"""Reverse Remote Play — PC video sender (Phase 2 MJPEG / Phase 3 H.264).

Captures the desktop (or a synthetic test pattern), encodes each frame, splits it
into <=1200-byte UDP datagrams, and streams them to the PS4.

    mjpeg : FFmpeg/jpegenc (or GStreamer jpegenc on Wayland)  -- simple, but
            ~40-60 Mbps for good 720p30 quality.
    h264  : GStreamer/openh264 via grab_h264.py               -- far better
            quality per bit; each access unit is one RP4V frame.

Cross-platform screen capture is delegated to either the PipeWire portal
(Wayland) or FFmpeg:

    Wayland : grab_wayland.py / grab_h264.py (xdg-desktop-portal + GStreamer)
    Linux   : x11grab  (default input :0.0, X11 only)
    Windows : gdigrab  ("desktop")
    any     : --test-pattern  -> synthetic source (no display needed)

On a Wayland session (``$WAYLAND_DISPLAY`` set) the PipeWire portal is used
automatically because x11grab only sees the empty XWayland root window.  Use
``--capture x11``/``--capture wayland`` to override. H.264 always uses the
GStreamer helper (FFmpeg on this host has no H.264 encoder).

The PS4 side reassembles by frame_id and decodes with libjpeg-turbo (MJPEG,
ps4/src/jpeg_dec.c) or FFmpeg (H.264, ps4/src/h264_dec.c).

Examples
--------
    # Stream the whole screen at 720p30 to the PS4
    python3 send_mjpeg.py --host 192.168.1.20

    # H.264 at the default 2.5 Mbps (weak-network friendly)
    python3 send_mjpeg.py --host 192.168.1.20 --codec h264

    # No PS4 handy? Verify encoding/framing locally without sending:
    python3 send_mjpeg.py --test-pattern --no-send
"""

import argparse
import configparser
import os
import shlex
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from protocol import (  # noqa: E402
    VIDEO_FLAG_H264, VIDEO_FLAG_KEYFRAME, h264_has_idr, iter_chunks,
)
from feedback import FeedbackListener  # noqa: E402
from abr import AdaptiveBitrateController  # noqa: E402
from pacing import PacketPacer, chunk_count  # noqa: E402

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "ps4_host": "192.168.1.20",
    "video_port": 9025,
    "width": 1280,
    "height": 720,
    "fps": 30,
    "quality": 7,
    "display": ":0.0",
    "ffmpeg_input": "",
    "capture": "auto",
    "wayland_quality": 65,
    "codec": "auto",
    "h264_bitrate": 2500000,
    "h264_gop": 60,
    "h264_complexity": 0,
    "h264_profile": "constrained-baseline",
    # Step 3: GOP ~2 s plus an immediate IDR whenever the PS4 asks for one.
    "h264_keyframe_on_demand": "true",
    # openh264enc has no intra-refresh property; the helper logs the fallback.
    "h264_intra_refresh": "false",
    # Unified quality scale (0 = worst .. 100 = best). Empty/None = use the
    # legacy `quality` / `wayland_quality` keys as-is.
    "quality_pct": "",
    # Feedback channel (Step 2).
    "feedback_enabled": "true",
    "feedback_port": 47002,
    # Adaptive bitrate bounds (Step 4 consumes these).
    "abr_enabled": "true",
    "min_bitrate": 600000,
    "max_bitrate": 6000000,
    "min_fps": 15,
    "abr_loss_high": 2.0,
    "abr_loss_low": 1.0,
    "abr_decrease_factor": 0.75,
    "abr_increase_step": 0.05,
    "abr_increase_interval": 5.0,
    "abr_cooldown": 2.0,
    "abr_jitter_high_ms": 80.0,
    "abr_drop_ratio_high": 0.45,
    "abr_escalate_after": 3,
    "abr_restore_interval": 20.0,
    # Packet pacing (Step 5): spread one frame's datagrams over this fraction
    # of the frame interval. 0 = send each frame as one burst (old behaviour).
    "pacing_ratio": 0.6,
}

# [adaptive] key -> cfg key. Kept as a mapping so config.ini can use short,
# readable names without renaming the internal fields.
ADAPTIVE_KEYS = {
    "abr_enabled": "enabled",
    "min_bitrate": "min_bitrate",
    "max_bitrate": "max_bitrate",
    "min_fps": "min_fps",
    "abr_loss_high": "loss_threshold",
    "abr_loss_low": "loss_recover",
    "abr_decrease_factor": "decrease_factor",
    "abr_increase_step": "increase_step",
    "abr_increase_interval": "increase_interval",
    "abr_cooldown": "cooldown",
    "abr_jitter_high_ms": "jitter_threshold_ms",
    "abr_drop_ratio_high": "drop_ratio_threshold",
    "abr_escalate_after": "escalate_after",
    "abr_restore_interval": "restore_interval",
}

_BOOL_KEYS = ("feedback_enabled", "abr_enabled", "h264_intra_refresh",
              "h264_keyframe_on_demand")
_INT_KEYS = ("video_port", "width", "height", "fps", "quality", "wayland_quality",
             "h264_bitrate", "h264_gop", "h264_complexity", "feedback_port",
             "min_bitrate", "max_bitrate", "min_fps", "abr_escalate_after")
_FLOAT_KEYS = ("abr_loss_high", "abr_loss_low", "abr_decrease_factor",
               "abr_increase_step", "abr_increase_interval", "abr_cooldown",
               "abr_jitter_high_ms", "abr_drop_ratio_high",
               "abr_restore_interval", "pacing_ratio")


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        cp.read(path)
        if cp.has_section("video"):
            for k in DEFAULTS:
                if cp.has_option("video", k):
                    cfg[k] = cp.get("video", k)
        if cp.has_section("adaptive"):
            for cfg_key, ini_key in ADAPTIVE_KEYS.items():
                if cp.has_option("adaptive", ini_key):
                    cfg[cfg_key] = cp.get("adaptive", ini_key)
    for k in _INT_KEYS:
        cfg[k] = int(cfg[k])
    for k in _FLOAT_KEYS:
        cfg[k] = float(cfg[k])
    for k in _BOOL_KEYS:
        cfg[k] = str(cfg[k]).strip().lower() in ("1", "true", "yes", "on")
    raw_pct = str(cfg["quality_pct"]).strip()
    cfg["quality_pct"] = int(raw_pct) if raw_pct else None
    return cfg


# --- Quality scale normalisation ----------------------------------------
#
# Three knobs historically described the same thing on opposite scales:
#   * `quality`         -> FFmpeg `-q:v`, 2 = best .. 31 = worst (lower better)
#   * `wayland_quality` -> GStreamer `jpegenc quality`, 0 = worst .. 100 = best
#   * `quality_pct`     -> unified 0 = worst .. 100 = best (higher better)
#
# `quality_pct`, when set, wins and is mapped onto both legacy keys so the
# rest of the pipeline can keep using its native value.

def ffmpeg_q_from_pct(pct):
    """0..100 (higher=better) -> FFmpeg -q:v (2=best .. 31=worst)."""
    pct = max(0, min(100, int(pct)))
    return int(round(31 - pct * 29 / 100))


def pct_from_ffmpeg_q(q):
    """FFmpeg -q:v -> 0..100 (higher=better)."""
    q = max(2, min(31, int(q)))
    return int(round((31 - q) * 100 / 29))


def apply_quality_scale(cfg):
    """Resolve the legacy quality keys from `quality_pct` if it is set."""
    if cfg.get("quality_pct") is not None:
        pct = max(0, min(100, int(cfg["quality_pct"])))
        cfg["quality"] = ffmpeg_q_from_pct(pct)
        cfg["wayland_quality"] = pct
    return cfg


def resolve_backend(cfg, test_pattern):
    """Pick the capture backend: 'wayland' (PipeWire portal) or 'ffmpeg'."""
    if test_pattern:
        return "ffmpeg"
    c = (cfg.get("capture") or "auto").lower()
    if c in ("wayland", "portal"):
        return "wayland"
    if c in ("x11", "x11grab", "ffmpeg", "gdigrab", "raw"):
        return "ffmpeg"
    # auto: on a Wayland session x11grab only sees the empty XWayland root,
    # so use the PipeWire portal when a Wayland display is present.
    if not sys.platform.startswith("win") and os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "ffmpeg"


def h264_encoder_available():
    """True if GStreamer can actually encode H.264 on this machine.

    ``openh264enc`` is the only H.264 encoder the PC path drives (FFmpeg on
    this host has no H.264 encoder), and ``grab_h264.py`` needs the GStreamer
    Python bindings. A missing typelib, a missing GStreamer or a missing plugin
    all mean "use MJPEG instead" rather than a hard failure.
    """
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        return Gst.ElementFactory.find("openh264enc") is not None
    except Exception:
        return False


def resolve_codec(cfg):
    """Map ``codec = auto`` to ``h264`` or ``mjpeg``; validate explicit values.

    Auto is the shipped default so the same config works on Linux (H.264) and
    on Windows without GStreamer (MJPEG, ~10x the bandwidth but functional).
    """
    codec = str(cfg["codec"]).strip().lower()
    if codec == "auto":
        return "h264" if h264_encoder_available() else "mjpeg"
    if codec not in ("h264", "mjpeg"):
        raise SystemExit(f"[send_mjpeg] codec must be auto, h264 or mjpeg "
                         f"(got {cfg['codec']!r})")
    return codec


def build_capture_argv(cfg, test_pattern, ffmpeg_bin, bitrate=None):
    # H.264 is encoded by GStreamer (openh264enc) inside grab_h264.py; FFmpeg on
    # this host has no H.264 *encoder*, so the capture path is always the
    # GStreamer helper when codec=h264 (synthetic pattern included).
    if cfg["codec"] == "h264":
        argv = [
            sys.executable, os.path.join(HERE, "grab_h264.py"),
            "--width", str(cfg["width"]),
            "--height", str(cfg["height"]),
            "--fps", str(cfg["fps"]),
            "--bitrate", str(cfg["h264_bitrate"] if bitrate is None else bitrate),
            "--gop", str(cfg["h264_gop"]),
            "--complexity", str(cfg["h264_complexity"]),
            "--profile", str(cfg["h264_profile"]),
        ]
        if test_pattern:
            argv.append("--test-pattern")
        if cfg.get("h264_intra_refresh"):
            argv.append("--intra-refresh")
        if cfg.get("h264_keyframe_on_demand"):
            argv.append("--control-stdin")
        return argv

    if resolve_backend(cfg, test_pattern) == "wayland":
        return [
            sys.executable, os.path.join(HERE, "grab_wayland.py"),
            "--width", str(cfg["width"]),
            "--height", str(cfg["height"]),
            "--fps", str(cfg["fps"]),
            "--quality", str(cfg["wayland_quality"]),
        ]

    w, h, fps, q = cfg["width"], cfg["height"], cfg["fps"], cfg["quality"]

    if test_pattern:
        # -re paces lavfi at its native rate; otherwise it emits as fast as it can.
        inp = ["-re", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}"]
    elif cfg["ffmpeg_input"]:
        inp = shlex.split(cfg["ffmpeg_input"])
    elif sys.platform.startswith("win"):
        inp = ["-f", "gdigrab", "-framerate", str(fps), "-i", "desktop"]
    else:
        inp = ["-f", "x11grab", "-framerate", str(fps),
               "-video_size", f"{w}x{h}", "-i", cfg["display"]]

    return [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error",
        *inp,
        "-vf", f"scale={w}:{h}:flags=fast_bilinear",
        "-an",
        "-c:v", "mjpeg", "-q:v", str(q), "-pix_fmt", "yuvj420p",
        "-f", "image2pipe",
        "-",
    ]


def iter_jpeg_frames(stream, chunk_size=1 << 16):
    """Yield complete JPEG byte strings from an MJPEG byte stream."""
    buf = bytearray()
    while True:
        data = stream.read(chunk_size)
        if not data:
            return
        buf += data
        while True:
            start = buf.find(SOI)
            if start < 0:
                buf.clear()
                break
            end = buf.find(EOI, start + 2)
            if end < 0:
                if start > 0:
                    del buf[:start]  # drop junk before the SOI
                break
            yield bytes(buf[start:end + 2])
            del buf[:end + 2]


def iter_h264_frames(stream):
    """Yield Annex-B access units from grab_h264.py's length-prefixed stream."""
    hdr = 4
    while True:
        prefix = stream.read(hdr)
        if not prefix or len(prefix) < hdr:
            return
        (size,) = struct.unpack("<I", prefix)
        if size == 0:
            continue
        data = bytearray()
        while len(data) < size:
            more = stream.read(size - len(data))
            if not more:
                return
            data += more
        yield bytes(data)


def send_encoder_keyframe(proc):
    """Ask grab_h264.py for an immediate IDR via its stdin control channel.

    Returns True if the command was written. A dead/closed pipe is expected
    while shutting down and must never raise into the send loop."""
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return False
    try:
        stdin.write(b"keyframe\n")
        stdin.flush()
        return True
    except (BrokenPipeError, ValueError, OSError):
        return False


def send_encoder_bitrate(proc, bitrate):
    """Change the encoder target bitrate live (no pipeline restart)."""
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return False
    try:
        stdin.write(f"bitrate {int(bitrate)}\n".encode("ascii"))
        stdin.flush()
        return True
    except (BrokenPipeError, ValueError, OSError):
        return False


def terminate_proc(proc):
    """Close the control pipe and stop the capture process (never raises)."""
    if proc is None:
        return
    stdin = getattr(proc, "stdin", None)
    if stdin is not None:
        try:
            stdin.close()
        except OSError:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    except OSError:
        pass


def stats_interval_loss(prev, cur):
    """Derive (loss_pct, dropped_delta, completed_delta) from two STATS samples.

    The PS4 counters are cumulative, so differencing gives last interval's loss,
    which is what the ABR needs. Returns None when the counters cannot be
    compared (first sample, receiver restart, wrap)."""
    if prev is None or cur is None:
        return None
    exp = cur.packets_expected - prev.packets_expected
    recv = cur.packets_received - prev.packets_received
    drop = cur.frames_dropped - prev.frames_dropped
    comp = cur.frames_completed - prev.frames_completed
    if exp <= 0 or recv < 0 or drop < 0 or comp < 0:
        return None
    lost = max(0, exp - recv)
    return min(100.0, 100.0 * lost / exp), drop, comp


def main(argv=None):
    # The ABR logs decisions at 1 Hz; when stdout is redirected to a file the
    # default block buffering would hide them for many seconds (and reorder them
    # against the helper's stderr). Line buffering keeps logs usable live.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="Stream MJPEG to the PS4.")
    ap.add_argument("--config", default=os.path.join(HERE, "config.ini"))
    ap.add_argument("--host", help="PS4 IP address (overrides config)")
    ap.add_argument("--port", type=int, help="PS4 video port (overrides config)")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--fps", type=int)
    ap.add_argument("--quality", type=int, help="FFmpeg -q:v, 2 (best) .. 31 (worst)")
    ap.add_argument("--quality-pct", type=int, dest="quality_pct",
                    help="unified quality 0 (worst) .. 100 (best); overrides "
                         "--quality/wayland_quality")
    ap.add_argument("--display", help="X11 display for x11grab, e.g. :0.0")
    ap.add_argument("--input", dest="ffmpeg_input",
                    help="raw FFmpeg input args (overrides platform capture)")
    ap.add_argument("--capture", choices=["auto", "wayland", "x11", "ffmpeg"],
                    help="capture backend (default auto: PipeWire on Wayland)")
    ap.add_argument("--codec", choices=["auto", "mjpeg", "h264"],
                    help="video codec (default auto: h264 when GStreamer can "
                         "encode it, else mjpeg)")
    ap.add_argument("--bitrate", type=int, dest="h264_bitrate",
                    help="H.264 target bitrate in bits/s (codec=h264)")
    ap.add_argument("--gop", type=int, dest="h264_gop",
                    help="H.264 keyframe interval in frames, 0=1s (codec=h264)")
    ap.add_argument("--profile", dest="h264_profile",
                    help="H.264 profile (codec=h264)")
    ap.add_argument("--complexity", type=int, dest="h264_complexity",
                    help="H.264 encoder complexity (codec=h264)")
    ap.add_argument("--test-pattern", action="store_true",
                    help="stream a synthetic testsrc instead of the screen")
    ap.add_argument("--no-send", action="store_true",
                    help="encode and parse but do not send (for local checks)")
    ap.add_argument("--no-feedback", action="store_true",
                    help="do not listen for PS4 STATS/KEYFRAME_REQUEST packets")
    ap.add_argument("--feedback-port", type=int, dest="feedback_port",
                    help="UDP port to listen on for PS4 feedback (default 47002)")
    ap.add_argument("--pacing-ratio", type=float, dest="pacing_ratio",
                    help="spread a frame's packets over this fraction of the "
                         "frame interval (0 disables pacing; default 0.6)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = run forever)")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg binary")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    # CLI overrides
    if args.host is not None:
        cfg["ps4_host"] = args.host
    if args.port is not None:
        cfg["video_port"] = args.port
    if args.width is not None:
        cfg["width"] = args.width
    if args.height is not None:
        cfg["height"] = args.height
    if args.fps is not None:
        cfg["fps"] = args.fps
    if args.quality is not None:
        cfg["quality"] = args.quality
    if args.display is not None:
        cfg["display"] = args.display
    if args.ffmpeg_input is not None:
        cfg["ffmpeg_input"] = args.ffmpeg_input
    if args.capture is not None:
        cfg["capture"] = args.capture
    if args.codec is not None:
        cfg["codec"] = args.codec
    if args.h264_bitrate is not None:
        cfg["h264_bitrate"] = args.h264_bitrate
    if args.h264_gop is not None:
        cfg["h264_gop"] = args.h264_gop
    if args.h264_profile is not None:
        cfg["h264_profile"] = args.h264_profile
    if args.h264_complexity is not None:
        cfg["h264_complexity"] = args.h264_complexity
    if args.quality_pct is not None:
        cfg["quality_pct"] = args.quality_pct
    if args.pacing_ratio is not None:
        cfg["pacing_ratio"] = args.pacing_ratio
    if args.feedback_port is not None:
        cfg["feedback_port"] = args.feedback_port
    if args.no_feedback:
        cfg["feedback_enabled"] = False

    # Resolve the unified 0..100 quality onto the legacy keys (see the helper
    # definitions above).
    apply_quality_scale(cfg)

    requested_codec = str(cfg["codec"]).strip().lower()
    cfg["codec"] = resolve_codec(cfg)
    argv_cap = build_capture_argv(cfg, args.test_pattern, args.ffmpeg)
    is_h264 = cfg["codec"] == "h264"
    if requested_codec == "auto":
        print(f"[send_mjpeg] codec: auto -> {cfg['codec']} "
              f"({'openh264enc found' if is_h264 else 'no GStreamer H.264 encoder'})")
    print(f"[send_mjpeg] codec={cfg['codec']} "
          f"({'H.264/GStreamer' if is_h264 else 'MJPEG/FFmpeg'})")
    if not is_h264:
        print(f"[send_mjpeg] quality: ffmpeg -q:v={cfg['quality']} "
              f"(= {pct_from_ffmpeg_q(cfg['quality'])}/100), "
              f"wayland={cfg['wayland_quality']}/100")
    print(f"[send_mjpeg] adaptive: enabled={cfg['abr_enabled']} "
          f"bitrate={cfg['h264_bitrate']} "
          f"bounds=[{cfg['min_bitrate']}..{cfg['max_bitrate']}] "
          f"min_fps={cfg['min_fps']}")
    print("[send_mjpeg] capture:", " ".join(shlex.quote(a) for a in argv_cap))

    # Feedback listener: starts even with --no-send so the channel can be
    # exercised with pc/tools/fake_ps4_stats.py and no PS4 attached.
    fb = None
    if cfg["feedback_enabled"]:
        try:
            fb = FeedbackListener(cfg["feedback_port"]).start()
            print(f"[send_mjpeg] feedback: listening on udp "
                  f"0.0.0.0:{cfg['feedback_port']}")
        except OSError as e:
            print(f"[send_mjpeg] feedback: disabled ({e})")
    else:
        print("[send_mjpeg] feedback: disabled")

    sock = None
    if not args.no_send:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # A large send buffer absorbs a keyframe burst; non-blocking mode means a
        # full buffer drops frames instead of stalling the capture loop (which
        # would show up as stutter).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        sock.setblocking(False)
        dest = (cfg["ps4_host"], cfg["video_port"])
        print(f"[send_mjpeg] -> udp {dest[0]}:{dest[1]}")
    else:
        print("[send_mjpeg] --no-send: frames parsed but not transmitted")

    # Step 5: packet pacing. Disabled with --no-send (nothing reaches the
    # network, so sleeping would only make the local check slower).
    try:
        pacer = PacketPacer(cfg["pacing_ratio"], cfg["fps"])
    except ValueError as e:
        raise SystemExit(f"[send_mjpeg] pacing_ratio: {e}")
    pace = pacer.enabled and sock is not None
    if pace:
        print(f"[send_mjpeg] pacing: {pacer.describe()} "
              f"(pacing_ratio = 0 disables)")
    elif pacer.enabled:
        print("[send_mjpeg] pacing: off for --no-send")
    else:
        print("[send_mjpeg] pacing: disabled (pacing_ratio = 0)")

    def spawn_capture(bitrate=None):
        """Start the capture helper.

        ``bitrate`` overrides the configured h264_bitrate so a restart caused by
        an fps/resolution change keeps the ABR's current target instead of
        snapping back to the starting bitrate.
        """
        argv = build_capture_argv(cfg, args.test_pattern, args.ffmpeg,
                                  bitrate=bitrate)
        return subprocess.Popen(
            argv, stdout=subprocess.PIPE,
            stdin=subprocess.PIPE if is_h264 else None, bufsize=0)

    proc = spawn_capture()
    start = time.time()

    abr = None
    if cfg["abr_enabled"] and is_h264:
        try:
            abr = AdaptiveBitrateController(
                start_bitrate=cfg["h264_bitrate"],
                min_bitrate=cfg["min_bitrate"],
                max_bitrate=cfg["max_bitrate"],
                start_fps=cfg["fps"],
                min_fps=cfg["min_fps"],
                width=cfg["width"],
                height=cfg["height"],
                loss_high=cfg["abr_loss_high"],
                loss_low=cfg["abr_loss_low"],
                decrease_factor=cfg["abr_decrease_factor"],
                decrease_cooldown_s=cfg["abr_cooldown"],
                increase_step=cfg["abr_increase_step"],
                increase_interval_s=cfg["abr_increase_interval"],
                jitter_high_ms=cfg["abr_jitter_high_ms"],
                drop_ratio_high=cfg["abr_drop_ratio_high"],
                escalate_after=cfg["abr_escalate_after"],
                restore_interval_s=cfg["abr_restore_interval"],
            )
            print(f"[send_mjpeg] abr: decrease x{cfg['abr_decrease_factor']} "
                  f"when loss>{cfg['abr_loss_high']}% ; +"
                  f"{cfg['abr_increase_step'] * 100:.0f}% every "
                  f"{cfg['abr_increase_interval']:.0f}s when loss<"
                  f"{cfg['abr_loss_low']}%")
            if fb is None:
                print("[send_mjpeg] abr: idle - feedback is disabled, so the "
                      "controller has no samples to react to")
        except ValueError as e:
            print(f"[send_mjpeg] abr: disabled ({e})")
    elif cfg["abr_enabled"]:
        print("[send_mjpeg] abr: only used with codec=h264")

    # Seed from the clock so a restart resumes at a *higher* frame_id than the
    # previous run; the PS4 receiver drops any frame_id lower than its current
    # one, so starting from 0 again would wedge it.
    frame_id = int(time.time() * 1000) & 0xFFFFFFFF
    sent_pkts = 0
    sent_bytes = 0
    send_errors = 0
    send_drops = 0                 # frames abandoned because the buffer was full
    errno_counts = {}
    last_report = start
    report_frames = 0
    report_bytes = 0
    last_report_pkts = 0
    last_report_errs = 0
    kf_requests_seen = 0
    last_report_kf = 0
    last_kf_send = 0.0
    last_stats = None
    last_restart = time.monotonic()
    abr_bitrate_changes = 0

    try:
        stop = False
        while not stop:
            restart_needed = False
            frame_iter = (iter_h264_frames(proc.stdout) if is_h264
                          else iter_jpeg_frames(proc.stdout))
            for frame in frame_iter:
                if is_h264:
                    flags = VIDEO_FLAG_H264
                    if h264_has_idr(frame):
                        flags |= VIDEO_FLAG_KEYFRAME
                else:
                    flags = VIDEO_FLAG_KEYFRAME

                # Step 3: act on a PS4 KEYFRAME_REQUEST immediately (polled per
                # frame, not at the 1 Hz log boundary). The sender also caps this
                # at 2/s, independent of the PS4's own limit, so neither side can
                # trigger a keyframe storm.
                if fb is not None and is_h264:
                    if fb.take_keyframe_request():
                        mono = time.monotonic()
                        if mono - last_kf_send >= 0.5:
                            last_kf_send = mono
                            if send_encoder_keyframe(proc):
                                kf_requests_seen += 1

                if sock is not None:
                    # Step 5: spread this frame's datagrams over part of the
                    # frame interval instead of handing the kernel one burst.
                    if pace:
                        pacer.begin_frame(chunk_count(len(frame)))
                    for dgram in iter_chunks(frame_id & 0xFFFFFFFF, frame, flags):
                        if pace:
                            pacer.wait()
                        try:
                            sock.sendto(dgram, dest)
                            sent_pkts += 1
                            sent_bytes += len(dgram)
                        except BlockingIOError:
                            # Send buffer full: the kernel is throttled. The rest of
                            # this access unit cannot be completed, so drop it and
                            # move on rather than blocking (the PS4 would discard a
                            # partial frame anyway).
                            send_errors += 1
                            send_drops += 1
                            break
                        except OSError as e:
                            # Route hiccups (Wi-Fi roaming) must not kill the whole
                            # session; count by errno so we can diagnose them.
                            send_errors += 1
                            errno_counts[e.errno] = errno_counts.get(e.errno, 0) + 1
                frame_id += 1
                report_frames += 1
                report_bytes += len(frame)

                now = time.time()
                if now - last_report >= 1.0:
                    fps = report_frames / (now - last_report)
                    mbps = (report_bytes * 8) / (now - last_report) / 1e6
                    extra = ("" if sock is None else
                             f" pkts/s={(sent_pkts - last_report_pkts) / (now - last_report):.0f}"
                             f" err/s={send_errors - last_report_errs}")
                    if pace:
                        extra += (f" pace={pacer.ratio:.2f}"
                                  f" late={pacer.late_frames}")
                    st = None
                    if fb is not None:
                        st, _ts = fb.latest()
                        if st is not None:
                            extra += (f" fb_loss={st.loss_permil() / 10:.1f}%"
                                      f" fb_jit={st.jitter_ms}ms"
                                      f" fb_decq={st.decoder_queue}")
                        else:
                            extra += " fb=waiting"
                        since = kf_requests_seen - last_report_kf
                        last_report_kf = kf_requests_seen
                        if since:
                            extra += f" kfreq={since}"
                    if abr is not None:
                        extra += f" abr={abr.target.bitrate // 1000}k"
                    print(f"[send_mjpeg] frame={frame_id} {fps:5.1f} fps "
                          f"{report_bytes/(now-last_report)/1024:7.1f} KiB/s "
                          f"{mbps:5.2f} Mbps{extra} "
                          f"last_{'au' if is_h264 else 'jpg'}={len(frame)}B")
                    last_report = now
                    last_report_errs = send_errors
                    report_frames = 0
                    report_bytes = 0
                    last_report_pkts = sent_pkts

                    # --- Step 4: adapt once per fresh STATS sample -----------
                    if (abr is not None and st is not None
                            and st is not last_stats):
                        delta = stats_interval_loss(last_stats, st)
                        last_stats = st
                        if delta is not None:
                            loss_i, drop_i, comp_i = delta
                            allow = (time.monotonic() - last_restart) >= 5.0
                            decision = abr.update(
                                loss_i, jitter_ms=st.jitter_ms,
                                frames_dropped=drop_i,
                                frames_completed=comp_i,
                                allow_restart=allow,
                                now=time.monotonic())
                            if decision is not None:
                                nt = decision.target
                                print(f"[abr +{time.time() - start:.1f}s] "
                                      f"{decision.reason} => "
                                      f"bitrate={nt.bitrate} "
                                      f"{nt.width}x{nt.height}@{nt.fps}")
                                if decision.kind == "bitrate":
                                    if send_encoder_bitrate(proc, nt.bitrate):
                                        abr_bitrate_changes += 1
                                else:
                                    # fps/resolution: restart on the next loop.
                                    cfg["fps"] = nt.fps
                                    cfg["width"] = nt.width
                                    cfg["height"] = nt.height
                                    restart_needed = True

                if args.duration and (now - start) >= args.duration:
                    stop = True
                    break
                if restart_needed:
                    break

            if stop or not restart_needed:
                break

            # fps/resolution changed: clean restart. The ABR only escalates
            # quality when at least 5 s have passed since the previous restart.
            print(f"[abr +{time.time() - start:.1f}s] restarting capture -> "
                  f"{cfg['width']}x{cfg['height']}@{cfg['fps']} "
                  f"(bitrate={abr.target.bitrate})")
            terminate_proc(proc)
            proc = spawn_capture(abr.target.bitrate)
            pacer.set_fps(cfg["fps"])
            last_restart = time.monotonic()
            last_report = time.time()
            last_report_pkts = sent_pkts
            last_report_errs = send_errors
            report_frames = 0
            report_bytes = 0
    except KeyboardInterrupt:
        pass
    finally:
        terminate_proc(proc)
        if sock is not None:
            sock.close()
        if fb is not None:
            fb.stop()

    elapsed = time.time() - start
    print(f"[send_mjpeg] done: {frame_id} frames in {elapsed:.1f}s, "
          f"{sent_pkts} packets, {sent_bytes} bytes, {send_errors} send errors "
          f"({send_drops} frames dropped on a full buffer)")
    if pace:
        print(f"[send_mjpeg] {pacer.summary()}")
    if errno_counts:
        import errno as _errno
        pretty = ", ".join(
            f"{_errno.errorcode.get(k, k)}={v}" for k, v in sorted(errno_counts.items()))
        print(f"[send_mjpeg] sendto errnos: {pretty}")
    if fb is not None:
        c = fb.counters()
        print(f"[send_mjpeg] feedback: {c['stats']} STATS, "
              f"{c['kf_requests']} KEYFRAME_REQUEST "
              f"({kf_requests_seen} forwarded to encoder), "
              f"{c['malformed']} malformed")
    if abr is not None:
        t = abr.target
        print(f"[send_mjpeg] abr: final target={t.bitrate} bps "
              f"{t.width}x{t.height}@{t.fps}, "
              f"{abr_bitrate_changes} live bitrate change(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
