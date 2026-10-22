"""The pit wall must not change its mind on noise.

The model is re-run every second.  Two candidate stops that are within the
model's own noise of each other can swap places between one run and the
next, which would make the card flicker between "box on lap 27" and "box on
lap 28" while the race has not moved a metre.
"""

import pytest

from pitwall.engine.calibration import CircuitCalibration
from pitwall.engine.pitwall import PitWallEngine, StrategyOption


def _bundle() -> CircuitCalibration:
    return CircuitCalibration(
        circuit="austin",
        year=2024,
        pit_loss_s=21.0,
        base_pace_s=99.4,
        fuel_burn_rate_kg_per_lap=1.5,
        fuel_effect_s_per_kg=0.05,
        sc_probability=0.0,
        tyre_cliff_soft=999.0,
        tyre_cliff_medium=999.0,
        tyre_cliff_hard=999.0,
        degradation={"MEDIUM": [0.03, 0.0, 999.0]},
        source="unit test fixture",
    )


def _option(pit_lap: int | None, total: float) -> StrategyOption:
    return StrategyOption(
        strategy_id=f"pit-{pit_lap}",
        label=f"Box on lap {pit_lap}",
        pit_lap=pit_lap,
        compound="MEDIUM",
        expected_total_s=total,
        p10_s=total - 1.0,
        p50_s=total,
        p90_s=total + 1.0,
        delta_vs_current_s=total - 1000.0,
    )


def _engine() -> PitWallEngine:
    return PitWallEngine(_bundle(), n_sims=10)


def test_the_called_pit_lap_is_kept_within_a_lap() -> None:
    """Two stops a hundredth of a second apart must not swap the call."""
    engine = _engine()
    options = [_option(None, 1000.0), _option(28, 990.0), _option(27, 990.01)]

    call, _, pit_lap = engine._recommend(options, 24, 1, noise_sigma=0.5)
    assert call == "WAIT"
    assert pit_lap == 28

    # The next run has them the other way round, by a hundredth of a second.
    options = [_option(None, 1000.0), _option(28, 990.01), _option(27, 990.0)]
    call, _, pit_lap = engine._recommend(options, 24, 1, noise_sigma=0.5)
    assert pit_lap == 28, "the call must not move within the same lap"


def test_a_new_lap_may_change_the_call() -> None:
    """Hysteresis is per lap, not forever."""
    engine = _engine()
    options = [_option(None, 1000.0), _option(28, 990.0), _option(27, 989.0)]
    _, _, pit_lap = engine._recommend(options, 24, 1, noise_sigma=0.5)
    assert pit_lap == 27

    options = [_option(None, 1000.0), _option(28, 990.0), _option(27, 989.0)]
    _, _, pit_lap = engine._recommend(options, 25, 1, noise_sigma=0.5)
    assert pit_lap == 27


def test_a_stop_within_the_noise_of_staying_out_is_a_hold() -> None:
    """A stop that beats staying out by a tenth of a second is not a stop."""
    engine = _engine()
    options = [_option(None, 1000.0), _option(30, 999.9)]
    call, reason, _ = engine._recommend(options, 24, 1, noise_sigma=0.5)
    assert call == "HOLD"
    assert "within" in reason


def test_a_clear_gain_is_still_a_stop() -> None:
    engine = _engine()
    options = [_option(None, 1000.0), _option(30, 995.0)]
    call, _, pit_lap = engine._recommend(options, 24, 1, noise_sigma=0.5)
    assert call == "WAIT"
    assert pit_lap == 30


def test_two_drivers_get_their_own_calls() -> None:
    """The hysteresis is per driver, not global."""
    engine = _engine()
    options = [_option(None, 1000.0), _option(28, 990.0)]
    engine._recommend(options, 24, 1, noise_sigma=0.5)
    other = [_option(None, 1000.0), _option(31, 985.0)]
    _, _, pit_lap = engine._recommend(other, 24, 2, noise_sigma=0.5)
    assert pit_lap == 31


@pytest.mark.parametrize("gap", [0.01, 0.1, 0.49])
def test_a_small_reordering_never_moves_the_call(gap: float) -> None:
    engine = _engine()
    first = [_option(None, 1000.0), _option(28, 990.0), _option(27, 990.0 + gap)]
    _, _, pit_lap = engine._recommend(first, 24, 1, noise_sigma=0.5)
    second = [_option(None, 1000.0), _option(28, 990.0 + gap), _option(27, 990.0)]
    _, _, pit_lap_again = engine._recommend(second, 24, 1, noise_sigma=0.5)
    assert pit_lap == pit_lap_again
