from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pitwall.data.models import DriverState, FlagState, StrategyResult, WorldState, WorldStateDelta


def test_world_state_and_delta_import_and_validate() -> None:
    timestamp = datetime.now(UTC)
    driver = DriverState(driver_number=1, position=1, compound="MEDIUM", tyre_age_laps=5)
    state = WorldState(
        session_key="test-session",
        timestamp=timestamp,
        lap=5,
        total_laps=58,
        flag_state=FlagState.GREEN,
        drivers={1: driver},
    )
    delta = WorldStateDelta(session_key="test-session", timestamp=timestamp, lap=6)

    assert state.drivers[1].driver_number == 1
    assert delta.lap == 6


def test_invalid_rain_probability_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorldState(
            session_key="test-session",
            timestamp=datetime.now(UTC),
            lap=1,
            rain_probability=1.1,
        )


def test_strategy_result_validates_probability_and_simulation_count() -> None:
    result = StrategyResult(
        driver_number=1,
        strategy_id="one-stop-m-h",
        expected_total_s=5400,
        p10_s=5390,
        p50_s=5400,
        p90_s=5410,
        p_beat_rival=0.68,
        n_sims=10_000,
    )
    assert result.n_sims == 10_000
    with pytest.raises(ValidationError):
        StrategyResult(
            driver_number=1,
            strategy_id="invalid",
            expected_total_s=5400,
            p10_s=5390,
            p50_s=5400,
            p90_s=5410,
            p_beat_rival=1.2,
            n_sims=10_000,
        )
