#!/usr/bin/env python3
"""Simulate the PS4 feedback channel (STATS + KEYFRAME_REQUEST) with no PS4.

Lets Step 2 be verified entirely on the PC:

    # terminal 1 - sender listening for feedback (no need to transmit video)
    python3 send_mjpeg.py --test-pattern --no-send --feedback-port 47002

    # terminal 2 - pretend to be the PS4 with 4% packet loss
    python3 tools/fake_ps4_stats.py --loss 4 --seconds 20

Expected in terminal 1: the 1 Hz line grows ``fb_loss=4.0% ...``.

Set --loss to simulate a bad link, --kf-every to also send rate-limited
KEYFRAME_REQUEST packets (Step 3 forces an IDR on the PC), and
--chunks-per-frame to change how many packets an access unit is split into.

Frame loss is modelled per access unit, not per packet: a frame only arrives if
*all* of its chunks do, so ``--loss 1.5`` already loses ~33 % of frames. That is
what the PS4 sees, and what the ABR's drop-ratio threshold is calibrated for.
"""

import argparse
import os
import random
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # pc/

from protocol import (  # noqa: E402
    FeedbackStatsPacket,
    KeyframeRequestPacket,
    KF_REASON_DECODE_ERROR,
    KF_REASON_INCOMPLETE,
    KF_REASON_STARTUP,
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fake PS4 feedback sender.")
    ap.add_argument("--host", default="127.0.0.1", help="PC feedback listener")
    ap.add_argument("--port", type=int, default=47002, help="feedback port")
    ap.add_argument("--loss", type=float, default=0.0,
                    help="simulated packet loss, percent (0-100)")
    ap.add_argument("--jitter-ms", type=int, default=3,
                    help="base jitter reported in STATS")
    ap.add_argument("--fps", type=int, default=30,
                    help="frames per second to model")
    ap.add_argument("--chunks-per-frame", type=int, default=26,
                    help="average RP4V chunks per access unit; a frame is lost "
                         "if any chunk is lost, so this drives the drop ratio")
    ap.add_argument("--kf-every", type=int, default=0,
                    help="send a KEYFRAME_REQUEST every N seconds (0=off)")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds (0 = forever)")
    args = ap.parse_args(argv)

    loss = max(0.0, min(100.0, args.loss)) / 100.0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)
    print(f"[fake_ps4] -> udp {dest[0]}:{dest[1]} loss={loss*100:.1f}% "
          f"fps={args.fps}")

    seq = 0
    start = time.monotonic()
    last_kf = start
    last_report = start
    total_expected = 0
    total_received = 0
    total_completed = 0
    total_dropped = 0
    frame_id = 0

    # A receiver always needs a keyframe after it starts.
    startup = KeyframeRequestPacket(seq=seq, last_frame_id=0,
                                    reason=KF_REASON_STARTUP)
    sock.sendto(startup.pack(), dest)
    seq = (seq + 1) & 0xFFFF

    try:
        while True:
            now = time.monotonic()
            if args.seconds and (now - start) >= args.seconds:
                break
            if now - last_report < 1.0:
                time.sleep(0.05)
                continue

            dt = now - last_report
            last_report = now
            frames = max(1, int(round(args.fps * dt)))
            chunks = max(1, args.chunks_per_frame)
            expected = frames * chunks
            # An access unit is only complete when *every* chunk arrives, so a
            # low packet-loss rate still loses a large fraction of frames:
            # 1 % loss over 26 chunks already loses ~23 % of frames. Model both
            # counters from the same probability so the ABR sees realistic data.
            per_frame_loss = 1.0 - (1.0 - loss) ** chunks
            dropped = int(round(frames * per_frame_loss))
            completed = frames - dropped
            received = expected - int(round(expected * loss))

            total_expected += expected
            total_received += received
            total_completed += completed
            total_dropped += dropped
            frame_id += frames

            jitter = max(0, args.jitter_ms + random.randint(-1, 3))
            pkt = FeedbackStatsPacket(
                seq=seq,
                packets_expected=total_expected,
                packets_received=total_received,
                frames_completed=total_completed,
                frames_dropped=total_dropped,
                jitter_ms=jitter,
                last_frame_id=frame_id,
                decoder_queue=random.randint(0, 2),
            )
            sock.sendto(pkt.pack(), dest)
            seq = (seq + 1) & 0xFFFF

            print(f"[fake_ps4] stats seq={pkt.seq} frames={completed} "
                  f"drop={dropped} exp={expected} recv={received} "
                  f"jitter={jitter}ms loss={pkt.loss_permil()/10:.1f}%")

            if args.kf_every and (now - last_kf) >= args.kf_every:
                last_kf = now
                reason = (KF_REASON_INCOMPLETE if dropped
                          else KF_REASON_DECODE_ERROR)
                kf = KeyframeRequestPacket(seq=seq, last_frame_id=frame_id,
                                           reason=reason)
                sock.sendto(kf.pack(), dest)
                seq = (seq + 1) & 0xFFFF
                print(f"[fake_ps4] keyframe_request reason={reason}")
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    elapsed = time.monotonic() - start
    print(f"[fake_ps4] done in {elapsed:.1f}s: {total_expected} expected, "
          f"{total_received} received, {total_dropped} frames dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
