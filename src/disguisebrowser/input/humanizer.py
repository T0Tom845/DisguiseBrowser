"""Kinematic models for human-like input: pure functions, no I/O.

Mouse: Fitts's-law durations, cubic Bézier paths with perpendicular curvature,
minimum-jerk velocity profile, physiological tremor and occasional overshoot
with correction. Keyboard: log-normal inter-key intervals modulated by
character class and punctuation pauses. Scroll: wheel notches with ease-out.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class HumanProfile:
    name: str = "default"
    speed: float = 1.0
    sample_rate_hz: float = 125.0
    curvature: float = 0.22
    tremor_px: float = 0.6
    overshoot_probability: float = 0.25
    typing_cpm: float = 280.0
    hesitation_probability: float = 0.02


PRESETS: dict[str, HumanProfile] = {
    "default": HumanProfile(),
    "careful": HumanProfile(name="careful", speed=0.7, overshoot_probability=0.1, typing_cpm=200.0),
    "fast": HumanProfile(name="fast", speed=1.4, curvature=0.15, typing_cpm=380.0),
}


@dataclass(frozen=True)
class PathPoint:
    x: float
    y: float
    t: float
    """Milliseconds since the start of the movement."""


Point = tuple[float, float]


def fitts_duration(
    distance: float, target_width: float, rng: random.Random, profile: HumanProfile
) -> float:
    """Movement time in ms: ``a + b·log2(D/W + 1)`` with ±15 % jitter."""
    a, b = 110.0, 140.0
    index = math.log2(distance / max(target_width, 1.0) + 1.0)
    duration = (a + b * index) / profile.speed * rng.uniform(0.85, 1.15)
    return max(60.0, min(duration, 2500.0))


def minimum_jerk(tau: float) -> float:
    """Normalized minimum-jerk displacement, ``tau`` in [0, 1]."""
    return 10 * tau**3 - 15 * tau**4 + 6 * tau**5


def cubic_bezier(p0: Point, p1: Point, p2: Point, p3: Point, s: float) -> Point:
    u = 1.0 - s
    x = u**3 * p0[0] + 3 * u**2 * s * p1[0] + 3 * u * s**2 * p2[0] + s**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * s * p1[1] + 3 * u * s**2 * p2[1] + s**3 * p3[1]
    return x, y


def mouse_path(
    start: Point,
    end: Point,
    *,
    rng: random.Random,
    profile: HumanProfile = PRESETS["default"],
    target_width: float = 24.0,
    allow_overshoot: bool = True,
) -> list[PathPoint]:
    """Sampled trajectory from ``start`` to ``end``; the last point is exactly ``end``."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance < 1.0:
        return [PathPoint(end[0], end[1], 0.0)]

    if allow_overshoot and distance > 150 and rng.random() < profile.overshoot_probability:
        ux, uy = dx / distance, dy / distance
        over = distance * rng.uniform(0.02, 0.08)
        side = rng.uniform(-4.0, 4.0)
        over_point = (end[0] + ux * over - uy * side, end[1] + uy * over + ux * side)
        first = mouse_path(
            start, over_point, rng=rng, profile=profile, target_width=target_width,
            allow_overshoot=False,
        )  # fmt: skip
        pause = rng.uniform(20.0, 70.0)
        offset = first[-1].t + pause
        correction = mouse_path(
            over_point, end, rng=rng, profile=profile, target_width=target_width,
            allow_overshoot=False,
        )  # fmt: skip
        return first + [PathPoint(p.x, p.y, p.t + offset) for p in correction]

    duration = fitts_duration(distance, target_width, rng, profile)
    perp = (-dy / distance, dx / distance)
    off1 = distance * profile.curvature * rng.uniform(-1.0, 1.0)
    off2 = distance * profile.curvature * rng.uniform(-0.6, 0.6)
    c1 = (start[0] + dx * 0.3 + perp[0] * off1, start[1] + dy * 0.3 + perp[1] * off1)
    c2 = (start[0] + dx * 0.7 + perp[0] * off2, start[1] + dy * 0.7 + perp[1] * off2)

    samples = max(2, int(duration / 1000.0 * profile.sample_rate_hz))
    step = duration / samples
    points: list[PathPoint] = []
    for i in range(1, samples + 1):
        tau = i / samples
        s = minimum_jerk(tau)
        x, y = cubic_bezier(start, c1, c2, end, s)
        if i < samples:
            # Tremor fades as the hand decelerates onto the target.
            damping = 1.0 - s
            x += rng.gauss(0.0, profile.tremor_px) * damping
            y += rng.gauss(0.0, profile.tremor_px) * damping
            t = (i - rng.uniform(0.0, 0.35)) * step
        else:
            x, y = end
            t = duration
        points.append(PathPoint(x, y, t))
    return points


_PUNCTUATION_PAUSE = frozenset(".,;:!?")


def typing_delays(
    text: str, *, rng: random.Random, profile: HumanProfile = PRESETS["default"]
) -> list[float]:
    """Delay in ms *before* each character of ``text``."""
    mean = 60_000.0 / profile.typing_cpm
    delays: list[float] = []
    previous = ""
    for index, char in enumerate(text):
        if index == 0:
            delay = rng.uniform(80.0, 250.0)
        else:
            delay = mean * math.exp(rng.gauss(0.0, 0.35))
            if char.isupper() or char.isdigit():
                delay *= 1.25
            elif not char.isalnum() and not char.isspace():
                delay *= 1.4
            if previous == " ":
                delay *= 1.15
            if previous in _PUNCTUATION_PAUSE:
                delay += rng.uniform(150.0, 400.0)
            if rng.random() < profile.hesitation_probability:
                delay += rng.uniform(400.0, 1200.0)
        delays.append(delay)
        previous = char
    return delays


WHEEL_NOTCH_PX = 53.0
"""Pixels per wheel notch as Chrome on Linux reports them (3 lines × ~17.67 px)."""


def scroll_steps(
    delta_y: float, *, rng: random.Random, profile: HumanProfile = PRESETS["default"]
) -> list[tuple[float, float]]:
    """``(deltaY, pause_ms_after)`` wheel events that add up to ``delta_y``."""
    if abs(delta_y) < 1.0:
        return []
    sign = 1.0 if delta_y > 0 else -1.0
    remaining = abs(delta_y)
    total = remaining
    steps: list[tuple[float, float]] = []
    while remaining > 0.5:
        notches = 1 if remaining < 2 * WHEEL_NOTCH_PX else rng.choice((1, 1, 2, 2, 3))
        chunk = min(remaining, notches * WHEEL_NOTCH_PX)
        progress = 1.0 - remaining / total
        # Flicks start quick and ease out: pauses grow towards the end of the gesture.
        pause = rng.lognormvariate(math.log(45.0 + 90.0 * progress), 0.3) / profile.speed
        steps.append((sign * chunk, pause))
        remaining -= chunk
    return steps
