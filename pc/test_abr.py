"""Unit tests for the Step 4 adaptive bitrate controller.

Run with::

    cd pc
    python3 -m pytest -q

Everything here is pure: no sockets, no GStreamer, no clock. Time is injected
through ``now=`` so the recovery/cooldown/restore timing is exact and fast.
"""

import pytest

from abr import AdaptiveBitrateController
from protocol import FeedbackStatsPacket
from send_mjpeg import stats_interval_loss


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def make(**overrides):
    """Controller with the shipped defaults unless a test overrides them."""
    args = dict(
        start_bitrate=2_500_000,
        min_bitrate=600_000,
        max_bitrate=6_000_000,
        start_fps=30,
        min_fps=15,
        width=1280,
        height=720,
    )
    args.update(overrides)
    return AdaptiveBitrateController(**args)


def feed(ctrl, samples, t0=0.0):
    """Feed ``(dt, loss[, jitter, dropped, completed, allow])`` samples.

    The first sample is always evaluated as if one second had elapsed (that is
    what the controller does when it has no previous timestamp), then each
    following sample advances the clock by its own ``dt``. Returns the list of
    decisions (``None`` where the controller held).
    """
    decisions = []
    t = float(t0)
    for i, s in enumerate(samples):
        dt = float(s[0])
        loss = s[1]
        jitter = s[2] if len(s) > 2 else 0.0
        dropped = s[3] if len(s) > 3 else 0
        completed = s[4] if len(s) > 4 else 30
        allow = s[5] if len(s) > 5 else True
        if i:
            t += dt
        decisions.append(ctrl.update(
            loss, jitter_ms=jitter, frames_dropped=dropped,
            frames_completed=completed, allow_restart=allow, now=t))
    return decisions


def changes(decisions):
    return [d for d in decisions if d is not None]


def repeats(sample, count):
    return [sample] * count


# --------------------------------------------------------------------------
# construction / validation
# --------------------------------------------------------------------------
def test_initial_target_matches_config():
    ctrl = make()
    assert ctrl.target.bitrate == 2_500_000
    assert (ctrl.target.width, ctrl.target.height, ctrl.target.fps) == (1280, 720, 30)
    assert ctrl.last_reason == ""


def test_start_bitrate_is_clamped_into_bounds():
    assert make(start_bitrate=100).target.bitrate == 600_000
    assert make(start_bitrate=99_000_000).target.bitrate == 6_000_000


@pytest.mark.parametrize("kwargs", [
    dict(min_bitrate=0),
    dict(min_bitrate=7_000_000, max_bitrate=6_000_000),
    dict(decrease_factor=1.0),
    dict(decrease_factor=0.0),
    dict(increase_step=0.0),
    dict(loss_low=2.0, loss_high=2.0),
    dict(escalate_after=0),
])
def test_invalid_parameters_raise(kwargs):
    with pytest.raises(ValueError):
        make(**kwargs)


# --------------------------------------------------------------------------
# decrease
# --------------------------------------------------------------------------
def test_loss_above_threshold_decreases_by_factor():
    ctrl = make()
    d = ctrl.update(5.0, now=0.0)
    assert d is not None and d.kind == "bitrate"
    assert d.target.bitrate == 1_875_000          # 2.5 Mbps * 0.75
    assert "loss 5.0%" in d.reason and "1875000" in d.reason


def test_jitter_alone_triggers_decrease():
    ctrl = make()
    d = ctrl.update(0.0, jitter_ms=200.0, now=0.0)
    assert d is not None and d.kind == "bitrate"
    assert d.target.bitrate == 1_875_000


def test_frame_drop_ratio_alone_triggers_decrease():
    ctrl = make()
    # 15 of 30 frames lost = 50 % > drop_ratio_high (35 %)
    d = ctrl.update(0.0, frames_dropped=15, frames_completed=15, now=0.0)
    assert d is not None and d.kind == "bitrate"
    assert d.target.bitrate == 1_875_000
    assert "frame drop 50%" in d.reason


def test_moderate_frame_drop_ratio_does_not_defeat_hysteresis():
    # 20 % of frames lost is normal at ~0.9 % packet loss with 26-chunk AUs:
    # it must stay inside the hysteresis band, not force a decrease.
    ctrl = make()
    ds = feed(ctrl, [(1.0, 1.5, 0.0, 6, 24)] * 60)
    assert changes(ds) == []
    assert ctrl.target.bitrate == 2_500_000


def test_decrease_respects_cooldown():
    ctrl = make()
    ds = feed(ctrl, repeats((1.0, 6.0), 3))
    assert [d is not None for d in ds] == [True, False, True]
    assert ds[0].target.bitrate == 1_875_000
    assert ds[2].target.bitrate == 1_406_250      # 1.875 Mbps * 0.75


def test_bitrate_never_goes_below_minimum():
    ctrl = make()
    ds = feed(ctrl, repeats((1.0, 25.0), 40))
    assert ctrl.target.bitrate == 600_000
    assert all(d.target.bitrate >= 600_000 for d in changes(ds))
    # Once at the floor the controller stops issuing bitrate decisions.
    assert [d.kind for d in changes(ds)][-1] in ("bitrate", "fps", "resolution")


# --------------------------------------------------------------------------
# increase / hysteresis
# --------------------------------------------------------------------------
def test_clean_samples_increase_by_five_percent():
    ctrl = make()
    # 4 clean seconds: still short of increase_interval_s (5 s).
    assert changes(feed(ctrl, repeats((1.0, 0.2), 4))) == []
    # The 5th clean second trips the additive increase.
    d = ctrl.update(0.2, now=4.0)
    assert d is not None and d.kind == "bitrate"
    assert d.target.bitrate == 2_625_000          # +5 %
    assert "stable 5s" in d.reason
    # Another increase needs another full interval, not the very next sample.
    assert ctrl.update(0.2, now=5.0) is None
    assert ctrl.target.bitrate == 2_625_000


def test_increase_never_exceeds_max():
    ctrl = make(start_bitrate=5_900_000)
    ds = feed(ctrl, repeats((1.0, 0.0), 120))
    assert ctrl.target.bitrate == 6_000_000
    assert all(d.target.bitrate <= 6_000_000 for d in changes(ds))


def test_hysteresis_band_holds_and_never_oscillates():
    """1–2 % loss must neither decrease nor increase, however long it lasts."""
    ctrl = make()
    ds = feed(ctrl, repeats((1.0, 1.5), 120))
    assert changes(ds) == []
    assert ctrl.target.bitrate == 2_500_000


def test_no_increase_during_recovery_hold():
    ctrl = make()
    assert ctrl.update(5.0, now=0.0) is not None      # decrease at t=0
    # Two clean samples: 2 s < recovery_hold_s (3 s) -> hold.
    assert ctrl.update(0.0, now=1.0) is None
    assert ctrl.update(0.0, now=2.0) is None
    assert ctrl.target.bitrate == 1_875_000


def test_one_burst_then_slow_recovery_without_oscillation():
    ctrl = make()
    ds = feed(ctrl, repeats((1.0, 8.0), 3) + repeats((1.0, 0.3), 60))
    kinds = [d.kind for d in changes(ds)]
    assert kinds[0] == "bitrate"
    # Two decreases fit in the 3 s burst (cooldown = 2 s), then only increases.
    bitrates = [d.target.bitrate for d in changes(ds) if d.kind == "bitrate"]
    assert bitrates[:2] == [1_875_000, 1_406_250]
    assert bitrates[1:] == sorted(bitrates[1:])
    # It recovers past the starting point (up to max_bitrate) but never collapses
    # again while the link stays clean.
    assert 2_000_000 <= ctrl.target.bitrate <= 6_000_000
    assert all(d.kind == "bitrate" for d in changes(ds))


# --------------------------------------------------------------------------
# escalation: bitrate floor -> fps -> resolution
# --------------------------------------------------------------------------
def test_escalates_fps_then_resolution_at_floor():
    ctrl = make(start_bitrate=600_000)                # already at the floor
    ds = feed(ctrl, repeats((1.0, 9.0), 12))
    seen = [(d.kind, d.target.fps, d.target.width, d.target.height)
            for d in changes(ds)]
    assert seen == [
        ("fps", 20, 1280, 720),
        ("fps", 15, 1280, 720),
        ("resolution", 15, 960, 540),
        ("resolution", 15, 640, 360),
    ]
    # Ladder exhausted: further loss produces no more changes.
    assert changes(feed(ctrl, repeats((1.0, 9.0), 9))) == []


def test_escalation_takes_three_bad_samples():
    ctrl = make(start_bitrate=600_000)
    ds = feed(ctrl, repeats((1.0, 9.0), 3))
    assert [d is not None for d in ds] == [False, False, True]


def test_escalation_is_deferred_while_restart_is_disallowed():
    ctrl = make(start_bitrate=600_000)
    # allow_restart=False: the caller cannot restart yet, so hold the target.
    ds = feed(ctrl, [(1.0, 9.0, 0.0, 0, 30, False)] * 10)
    assert changes(ds) == []
    assert ctrl.target.fps == 30
    # Once a restart is allowed, the accumulated bad streak escalates at once.
    d = ctrl.update(9.0, allow_restart=True, now=99.0)
    assert d is not None and d.kind == "fps" and d.target.fps == 20


def test_escalation_never_upscales_past_start_geometry():
    ctrl = make(start_bitrate=600_000, width=960, height=540, start_fps=30)
    feed(ctrl, repeats((1.0, 9.0), 40))
    assert ctrl.target.width <= 960 and ctrl.target.height <= 540
    assert ctrl.target.fps >= 15


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------
def test_quality_is_restored_only_after_long_clean_period():
    # Pin max_bitrate at the start so additive increase cannot muddy the test.
    ctrl = make(start_bitrate=1_000_000, min_bitrate=1_000_000,
                max_bitrate=1_000_000)
    feed(ctrl, repeats((1.0, 9.0), 12))
    assert (ctrl.target.fps, ctrl.target.width) == (15, 640)
    assert ctrl.target.height == 360

    # 19 clean seconds: not yet (timestamps must stay continuous, otherwise
    # the controller sees one huge dt and restores immediately).
    assert changes(feed(ctrl, repeats((1.0, 0.1), 19), t0=12.0)) == []
    # The 20th clean second restores resolution first.
    ds = feed(ctrl, repeats((1.0, 0.1), 1), t0=31.0)
    assert [d.kind for d in changes(ds)] == ["resolution"]
    assert (ds[0].target.width, ds[0].target.height) == (960, 540)

    ds = feed(ctrl, repeats((1.0, 0.1), 20), t0=32.0)
    assert [d.kind for d in changes(ds)] == ["resolution"]
    assert (ctrl.target.width, ctrl.target.height) == (1280, 720)

    ds = feed(ctrl, repeats((1.0, 0.1), 20), t0=52.0)
    assert [d.kind for d in changes(ds)] == ["fps"] and ctrl.target.fps == 20
    ds = feed(ctrl, repeats((1.0, 0.1), 20), t0=72.0)
    assert [d.kind for d in changes(ds)] == ["fps"] and ctrl.target.fps == 30


def test_restore_waits_for_bitrate_to_return_to_start():
    ctrl = make(max_bitrate=3_000_000)
    # Sustained loss with restarts disallowed: bitrate bottoms out, but the
    # caller says it cannot restart the pipeline, so fps/resolution stay put.
    feed(ctrl, [(1.0, 9.0, 0.0, 0, 30, False)] * 40)
    assert ctrl.target.bitrate == 600_000
    assert ctrl.target.fps == 30

    # Once restarts are allowed, the accumulated bad streak escalates at once.
    d = ctrl.update(9.0, allow_restart=True, now=40.0)
    assert d is not None and d.kind == "fps" and d.target.fps == 20

    # Clean samples: the bitrate climbs (+5 % / 5 s) but the restore gate needs
    # it back at its starting value, which takes a while.
    ds = feed(ctrl, repeats((1.0, 0.0), 60), t0=41.0)
    assert [d for d in changes(ds) if d.kind != "bitrate"] == []
    assert ctrl.target.bitrate < 2_500_000
    assert ctrl.target.fps == 20

    # Enough clean time both restores the bitrate and then the fps.
    ds = feed(ctrl, repeats((1.0, 0.0), 200), t0=101.0)
    assert ctrl.target.bitrate >= 2_500_000
    assert [d for d in changes(ds) if d.kind == "fps"]


# --------------------------------------------------------------------------
# STATS delta helper used by send_mjpeg.py
# --------------------------------------------------------------------------
def _stats(seq, expected, received, completed, dropped, jitter=0, queue=0):
    return FeedbackStatsPacket(seq=seq, packets_expected=expected,
                               packets_received=received,
                               frames_completed=completed,
                               frames_dropped=dropped,
                               jitter_ms=jitter, last_frame_id=0,
                               decoder_queue=queue)


def test_stats_interval_loss_uses_deltas():
    prev = _stats(1, 1000, 1000, 300, 2)
    cur = _stats(2, 2000, 1980, 600, 5)
    assert stats_interval_loss(prev, cur) == pytest.approx((2.0, 3, 300))


def test_stats_interval_loss_ignores_first_sample_and_restart():
    cur = _stats(1, 1000, 900, 300, 40)
    assert stats_interval_loss(None, cur) is None
    # Receiver restarted: counters went backwards.
    assert stats_interval_loss(cur, _stats(1, 10, 10, 5, 0)) is None
    # No packets expected in the interval.
    a = _stats(1, 100, 100, 10, 0)
    assert stats_interval_loss(a, a) is None


def test_stats_interval_loss_clamps_to_100_percent():
    prev = _stats(1, 100, 100, 10, 0)
    cur = _stats(2, 200, 100, 20, 0)
    loss, dropped, completed = stats_interval_loss(prev, cur)
    assert loss == 100.0 and dropped == 0 and completed == 10


# --------------------------------------------------------------------------
# Step 6: FEC strength follows the same bad/good classification
# --------------------------------------------------------------------------
def test_fec_starts_at_the_configured_group():
    c = make(fec_group=8)
    assert c.target.fec_group == 8
    assert c.fec_enabled


def test_fec_tightens_on_a_bad_sample_and_logs_why():
    c = make(fec_group=8, fec_group_bad=4)
    [d] = feed(c, [(1.0, 5.0)])
    assert c.target.fec_group == 4
    # The bitrate decrease wins the return value (it is the bigger change),
    # but the FEC reason is still recorded and the target already moved.
    assert d is not None and d.kind == "bitrate"
    assert "fec group 8->4" in c.last_fec_reason
    assert "loss 5.0% > 2.0%" in c.last_fec_reason


def test_fec_change_alone_returns_none_but_moves_the_target():
    # Already at the floor and escalation is far away: only FEC can react, and
    # the sender polls target.fec_group every frame, so no decision is needed.
    c = make(start_bitrate=600_000, min_bitrate=600_000, fec_group=8,
             fec_group_bad=4, escalate_after=100)
    [d] = feed(c, [(1.0, 9.0)])
    assert d is None
    assert c.target.fec_group == 4
    assert "fec group 8->4" in c.last_fec_reason


def test_fec_relaxes_when_the_link_is_clean_again():
    c = make(fec_group=8, fec_group_bad=4, fec_group_good=8)
    feed(c, [(1.0, 9.0)])
    assert c.target.fec_group == 4
    c.update(0.0, now=2.0)
    c.update(0.0, now=3.0)
    assert c.target.fec_group == 8
    assert "fec group 4->8" in c.last_fec_reason


def test_fec_holds_inside_the_hysteresis_band():
    c = make(fec_group=8)
    c.update(9.0, now=0.0)         # 8 -> 4
    assert c.target.fec_group == 4
    c.update(1.5, now=1.0)         # between loss_recover and loss_high
    assert c.target.fec_group == 4


def test_fec_disabled_by_default_stays_disabled():
    c = make()
    assert c.target.fec_group == 0
    assert not c.fec_enabled
    feed(c, [(1.0, 20.0), (1.0, 20.0)])
    assert c.target.fec_group == 0
    assert c.last_fec_reason == ""


def test_fec_group_validation():
    with pytest.raises(ValueError):
        make(fec_group=17)
    with pytest.raises(ValueError):
        make(fec_group_bad=-1)


def test_fec_change_leaves_bitrate_and_geometry_alone():
    c = make(start_bitrate=600_000, min_bitrate=600_000, fec_group=8,
             fec_group_bad=4, escalate_after=100)
    before = (c.target.bitrate, c.target.fps, c.target.width, c.target.height)
    c.update(9.0, now=0.0)
    assert c.target.fec_group == 4
    assert (c.target.bitrate, c.target.fps, c.target.width,
            c.target.height) == before
