"""Adaptive bitrate controller for Reverse Remote Play (Step 4).

Pure, I/O-free decision logic: feed it one sample per PS4 STATS interval and it
returns ``None`` (nothing to do) or an :class:`AbrDecision` describing the new
target and why it changed. ``pc/send_mjpeg.py`` applies the decision:

* bitrate changes are written to the encoder's control channel (live, no
  restart);
* fps / resolution changes restart the capture pipeline (rate-limited by the
  caller, at most once every 5 s).

Policy
------
* **Decrease** quickly: loss above ``loss_high`` (default 2 %), high jitter, or
  a pathological frame-drop ratio multiplies the bitrate by ``decrease_factor``
  (0.75), clamped to ``min_bitrate``. A cooldown stops a single burst from
  collapsing the bitrate in consecutive samples. ``drop_ratio_high`` defaults to
  0.45, not something small: an access unit is ~26 chunks, so even 1 % packet
  loss already loses ~23 % of frames — a low threshold would make the loss
  hysteresis band meaningless (0.45 ≈ 2.3 % packet loss).
* **Increase** slowly: after ``recovery_hold_s`` since the last decrease and
  ``increase_interval_s`` of *clean* samples (loss below ``loss_low``, default
  1 %), add ``increase_step`` (5 %). The 1–2 % band is hysteresis: inside it the
  controller holds, so a link sitting near the threshold cannot oscillate.
* **Escalate quality** only when already at ``min_bitrate`` and loss persists:
  fps 30→20→15, then resolution 1280×720→960×540→640×360. Never upscales past
  the configured start geometry (the PS4 allocates buffers for that size).
* **Restore quality** only after a long clean period and once the bitrate has
  recovered to its starting value.

The controller never raises or blocks; it is deterministic given the same
sequence of ``(loss, jitter, dropped, completed, now, allow_restart)`` inputs.
"""

from dataclasses import dataclass, replace
import time


@dataclass(frozen=True)
class AbrTarget:
    bitrate: int
    fps: int
    width: int
    height: int


@dataclass(frozen=True)
class AbrDecision:
    target: AbrTarget
    reason: str
    kind: str          # "bitrate" | "fps" | "resolution"


def _stress_reason(loss, loss_high, jitter, jitter_high, drop_ratio, drop_high):
    """Name the signal(s) that made this sample count as bad.

    Logging only "loss > x %" is misleading when jitter or frame drops are the
    actual trigger, so list every condition that fired.
    """
    parts = []
    if loss > loss_high:
        parts.append(f"loss {loss:.1f}% > {loss_high:.1f}%")
    if jitter > jitter_high:
        parts.append(f"jitter {jitter:.0f}ms > {jitter_high:.0f}ms")
    if drop_ratio > drop_high:
        parts.append(f"frame drop {drop_ratio * 100:.0f}% > {drop_high * 100:.0f}%")
    return " + ".join(parts) or "stressed link"


class AdaptiveBitrateController:
    """Hysteretic, multiplicative-decrease / additive-increase controller."""

    def __init__(
        self,
        *,
        start_bitrate,
        min_bitrate,
        max_bitrate,
        start_fps=30,
        min_fps=15,
        width=1280,
        height=720,
        loss_high=2.0,
        loss_low=1.0,
        decrease_factor=0.75,
        decrease_cooldown_s=2.0,
        increase_step=0.05,
        increase_interval_s=5.0,
        recovery_hold_s=3.0,
        jitter_high_ms=80.0,
        drop_ratio_high=0.45,
        escalate_after=3,
        restore_interval_s=20.0,
        fps_ladder=(30, 20, 15),
        resolution_ladder=((1280, 720), (960, 540), (640, 360)),
    ):
        min_bitrate = int(min_bitrate)
        max_bitrate = int(max_bitrate)
        if min_bitrate <= 0 or max_bitrate < min_bitrate:
            raise ValueError("need 0 < min_bitrate <= max_bitrate")
        if not (0.0 < decrease_factor < 1.0):
            raise ValueError("decrease_factor must be in (0, 1)")
        if increase_step <= 0:
            raise ValueError("increase_step must be positive")
        if loss_low >= loss_high:
            raise ValueError("loss_low must be below loss_high (hysteresis)")
        if escalate_after < 1:
            raise ValueError("escalate_after must be >= 1")

        self.min_bitrate = min_bitrate
        self.max_bitrate = max_bitrate
        self.start_bitrate = max(min_bitrate, min(max_bitrate, int(start_bitrate)))
        self.start_fps = int(start_fps)
        self.min_fps = int(min_fps)
        self.width = int(width)
        self.height = int(height)
        self.loss_high = float(loss_high)
        self.loss_low = float(loss_low)
        self.decrease_factor = float(decrease_factor)
        self.decrease_cooldown_s = float(decrease_cooldown_s)
        self.increase_step = float(increase_step)
        self.increase_interval_s = float(increase_interval_s)
        self.recovery_hold_s = float(recovery_hold_s)
        self.jitter_high_ms = float(jitter_high_ms)
        self.drop_ratio_high = float(drop_ratio_high)
        self.escalate_after = int(escalate_after)
        self.restore_interval_s = float(restore_interval_s)

        # FPS ladder: highest first, never below min_fps or above start_fps.
        ladder = sorted({self.start_fps, *(int(f) for f in fps_ladder)},
                        reverse=True)
        self._fps_ladder = [f for f in ladder
                            if self.min_fps <= f <= self.start_fps] or [self.start_fps]

        # Resolution ladder: start geometry first, then smaller steps only.
        res = [(self.width, self.height)]
        for w, h in resolution_ladder:
            w, h = int(w), int(h)
            if (w, h) not in res and w <= self.width and h <= self.height:
                res.append((w, h))
        self._res_ladder = res

        self.target = AbrTarget(self.start_bitrate, self.start_fps,
                                self.width, self.height)
        self.last_reason = ""

        self._last_time = None
        self._since_decrease = 1e9      # allow a decrease on the first sample
        self._good_s = 0.0
        self._stable_s = 0.0
        self._bad_streak = 0

    # -- helpers -----------------------------------------------------------
    def _decision(self, kind, reason):
        self.last_reason = reason
        return AbrDecision(target=self.target, reason=reason, kind=kind)

    def _fps_index(self):
        try:
            return self._fps_ladder.index(self.target.fps)
        except ValueError:
            return 0

    def _res_index(self):
        try:
            return self._res_ladder.index((self.target.width, self.target.height))
        except ValueError:
            return 0

    def _escalate_quality(self):
        """Drop fps one step, then resolution one step. Returns a decision."""
        idx = self._fps_index()
        if idx + 1 < len(self._fps_ladder):
            old = self.target
            new_fps = self._fps_ladder[idx + 1]
            self.target = replace(old, fps=new_fps)
            return self._decision(
                "fps",
                f"min bitrate and loss persists: fps {old.fps}->{new_fps}")
        idx = self._res_index()
        if idx + 1 < len(self._res_ladder):
            old = self.target
            w, h = self._res_ladder[idx + 1]
            self.target = replace(old, width=w, height=h)
            return self._decision(
                "resolution",
                f"min bitrate and {self.target.fps} fps, loss persists: "
                f"{old.width}x{old.height}->{w}x{h}")
        return None

    def _restore_quality(self):
        """Raise resolution first, then fps (reverse of escalation)."""
        idx = self._res_index()
        if idx > 0:
            old = self.target
            w, h = self._res_ladder[idx - 1]
            self.target = replace(old, width=w, height=h)
            return self._decision(
                "resolution",
                f"stable {self.restore_interval_s:.0f}s at {self.target.bitrate} bps: "
                f"{old.width}x{old.height}->{w}x{h}")
        idx = self._fps_index()
        if idx > 0:
            old = self.target
            new_fps = self._fps_ladder[idx - 1]
            self.target = replace(old, fps=new_fps)
            return self._decision(
                "fps",
                f"stable {self.restore_interval_s:.0f}s at {self.target.bitrate} bps: "
                f"fps {old.fps}->{new_fps}")
        return None

    # -- main entry point --------------------------------------------------
    def update(self, loss_pct, jitter_ms=0.0, frames_dropped=0,
               frames_completed=0, allow_restart=True, now=None):
        """Feed one interval sample.

        ``loss_pct`` is the loss *within this interval* (not cumulative).
        ``frames_dropped``/``frames_completed`` are also per-interval deltas.
        ``allow_restart`` is False when the caller cannot restart the pipeline
        yet (the 5 s restart rate limit); fps/resolution changes are then
        deferred, so the controller's target always matches reality.

        Returns an :class:`AbrDecision` on a change, otherwise ``None``.
        """
        now = time.monotonic() if now is None else float(now)
        if self._last_time is None:
            dt = 1.0
        else:
            dt = max(0.0, now - self._last_time)
        self._last_time = now
        self._since_decrease += dt

        loss_pct = max(0.0, float(loss_pct))
        jitter_ms = max(0.0, float(jitter_ms))
        dropped = max(0, int(frames_dropped))
        completed = max(0, int(frames_completed))
        total = dropped + completed
        drop_ratio = (dropped / total) if total else 0.0

        bad = (loss_pct > self.loss_high
               or jitter_ms > self.jitter_high_ms
               or drop_ratio > self.drop_ratio_high)
        good = (loss_pct < self.loss_low
                and jitter_ms <= self.jitter_high_ms
                and drop_ratio <= self.drop_ratio_high)

        if bad:
            self._bad_streak += 1
            self._good_s = 0.0
            self._stable_s = 0.0
        else:
            self._bad_streak = 0
            if good:
                self._good_s += dt
                self._stable_s += dt
            else:
                # Hysteresis band: neither increase nor restore quality.
                self._good_s = 0.0
                self._stable_s = 0.0

        # 1. Multiplicative decrease while above the bitrate floor.
        if bad and self.target.bitrate > self.min_bitrate:
            if self._since_decrease >= self.decrease_cooldown_s:
                old = self.target.bitrate
                new = max(self.min_bitrate,
                          int(round(old * self.decrease_factor)))
                if new < old:
                    self._since_decrease = 0.0
                    self._bad_streak = 0
                    self._good_s = 0.0
                    self._stable_s = 0.0
                    self.target = replace(self.target, bitrate=new)
                    return self._decision(
                        "bitrate",
                        _stress_reason(loss_pct, self.loss_high, jitter_ms,
                                       self.jitter_high_ms, drop_ratio,
                                       self.drop_ratio_high)
                        + f": bitrate {old}->{new}")

        # 2. At the floor: escalate quality after sustained loss.
        elif bad and self._bad_streak >= self.escalate_after:
            if allow_restart:
                self._bad_streak = 0
                self._since_decrease = 0.0
                self._good_s = 0.0
                self._stable_s = 0.0
                decision = self._escalate_quality()
                if decision is not None:
                    return decision

        # 3. Slow additive increase after a sustained clean period.
        if (not bad
                and self._since_decrease >= self.recovery_hold_s
                and self._good_s >= self.increase_interval_s
                and self.target.bitrate < self.max_bitrate):
            old = self.target.bitrate
            new = min(self.max_bitrate,
                      max(old + 1, int(round(old * (1.0 + self.increase_step)))))
            self._good_s = max(0.0, self._good_s - self.increase_interval_s)
            if new > old:
                self.target = replace(self.target, bitrate=new)
                return self._decision(
                    "bitrate",
                    f"stable {self.increase_interval_s:.0f}s below "
                    f"{self.loss_low:.1f}% loss: bitrate {old}->{new}")

        # 4. Very slow quality restoration once the bitrate is fully back.
        if (allow_restart and good and not bad
                and self._stable_s >= self.restore_interval_s
                and self.target.bitrate >= self.start_bitrate):
            decision = self._restore_quality()
            if decision is not None:
                self._stable_s = 0.0
                return decision

        return None
