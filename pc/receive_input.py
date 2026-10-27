"""Phase 1 PC receiver: listen for RP4I input packets, drive a virtual Xbox 360 pad.

Works on Windows (ViGEmBus) and Linux (uinput).

Usage:
    python receive_input.py             # uses config.ini
    python receive_input.py --debug     # print every packet (very spammy at 250 Hz)
    python receive_input.py --no-gamepad# listen + print only (no virtual pad)
"""

import argparse
import configparser
import socket
import sys
import time
from pathlib import Path

from protocol import InputPacket

CONFIG_PATH = Path(__file__).parent / "config.ini"


def load_config() -> dict:
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH)
    return {
        "host": cp.get("input", "listen_host", fallback="0.0.0.0"),
        "port": cp.getint("input", "listen_port", fallback=47001),
        "debug": cp.getboolean("input", "debug", fallback=False),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Reverse Remote Play input receiver")
    ap.add_argument("--host", help="listen address (default from config.ini)")
    ap.add_argument("--port", type=int, help="listen port (default from config.ini)")
    ap.add_argument("--debug", action="store_true", help="print every packet")
    ap.add_argument("--no-gamepad", "--no-vigem", dest="no_gamepad",
                    action="store_true",
                    help="do not open a virtual gamepad (listen + print only)")
    args = ap.parse_args()

    cfg = load_config()
    host = args.host or cfg["host"]
    port = args.port or cfg["port"]
    debug = args.debug or cfg["debug"]

    pad = None
    if not args.no_gamepad:
        # Imported lazily so --no-gamepad works without vgamepad/evdev.
        from gamepad import VirtualPad
        pad = VirtualPad()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind((host, port))
    sock.settimeout(0.5)
    print(f"listening on udp://{host}:{port}  gamepad={'off' if pad is None else 'on'}")

    count = 0
    bad = 0
    last_report = time.monotonic()
    last_seq = None
    seq_gaps = 0
    last_pkt: InputPacket | None = None

    try:
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue

            pkt = InputPacket.unpack(data)
            if pkt is None:
                bad += 1
                continue
            print("ziad")


            if last_seq is not None and pkt.seq != (last_seq + 1) & 0xFF:
                seq_gaps += 1
            last_seq = pkt.seq
            count += 1
            last_pkt = pkt

            if pad is not None:
                print("ziad")
                pad.apply(pkt)

            if debug:
                sticks = f"L({pkt.lx:6d},{pkt.ly:6d}) R({pkt.rx:6d},{pkt.ry:6d})"
                print(f"seq={pkt.seq:3d} btn=0x{pkt.buttons:04x} "
                      f"[{' '.join(pkt.pressed_buttons())}] "
                      f"L2={pkt.l2:3d} R2={pkt.r2:3d} {sticks}")

            now = time.monotonic()
            if now - last_report >= 1.0:
                print(f"rate={count:4d} pkt/s  seq_gaps={seq_gaps}  bad={bad}"
                      + (f"  last={last_pkt.pressed_buttons()}" if last_pkt else ""))
                count = 0
                seq_gaps = 0
                bad = 0
                last_report = now
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        sock.close()
        if pad is not None:
            pad.reset()
    return 0


if __name__ == "__main__":
    sys.exit(main())
