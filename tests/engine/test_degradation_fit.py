"""The degradation estimator.

The regression this replaces computed the fuel mass from the lap number, so
the fuel never reset at a pit stop and the fitted degradation collapsed to
almost nothing.  These tests pin the behaviour that fixes it.
"""

import numpy as np
import pytest

from pitwall.engine.calibrate import (
    FUEL_BURN_KG_PER_LAP,
    FUEL_EFFECT_S_PER_KG,
    _detect_cliff,
    _measured_slopes,
    _outlier_mask,
)


def _stint(
    driver: int,
    compound: str,
    start_lap: int,
    length: int,
    *,
    net_slope: float,
    level: float = 100.0,
    age_at_start: int = 0,
) -> list[dict]:
    """A stint whose lap times follow ``level + net_slope * age``."""
    samples = []
    for index in range(length):
        age = age_at_start + index
        samples.append(
            {
                "driver_number": driver,
                "lap_number": start_lap + index,
                "duration": level + net_slope * age,
                "compound": compound,
                "age": age,
                "stint_start": start_lap,
            }
        )
    return samples


def test_the_measured_slope_is_the_net_of_wear_and_fuel() -> None:
    """The within-stint slope is what the data actually says."""
    samples = _stint(1, "SOFT", 1, 25, net_slope=-0.01)
    samples += _stint(2, "SOFT", 1, 25, net_slope=-0.01, level=100.4)
    slopes = _measured_slopes(samples)
    assert slopes["SOFT"] == pytest.approx(-0.01, abs=1e-6)


def test_a_pit_lap_does_not_drag_the_slope() -> None:
    """The lap a driver pits on is not a lap time."""
    samples = _stint(1, "MEDIUM", 1, 20, net_slope=-0.02)
    samples[3]["duration"] += 25.0
    slopes = _measured_slopes(samples)
    assert slopes["MEDIUM"] == pytest.approx(-0.02, abs=1e-6)


def test_short_stints_are_not_measured() -> None:
    """Three laps cannot say anything about a slope."""
    samples = _stint(1, "HARD", 1, 3, net_slope=-0.02)
    assert _measured_slopes(samples) == {}


def test_the_true_degradation_is_the_slope_plus_the_fuel_effect() -> None:
    """The split that the fit cannot make itself."""
    net = -0.03
    samples = _stint(1, "MEDIUM", 1, 30, net_slope=net)
    samples += _stint(2, "MEDIUM", 31, 30, net_slope=net, level=100.2)
    slope = _measured_slopes(samples)["MEDIUM"]
    true_degradation = slope + FUEL_EFFECT_S_PER_KG * FUEL_BURN_KG_PER_LAP
    # With the published fuel effect the answer is a realistic degradation.
    assert 0.03 <= true_degradation <= 0.08


def test_a_straight_line_has_no_cliff() -> None:
    """A tyre that degrades linearly has no cliff, and the fit says so."""
    ages = np.arange(1, 31, dtype=float)
    residuals = np.zeros_like(ages)
    b2, cliff = _detect_cliff(ages, residuals)
    assert b2 == 0.0
    assert cliff == 999.0


def test_a_fall_away_is_detected_as_a_cliff() -> None:
    """The residual rises and stays up, so the tyre has gone off."""
    ages = np.arange(1, 41, dtype=float)
    residuals = np.where(ages >= 20, 1.0, 0.0)
    b2, cliff = _detect_cliff(ages, residuals)
    assert cliff == 20.0
    assert b2 > 0.0


def test_one_slow_lap_is_not_a_cliff() -> None:
    """A single outlier must not be read as the tyre falling away."""
    ages = np.arange(1, 41, dtype=float)
    residuals = np.zeros_like(ages)
    residuals[30] = 3.0
    b2, cliff = _detect_cliff(ages, residuals)
    assert cliff == 999.0


def test_a_curve_that_flattens_is_not_a_cliff() -> None:
    """The Austin races degrade concavely; that is not a cliff."""
    ages = np.arange(1, 41, dtype=float)
    residuals = 1.0 - 0.05 * ages
    b2, cliff = _detect_cliff(ages, residuals)
    assert cliff == 999.0


def test_the_outlier_filter_keeps_the_middle_of_the_pack() -> None:
    values = np.array([10.0, 10.1, 10.2, 10.1, 40.0, 10.0, 10.3])
    mask = _outlier_mask(values)
    assert mask.tolist() == [False, False, False, False, True, False, False]
