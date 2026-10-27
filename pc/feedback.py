"""PC-side feedback listener (PS4 -> PC): STATS and KEYFRAME_REQUEST.

Step 2 of the weak-network hardening plan. A single small daemon thread drains
a UDP socket and parses `RP4F` datagrams. The sender loop only reads the latest
snapshot, so nothing here can block video transmission.

Design notes
------------
* The parser validates magic, version and type, so malformed datagrams and
  unknown future types are silently ignored (forward compatible).
* KEYFRAME_REQUESTs are *coalesced*: one pending flag is enough, because a
  single IDR resynchronises the decoder after any number of lost frames.
* This module performs no I/O on the sender thread and is safe to use from
  ``--no-send`` runs, which makes the whole channel testable on one PC via
  ``pc/tools/fake_ps4_stats.py``.
"""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from protocol import (  # noqa: E402
    FeedbackStatsPacket,
    KeyframeRequestPacket,
    parse_feedback,
)


class FeedbackListener:
    """Listen for PS4 feedback packets on ``port``.

    Usage::

        fb = FeedbackListener(47002).start()
        ...
        stats, ts = fb.latest()          # FeedbackStatsPacket | None
        if fb.take_keyframe_request():   # 1 if one is pending (clears it)
            ...
        fb.stop()
    """

    def __init__(self, port, host="0.0.0.0"):
        self.port = int(port)
        self.host = host
        self._sock = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()

        self._latest = None
        self._latest_time = 0.0
        self._stats_packets = 0
        self._kf_requests = 0
        self._kf_pending = 0
        self._malformed = 0
        self._last_reason = None
        self._last_kf_time = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.settimeout(0.2)
        self._sock = s
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="feedback-listener", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed by stop()

            pkt = parse_feedback(data)
            if pkt is None:
                with self._lock:
                    self._malformed += 1
                continue

            now = time.monotonic()
            if isinstance(pkt, FeedbackStatsPacket):
                with self._lock:
                    self._latest = pkt
                    self._latest_time = now
                    self._stats_packets += 1
            elif isinstance(pkt, KeyframeRequestPacket):
                with self._lock:
                    self._kf_requests += 1
                    self._kf_pending = 1
                    self._last_kf_time = now
                    self._last_reason = pkt.reason

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- queries used by the sender ---------------------------------------
    def latest(self):
        """Return (FeedbackStatsPacket | None, monotonic timestamp)."""
        with self._lock:
            return self._latest, self._latest_time

    def take_keyframe_request(self):
        """Return 1 if a KEYFRAME_REQUEST is pending, clearing it."""
        with self._lock:
            if self._kf_pending:
                self._kf_pending = 0
                return 1
            return 0

    def counters(self):
        with self._lock:
            return {
                "stats": self._stats_packets,
                "kf_requests": self._kf_requests,
                "malformed": self._malformed,
                "last_reason": self._last_reason,
            }
