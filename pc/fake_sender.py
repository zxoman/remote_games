"""Phase 1 test sender: simulate one or more DualShock 4 pads without a PS4.

Sends valid RP4I packets to a receive_input.py instance so the whole PC-side
pipeline (UDP → decode → virtual pad) can be tested on a Windows machine alone.

Usage:
    python fake_sender.py                        # one pad to 127.0.0.1:47001
    python fake_sender.py --target 192.168.1.50 --port 47001
    python fake_sender.py --rate 250
    python fake_sender.py --players 2            # two pads, different patterns
"""

import argparse
import math
import socket
import time

from protocol import (
    InputPacket, INPUT_VERSION, INPUT_MAX_PLAYERS,
    BTN_CROSS, BTN_CIRCLE, BTN_SQUARE, BTN_TRIANGLE,
    BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_L1, BTN_R1,
)

CYCLE_BUTTONS = [
    BTN_CROSS, BTN_CIRCLE, BTN_SQUARE, BTN_TRIANGLE,
    BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_L1, BTN_R1,
]


def triangle(phase: float, amplitude: int) -> int:
    """-amplitude..+amplitude triangle wave, phase in [0,1)."""
    t = (phase % 1.0) * 4.0
    if t < 1:
        v = t
    elif t < 3:
        v = 2.0 - t
    else:
        v = t - 4.0
    return int(v * amplitude)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=47001)
    ap.add_argument("--rate", type=int, default=250,
                    help="packets per second (per player)")
    ap.add_argument("--players", type=int, default=1,
                    help=f"1..{INPUT_MAX_PLAYERS} simulated pads")
    args = ap.parse_args()

    if not 1 <= args.players <= INPUT_MAX_PLAYERS:
        raise SystemExit(f"--players must be 1..{INPUT_MAX_PLAYERS}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    period = 1.0 / args.rate
    seq = [0] * args.players
    start = time.monotonic()
    print(f"fake DS4 x{args.players} -> udp://{args.target}:{args.port} "
          f"at {args.rate} Hz each (ctrl-c to stop)")

    try:
        while True:
            t = time.monotonic() - start

            for player in range(args.players):
                # Each pad gets a distinct phase so the virtual pads do not
                # look identical (player 1 circles, player 2 sweeps, ...).
                ph = t + player * 0.37

                lx = int(math.sin(ph * 2.0 * math.pi * 0.5) * 30000)
                ly = int(math.cos(ph * 2.0 * math.pi * 0.5) * 30000)
                rx = triangle(ph * (0.4 + 0.1 * player), 30000)
                ry = triangle(ph * 0.3 + 0.25, 30000)

                # Buttons: one new button every 0.25 s, held for 0.5 s.
                btn_idx = int(ph / 0.25) % len(CYCLE_BUTTONS)
                buttons = (CYCLE_BUTTONS[btn_idx]
                           | CYCLE_BUTTONS[(btn_idx + 1) % len(CYCLE_BUTTONS)])

                l2 = int((math.sin(ph * 2.0 * math.pi * 0.25) * 0.5 + 0.5) * 255)
                r2 = int((math.sin(ph * 2.0 * math.pi * 0.2 + 1.0) * 0.5 + 0.5) * 255)

                pkt = InputPacket(INPUT_VERSION, seq[player] & 0xFF, buttons,
                                  l2, r2, lx, ly, rx, ry, player)
                sock.sendto(pkt.pack(), (args.target, args.port))
                seq[player] += 1
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
