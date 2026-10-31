"""Packet pacing for Reverse Remote Play (Step 5).

An H.264 access unit leaves the encoder as one burst of ~20-250 UDP
datagrams. Handing that whole burst to the kernel at once makes the Wi-Fi
driver queue it in one go: the AP sees an instantaneous 2-25 Mbit/s spike per
frame, shares the airtime badly, and the tail of the burst is what gets
retransmitted (or dropped) when the link is congested. Spreading the datagrams
over part of the frame interval keeps the *average* rate identical while
flattening the peak.

``PacketPacer`` is deliberately pure apart from two injectable callables
(``clock`` and ``sleep``), so the tests can run a virtual clock:

    pacer = PacketPacer(ratio=0.6, fps=30)
    for frame in frames:
        pacer.begin_frame(chunk_count(len(frame)))
        for dgram in iter_chunks(...):
            pacer.wait()          # may sleep ~gap seconds
            sock.sendto(dgram, dest)

Behaviour
---------
* ``ratio`` is the fraction of the **frame interval** used to send one frame
  (``0.5``-``0.7`` is the useful range, ``0`` disables pacing). The frame is
  sent in ``ratio / fps`` seconds, so the peak is ``1 / ratio`` times the
  average instead of "everything at once".
* Sleeping happens **before** a datagram, and only when the accumulated lag is
  at least ``min_sleep`` (0.5 ms). A 250-chunk keyframe therefore gets one
  sleep every few packets instead of 250 syscalls, while a 20-chunk P-frame is
  paced almost per packet.
* When the clock is already past a packet's slot the pacer **does not try to
  catch up**: it sends immediately and re-anchors, because bursting to make up
  a missed deadline is exactly what pacing exists to avoid. ``late_frames``
  counts frames in which that happened (the encode+send loop simply cannot
  keep up at that fps).
* ``begin_frame`` re-anchors every frame, so debt from a slow frame never turns
  into a burst in the next one.
* The pacer never blocks on the network: ``send_mjpeg.py`` keeps the socket
  non-blocking and still drops the rest of an access unit when the send buffer
  is full. Pacing only decides *when* a datagram is handed to the kernel.
"""

import time

from protocol import chunk_count  # noqa: F401  (re-exported for callers)

__all__ = ["PacketPacer", "chunk_count"]


class PacketPacer:
    """Spread one frame's datagrams over ``ratio`` of the frame interval."""

    def __init__(self, ratio, fps, clock=time.monotonic, sleep=time.sleep,
                 min_sleep=0.0005):
        ratio = float(ratio)
        if ratio < 0 or ratio > 1:
            raise ValueError(f"pacing ratio must be 0..1 (got {ratio})")
        self.ratio = ratio
        self.fps = int(fps)
        self._clock = clock
        self._sleep = sleep
        self.min_sleep = float(min_sleep)
        self._gap = 0.0
        self._n = 0
        self._deadline = 0.0
        self._late = False
        # counters (for the 1 Hz line and the final summary)
        self.frames = 0
        self.packets = 0
        self.sleeps = 0
        self.slept_s = 0.0
        self.late_frames = 0

    # -- configuration ---------------------------------------------------
    @property
    def enabled(self):
        return self.ratio > 0 and self.fps > 0

    def set_fps(self, fps):
        """Follow an ABR restart (the frame interval changed)."""
        self.fps = int(fps)

    def frame_budget(self):
        """Seconds of the frame interval this pacer may use (0 if disabled)."""
        if not self.enabled:
            return 0.0
        return self.ratio / self.fps

    def describe(self):
        return (f"{self.ratio:.2f} of the frame interval "
                f"({self.frame_budget() * 1000:.1f} ms at {self.fps} fps)")

    # -- per-frame -------------------------------------------------------
    def begin_frame(self, n_packets):
        """Start a frame made of ``n_packets`` datagrams.

        Returns the per-packet gap in seconds (0 when pacing is off).
        """
        self.frames += 1
        if not self.enabled or n_packets <= 0:
            self._n = 0
            self._gap = 0.0
            return 0.0
        self._n = int(n_packets)
        self._gap = self.frame_budget() / self._n
        self._late = False
        # Anchor the first slot one gap ahead so the very first datagram is
        # still spaced instead of being sent instantly.
        self._deadline = self._clock() + self._gap
        return self._gap

    # -- per-packet ------------------------------------------------------
    def wait(self):
        """Block until the next datagram's slot. Returns seconds slept."""

        if self._n <= 0 or self._gap <= 0:
            return 0.0
        now = self._clock()
        delay = self._deadline - now
        self.packets += 1

        if delay <= 0:
            # Already past this slot when we got here: the frame is behind
            # schedule. Do not burst to catch up — re-anchor and send now.
            # Only a real stall (> one min_sleep) is reported; the few
            # microseconds of normal loop jitter are not interesting.
            if -delay >= self.min_sleep and not self._late:
                self._late = True
                self.late_frames += 1
            self._deadline = now + self._gap
            return 0.0

        if delay >= self.min_sleep:
            self._sleep(delay)
            self.slept_s += delay
            self.sleeps += 1

        self._deadline += self._gap
        now = self._clock()
        if self._deadline <= now:
            # The sleep overshot (kernel granularity) or the loop body was
            # slow. Re-anchor instead of carrying the debt, otherwise every
            # overshoot would push the rest of the frame further behind.
            self._deadline = now + self._gap
        return delay

    # -- reporting -------------------------------------------------------
    def summary(self):
        return (f"pacing: ratio={self.ratio:.2f} ({self.frame_budget() * 1000:.1f} ms"
                f"/frame at {self.fps} fps), {self.frames} frames, "
                f"{self.packets} packets, {self.sleeps} sleeps, "
                f"{self.slept_s:.1f}s slept, {self.late_frames} late frames")
