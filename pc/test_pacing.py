"""Tests for the Step 5 packet pacer (pc/pacing.py).

The pacer takes injected ``clock``/``sleep`` callables, so these tests run on a
virtual clock: ``FakeClock.sleep`` advances time instead of blocking, which
makes the assertions exact and the suite instant.
"""

import pytest

from pacing import PacketPacer, chunk_count
from protocol import VIDEO_CHUNK_PAYLOAD, iter_chunks


class FakeClock:
    def __init__(self, start=1000.0, overshoot=0.0):
        self.t = start
        self.calls = []
        self.overshoot = overshoot

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.calls.append(seconds)
        self.t += seconds + self.overshoot


def make(ratio=0.6, fps=30, start=1000.0, overshoot=0.0, **kw):
    clock = FakeClock(start, overshoot)
    return clock, PacketPacer(ratio, fps, clock=clock, sleep=clock.sleep, **kw)


# --- geometry ------------------------------------------------------------

def test_frame_budget_is_a_fraction_of_the_frame_interval():
    _, p = make(ratio=0.6, fps=30)
    assert p.frame_budget() == pytest.approx(0.020)
    _, p = make(ratio=0.5, fps=25)
    assert p.frame_budget() == pytest.approx(0.020)


def test_gap_splits_the_budget_over_every_datagram():
    _, p = make(ratio=0.6, fps=30)
    gap = p.begin_frame(10)
    assert gap == pytest.approx(0.002)


def test_disabled_when_ratio_is_zero():
    clock, p = make(ratio=0)
    assert not p.enabled
    assert p.frame_budget() == 0.0
    assert p.begin_frame(50) == 0.0
    for _ in range(50):
        assert p.wait() == 0.0
    assert clock.calls == []


def test_disabled_for_zero_fps():
    _, p = make(ratio=0.6, fps=0)
    assert not p.enabled
    assert p.begin_frame(5) == 0.0
    assert p.wait() == 0.0


@pytest.mark.parametrize("ratio", [-0.1, 1.5, 2])
def test_ratio_is_validated(ratio):
    with pytest.raises(ValueError):
        PacketPacer(ratio, 30)


# --- spreading ------------------------------------------------------------

def test_a_frame_is_spread_over_its_budget():
    clock, p = make(ratio=0.6, fps=30)
    start = clock.t
    n = 20
    p.begin_frame(n)
    for _ in range(n):
        p.wait()
    # 20 packets * 1 ms = the 20 ms budget, not a single instant.
    assert clock.t - start == pytest.approx(0.020, abs=1e-9)
    assert len(clock.calls) == n
    assert p.packets == n
    assert p.late_frames == 0


def test_no_pacing_when_the_frame_is_empty():
    clock, p = make()
    assert p.begin_frame(0) == 0.0
    assert p.wait() == 0.0
    assert clock.calls == []


def test_large_keyframe_batches_sleeps_instead_of_sleeping_per_packet():
    clock, p = make(ratio=0.6, fps=30)
    n = 250                                    # ~296 KB keyframe
    gap = p.begin_frame(n)
    assert gap < p.min_sleep                   # sub-millisecond gaps
    for _ in range(n):
        p.wait()
    # One sleep per few packets: far fewer syscalls than packets ...
    assert 0 < p.sleeps < n // 4
    # ... but the frame as a whole still takes ~its full budget.
    assert p.slept_s == pytest.approx(0.020, rel=0.15)


def test_pacing_scales_with_fps_after_an_abr_restart():
    clock, p = make(ratio=0.6, fps=30)
    p.set_fps(15)
    assert p.frame_budget() == pytest.approx(0.040)
    start = clock.t
    p.begin_frame(4)
    for _ in range(4):
        p.wait()
    assert clock.t - start == pytest.approx(0.040)


# --- falling behind -------------------------------------------------------

def test_behind_schedule_sends_now_and_counts_one_late_frame():
    clock, p = make(ratio=0.6, fps=30)
    p.begin_frame(20)
    clock.t += 1.0                             # encoder was 1 s late
    assert p.wait() == 0.0                     # no catch-up burst
    assert p.late_frames == 1
    for _ in range(19):
        p.wait()
    assert p.late_frames == 1                  # counted once per frame
    # Re-anchored: the rest of the frame is paced, not dumped.
    assert p.sleeps >= 18


def test_late_flag_resets_on_the_next_frame():
    clock, p = make(ratio=0.6, fps=30)
    p.begin_frame(4)
    clock.t += 1.0
    p.wait()
    assert p.late_frames == 1
    p.begin_frame(4)
    for _ in range(4):
        p.wait()
    assert p.late_frames == 1


def test_each_frame_re_anchors_so_debt_does_not_burst():
    clock, p = make(ratio=0.6, fps=30)
    p.begin_frame(20)
    for _ in range(20):
        p.wait()                               # frame 1: full budget
    # Frame 2 starts late but must still be spaced out, not sent at once.
    clock.t += 0.5
    start = clock.t
    p.begin_frame(20)
    for _ in range(20):
        p.wait()
    assert clock.t - start == pytest.approx(0.020, abs=1e-9)


# --- chunk counting -------------------------------------------------------

@pytest.mark.parametrize("size", [0, 1, VIDEO_CHUNK_PAYLOAD,
                                  VIDEO_CHUNK_PAYLOAD + 1, 3000, 296000])
def test_chunk_count_matches_iter_chunks(size):
    payload = bytes(size)
    assert chunk_count(size) == len(list(iter_chunks(1, payload)))


def test_summary_mentions_the_ratio_and_frame():
    _, p = make(ratio=0.6, fps=30)
    p.begin_frame(10)
    for _ in range(10):
        p.wait()
    text = p.summary()
    assert "ratio=0.60" in text
    assert "20.0 ms/frame" in text
    assert "10 packets" in text


# --- sleep granularity ----------------------------------------------------

def test_sleep_overshoot_does_not_count_as_late():
    clock, p = make(ratio=0.6, fps=30, overshoot=0.00012)
    start = clock.t
    n = 20
    p.begin_frame(n)
    for _ in range(n):
        p.wait()
    assert p.late_frames == 0
    # Re-anchoring after each overshoot means the frame costs
    # budget + one overshoot, not budget + n * overshoot.
    assert clock.t - start == pytest.approx(0.020 + 0.00012, abs=1e-4)


def test_micro_jitter_is_not_reported_as_late():
    clock, p = make(ratio=0.6, fps=30)
    gap = p.begin_frame(20)
    clock.t += gap + 0.0002               # late by less than min_sleep
    assert p.wait() == 0.0
    assert p.late_frames == 0


def test_a_real_stall_mid_frame_is_reported_once():
    clock, p = make(ratio=0.6, fps=30)
    p.begin_frame(20)
    p.wait()
    clock.t += 0.010                      # 10 ms hiccup
    p.wait()
    assert p.late_frames == 1
    for _ in range(18):
        p.wait()
    assert p.late_frames == 1
