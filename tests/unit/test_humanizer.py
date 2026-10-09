import math
import random
from itertools import pairwise

import pytest

from disguisebrowser.input.humanizer import (
    PRESETS,
    WHEEL_NOTCH_PX,
    fitts_duration,
    minimum_jerk,
    mouse_path,
    scroll_steps,
    typing_delays,
)


def test_minimum_jerk_endpoints_and_monotonic() -> None:
    assert minimum_jerk(0.0) == 0.0
    assert minimum_jerk(1.0) == pytest.approx(1.0)
    values = [minimum_jerk(i / 50) for i in range(51)]
    assert all(b >= a for a, b in pairwise(values))


def test_fitts_duration_grows_with_distance() -> None:
    rng = random.Random(1)
    short = sum(fitts_duration(50, 24, rng, PRESETS["default"]) for _ in range(50)) / 50
    far = sum(fitts_duration(1200, 24, rng, PRESETS["default"]) for _ in range(50)) / 50
    assert far > short
    assert 60 <= short <= 2500 and 60 <= far <= 2500


def test_mouse_path_ends_exactly_on_target_with_increasing_time() -> None:
    rng = random.Random(42)
    path = mouse_path((10, 10), (800, 500), rng=rng)
    assert (path[-1].x, path[-1].y) == (800, 500)
    assert path[0].t >= 0
    assert all(b.t > a.t for a, b in pairwise(path))
    assert len(path) > 10


def test_mouse_path_is_curved_not_straight() -> None:
    rng = random.Random(7)
    path = mouse_path((0, 0), (1000, 0), rng=rng, allow_overshoot=False)
    max_deviation = max(abs(p.y) for p in path)
    assert max_deviation > 5  # a human never draws a perfectly straight line


def test_mouse_path_speed_is_bounded() -> None:
    rng = random.Random(3)
    path = mouse_path((0, 0), (600, 400), rng=rng, allow_overshoot=False)
    speeds = [
        math.hypot(b.x - a.x, b.y - a.y) / max(b.t - a.t, 1e-6) * 1000 for a, b in pairwise(path)
    ]
    assert max(speeds) < 20_000  # px/s; real pointing devices top out far below this


def test_mouse_path_overshoot_still_lands_on_target() -> None:
    rng = random.Random(11)
    profile = PRESETS["default"].__class__(overshoot_probability=1.0)
    path = mouse_path((0, 0), (900, 300), rng=rng, profile=profile)
    assert (path[-1].x, path[-1].y) == (900, 300)
    assert all(b.t > a.t for a, b in pairwise(path))


def test_zero_distance_path() -> None:
    path = mouse_path((5, 5), (5, 5), rng=random.Random(0))
    assert len(path) == 1 and path[0].t == 0


def test_typing_delays_shape() -> None:
    rng = random.Random(5)
    text = "Hello, world. OK"
    delays = typing_delays(text, rng=rng)
    assert len(delays) == len(text)
    assert all(d > 0 for d in delays)
    after_comma = delays[text.index(" w")]
    assert after_comma > 150  # punctuation pause


def test_typing_speed_matches_profile() -> None:
    rng = random.Random(9)
    text = "the quick brown fox jumps over the lazy dog " * 20
    delays = typing_delays(text, rng=rng, profile=PRESETS["fast"])
    cpm = len(text) / (sum(delays) / 60_000)
    assert 200 < cpm < 500


def test_scroll_steps_sum_to_delta_and_use_notches() -> None:
    rng = random.Random(2)
    steps = scroll_steps(700, rng=rng)
    assert sum(d for d, _ in steps) == pytest.approx(700)
    assert all(d > 0 for d, _ in steps)
    assert all(abs(d) <= 3 * WHEEL_NOTCH_PX + 1e-6 for d, _ in steps)
    assert all(p > 0 for _, p in steps)
    down = scroll_steps(-300, rng=rng)
    assert sum(d for d, _ in down) == pytest.approx(-300)
    assert scroll_steps(0.2, rng=rng) == []
