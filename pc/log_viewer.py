#!/usr/bin/env python3
"""Reverse Remote Play — PS4 debug log viewer.

Listens for the PS4 app's debug lines (sent as plain-text UDP datagrams, see
ps4/src/dlog.c) and prints them with a local timestamp.

The PS4 also mirrors every line to klog, so this viewer is just the convenient
path when you already have the PC nearby.

    python3 log_viewer.py                 # binds 0.0.0.0:47003
    python3 log_viewer.py --port 47003 --host 0.0.0.0
"""

import argparse
import configparser
import os
import socket
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def default_port(path):
    if path and os.path.exists(path):
        cp = configparser.ConfigParser()
        cp.read(path)
        if cp.has_section("log") and cp.has_option("log", "listen_port"):
            return cp.getint("log", "listen_port")
    return 47003


def main():
    ap = argparse.ArgumentParser(description="View PS4 debug logs over UDP.")
    ap.add_argument("--host", default="0.0.0.0", help="bind address")
    ap.add_argument("--port", type=int, default=None, help="UDP port")
    ap.add_argument("--config", default=os.path.join(HERE, "config.ini"))
    args = ap.parse_args()

    port = args.port if args.port is not None else default_port(args.config)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, port))
    print(f"[log_viewer] listening on udp {args.host}:{port} (Ctrl-C to quit)",
          flush=True)

    start = time.time()
    try:
        while True:
            data, addr = sock.recvfrom(2048)
            wall = time.strftime("%H:%M:%S")
            rel = time.time() - start
            text = data.decode("utf-8", "replace").rstrip("\r\n")
            print(f"{wall} (+{rel:7.2f}s) {addr[0]}  {text}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
