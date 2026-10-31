#!/usr/bin/env python3
"""Simulate PS4 DualShock 4 input (RP4I packets) with no real PS4.

Usage examples:
    python tools/fake_ps4_input.py                          # empty packets at 250 Hz
    python tools/fake_ps4_input.py --btn cross --btn square # Cross + Square held
    python tools/fake_ps4_input.py --stick-l 32767,0        # left stick full right
    python tools/fake_ps4_input.py --trigger-r 255          # right trigger full
    python tools/fake_ps4_input.py --dpad up                # D-pad up
    python tools/fake_ps4_input.py --pattern circle         # move left stick in a circle
    python tools/fake_ps4_input.py --loss 5 --seconds 30    # 5% loss for 30 s
    python tools/fake_ps4_input.py --player 2 --btn cross   # controller 2

Button names: cross, circle, square, triangle, l1, r1, l2, r2, share, options,
l3, r3, up, down, left, right
"""

import argparse
import math
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # pc/

from protocol import (  # noqa: E402
    INPUT_MAX_PLAYERS, InputPacket,
    BTN_CROSS, BTN_CIRCLE, BTN_SQUARE, BTN_TRIANGLE,
    BTN_L1, BTN_R1, BTN_L2, BTN_R2,
    BTN_SHARE, BTN_OPTIONS, BTN_L3, BTN_R3,
    BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT,
)

# name -> protocol bit
_BTN_BITS = {
    "cross": BTN_CROSS, "circle": BTN_CIRCLE, "square": BTN_SQUARE,
    "triangle": BTN_TRIANGLE, "l1": BTN_L1, "r1": BTN_R1,
    "l2": BTN_L2, "r2": BTN_R2, "share": BTN_SHARE,
    "options": BTN_OPTIONS, "l3": BTN_L3, "r3": BTN_R3,
    "up": BTN_UP, "down": BTN_DOWN, "left": BTN_LEFT, "right": BTN_RIGHT,
}

_DPAD_NAMES = ("up", "down", "left", "right")


def parse_stick(value: str):
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"stick expects x,y, got {value!r}")
    x = int(parts[0]); y = int(parts[1])
    return max(-32768, min(32767, x)), max(-32768, min(32767, y))


def parse_trigger(value: str):
    v = int(value)
    return max(0, min(255, v))


def build_pattern(kind: str, t: float):
    buttons = 0
    l2 = r2 = 0
    lx = ly = rx = ry = 0
    if kind == "circle":
        radius = 20000
        lx = int(radius * math.sin(t))
        ly = int(radius * math.cos(t))
    elif kind == "cross-press":
        buttons |= BTN_CROSS
    elif kind == "all-buttons":
        buttons = 0xFFFF
    elif kind == "triggers":
        l2 = 255
        r2 = 255
    elif kind == "dpad":
        phase = int(t) % 4
        if phase == 0: buttons |= BTN_UP
        elif phase == 1: buttons |= BTN_RIGHT
        elif phase == 2: buttons |= BTN_DOWN
        else: buttons |= BTN_LEFT
    return buttons, l2, r2, lx, ly, rx, ry


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Fake PS4 input sender (RP4I over UDP).")
    ap.add_argument("--host", default="127.0.0.1",
                    help="PC receiver host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=47001,
                    help="PC input port (default 47001)")
    ap.add_argument("--rate", type=float, default=250.0,
                    help="packets per second (default 250)")
    ap.add_argument("--player", type=int, default=1, metavar="N",
                    help=f"controller number 1..{INPUT_MAX_PLAYERS} "
                         "(1 = classic 18-byte packet, default)")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds (0 = forever)")
    ap.add_argument("--btn", action="append", default=[],
                    help="button to hold (can repeat). names: "
                         + ", ".join(sorted(_BTN_BITS)))
    ap.add_argument("--stick-l", type=parse_stick, default=None,
                    help="left stick as x,y (e.g. 32767,-12000)")
    ap.add_argument("--stick-r", type=parse_stick, default=None,
                    help="right stick as x,y")
    ap.add_argument("--trigger-l", type=parse_trigger, default=0,
                    help="left trigger 0-255")
    ap.add_argument("--trigger-r", type=parse_trigger, default=0,
                    help="right trigger 0-255")
    ap.add_argument("--dpad", choices=_DPAD_NAMES,
                    help="single dpad direction")
    ap.add_argument("--pattern", choices=["circle", "cross-press",
                        "all-buttons", "triggers", "dpad"],
                    help="run a demo pattern instead of static values")
    ap.add_argument("--loss", type=float, default=0.0,
                    help="simulated packet loss percent (0-100)")
    ap.add_argument("--debug", action="store_true",
                    help="print every packet sent")
    args = ap.parse_args(argv)

    buttons = 0
    for name in args.btn:
        key = name.lower()
        if key not in _BTN_BITS:
            ap.error(f"unknown button {name!r}; use one of: "
                     + ", ".join(sorted(_BTN_BITS)))
        buttons |= _BTN_BITS[key]
    if args.dpad:
        buttons |= _BTN_BITS[args.dpad]

    lx, ly = args.stick_l if args.stick_l else (0, 0)
    rx, ry = args.stick_r if args.stick_r else (0, 0)
    l2 = args.trigger_l
    r2 = args.trigger_r

    if not 1 <= args.player <= INPUT_MAX_PLAYERS:
        ap.error(f"--player must be 1..{INPUT_MAX_PLAYERS}")
    args.player -= 1                    # the wire field is 0-based

    loss = max(0.0, min(100.0, args.loss)) / 100.0
    interval = 1.0 / args.rate if args.rate > 0 else 0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)
    print(f"[fake_ps4_input] -> udp {dest[0]}:{dest[1]} "
          f"rate={args.rate:.1f} pkt/s loss={loss*100:.1f}%")
    if args.pattern:
        print(f"[fake_ps4_input] pattern: {args.pattern}")
    else:
        names = [n for n, b in _BTN_BITS.items() if buttons & b]
        print(f"[fake_ps4_input] buttons={names} "
              f"L2={l2} R2={r2} L=({lx},{ly}) R=({rx},{ry})")

    seq = 0
    start = time.monotonic()
    next_send = start
    sent = 0
    skipped = 0

    try:
        while True:
            now = time.monotonic()
            if args.seconds and (now - start) >= args.seconds:
                break

            if now < next_send:
                time.sleep(0.001)
                continue

            import random
            if random.random() < loss:
                skipped += 1
                next_send += interval
                continue

            if args.pattern:
                buttons, l2, r2, lx, ly, rx, ry = build_pattern(
                    args.pattern, now - start)

            pkt = InputPacket(
                version=1,
                seq=seq & 0xFF,
                buttons=buttons,
                l2=l2,
                r2=r2,
                lx=lx,
                ly=ly,
                rx=rx,
                ry=ry,
                player=args.player,
            )
            sock.sendto(pkt.pack(), dest)
            seq = (seq + 1) & 0xFF
            sent += 1
            next_send += interval

            if args.debug:
                pressed = [n for n, b in _BTN_BITS.items() if buttons & b]
                print(f"[fake_ps4_input] seq={pkt.seq} btn={pressed} "
                      f"L2={l2} R2={r2} L=({lx},{ly}) R=({rx},{ry})")
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    elapsed = time.monotonic() - start
    print(f"[fake_ps4_input] done in {elapsed:.1f}s: "
          f"{sent} sent, {skipped} dropped (loss)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
