"""Phase 1 PC receiver: listen for RP4I input packets, drive virtual Xbox 360 pads.

One virtual pad per player: player 0 arrives as the classic 18-byte packet,
players 1..3 as 19-byte version-2 packets (see docs/protocol.md §1).

Works on Windows (ViGEmBus) and Linux (uinput).

Usage:
    python receive_input.py             # uses config.ini
    python receive_input.py --debug     # print every packet (very spammy at 250 Hz)
    python receive_input.py --players 1 # single controller (old behaviour)
    python receive_input.py --no-gamepad# listen + print only (no virtual pad)
"""

import argparse
import configparser
import socket
import sys
import time
from pathlib import Path

from protocol import INPUT_MAX_PLAYERS, InputPacket

CONFIG_PATH = Path(__file__).parent / "config.ini"

# Reset a pad this many seconds after its last packet (PS4 pad unplugged or the
# sender restarted). Without it a held button would stay pressed on the PC.
PAD_TIMEOUT_S = 1.0
TICK_INTERVAL_S = 0.2


def load_config() -> dict:
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cp.read(CONFIG_PATH)
    return {
        "host": cp.get("input", "listen_host", fallback="0.0.0.0"),
        "port": cp.getint("input", "listen_port", fallback=47001),
        "debug": cp.getboolean("input", "debug", fallback=False),
        "max_players": cp.getint("input", "max_players", fallback=INPUT_MAX_PLAYERS),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Reverse Remote Play input receiver")
    ap.add_argument("--host", help="listen address (default from config.ini)")
    ap.add_argument("--port", type=int, help="listen port (default from config.ini)")
    ap.add_argument("--debug", action="store_true", help="print every packet")
    ap.add_argument("--players", type=int, metavar="N",
                    help=f"max players / virtual pads, 1..{INPUT_MAX_PLAYERS} "
                         "(default from config.ini; 1 = single controller)")
    ap.add_argument("--no-gamepad", "--no-vigem", dest="no_gamepad",
                    action="store_true",
                    help="do not open virtual gamepads (listen + print only)")
    args = ap.parse_args()

    cfg = load_config()
    host = args.host or cfg["host"]
    port = args.port or cfg["port"]
    debug = args.debug or cfg["debug"]
    max_players = args.players or cfg["max_players"]
    if not 1 <= max_players <= INPUT_MAX_PLAYERS:
        print(f"error: --players must be 1..{INPUT_MAX_PLAYERS}", file=sys.stderr)
        return 2

    pads = None
    if not args.no_gamepad:
        # Imported lazily so --no-gamepad works without vgamepad/evdev.
        from gamepad import VirtualPadSet
        pads = VirtualPadSet(max_players, timeout=PAD_TIMEOUT_S)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind((host, port))
    sock.settimeout(0.5)
    print(f"listening on udp://{host}:{port}  "
          f"gamepad={'off' if pads is None else f'on ({max_players} player(s))'}")

    count = 0
    bad = 0
    last_report = time.monotonic()
    last_tick = time.monotonic()
    last_seq = [None] * max_players   # per player: seq is per sender
    seq_gaps = 0
    seen_players: set[int] = set()
    ignored = 0                 # packets for players above --players
    last_pkt: InputPacket | None = None

    try:
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                # No traffic at all: still expire silent pads, otherwise a pad
                # that disconnects while idle keeps its last state on the PC.
                if pads is not None:
                    for gone in pads.tick():
                        print(f"player {gone + 1} timed out (pad reset)")
                continue

            pkt = InputPacket.unpack(data)
            if pkt is None:
                bad += 1
                continue

            before = (last_seq[pkt.player]
                      if 0 <= pkt.player < max_players else None)
            if before is not None and pkt.seq != (before + 1) & 0xFF:
                seq_gaps += 1
            if 0 <= pkt.player < max_players:
                last_seq[pkt.player] = pkt.seq
            count += 1
            if pkt.player < max_players:
                seen_players.add(pkt.player)
            else:
                ignored += 1
            last_pkt = pkt

            if pads is not None and pads.apply(pkt):
                print(f"player {pkt.player + 1} connected")

            if debug:
                sticks = f"L({pkt.lx:6d},{pkt.ly:6d}) R({pkt.rx:6d},{pkt.ry:6d})"
                print(f"p{pkt.player + 1} seq={pkt.seq:3d} btn=0x{pkt.buttons:04x} "
                      f"[{' '.join(pkt.pressed_buttons())}] "
                      f"L2={pkt.l2:3d} R2={pkt.r2:3d} {sticks}")

            now = time.monotonic()
            if pads is not None and now - last_tick >= TICK_INTERVAL_S:
                last_tick = now
                for gone in pads.tick(now):
                    print(f"player {gone + 1} timed out (pad reset)")

            if now - last_report >= 1.0:
                active = set(seen_players)
                if pads is not None:
                    active |= set(pads.active_players())
                names = ",".join(str(p + 1) for p in sorted(active))
                print(f"rate={count:4d} pkt/s  players={names or 'none'}"
                      f"  seq_gaps={seq_gaps}  bad={bad}"
                      + (f"  ignored={ignored}" if ignored else "")
                      + (f"  last={last_pkt.pressed_buttons()}" if last_pkt else ""))
                seen_players.clear()
                ignored = 0
                count = 0
                seq_gaps = 0
                bad = 0
                last_report = now
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        sock.close()
        if pads is not None:
            pads.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
