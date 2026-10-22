"""The strategy forecast: where the driver ends up, and who is either side."""

from types import SimpleNamespace

import pytest

from pitwall.engine.pitwall import PitWallEngine, StrategyOption


def _driver(number: int, code: str, position: int, gap: float | None):
    return SimpleNamespace(
        driver_number=number,
        code=code,
        position=position,
        gap_to_leader=gap,
        reporting=True,
    )


def _option(delta: float) -> StrategyOption:
    return StrategyOption(
        strategy_id="stay",
        label="Stay on Mediums",
        pit_lap=None,
        compound="MEDIUM",
        expected_total_s=1000.0,
        p10_s=990.0,
        p50_s=1000.0,
        p90_s=1010.0,
        delta_vs_current_s=delta,
    )


def _frame(*drivers):
    return SimpleNamespace(drivers=list(drivers))


def test_staying_out_leaves_the_position_alone() -> None:
    frame = _frame(
        _driver(1, "LEC", 1, 0.0),
        _driver(44, "VER", 2, 9.9),
        _driver(4, "NOR", 3, 13.6),
    )
    driver = frame.drivers[1]
    option = _option(0.0)
    PitWallEngine._project_position(frame, driver, option)
    assert option.position_after == 2
    assert option.ahead_of == "LEC"
    assert option.behind_of == "NOR"


def test_gaining_time_passes_the_car_in_front() -> None:
    """A stop that is quicker than staying out catches the car ahead."""
    frame = _frame(
        _driver(1, "LEC", 1, 0.0),
        _driver(44, "VER", 2, 9.9),
        _driver(4, "NOR", 3, 13.6),
    )
    driver = frame.drivers[1]
    option = _option(-12.0)  # twelve seconds gained
    PitWallEngine._project_position(frame, driver, option)
    # 9.9 - 12 = -2.1, which is in front of the leader's 0.0, so the driver
    # takes the lead and LEC is now the car immediately behind.
    assert option.position_after == 1
    assert option.ahead_of is None
    assert option.behind_of == "LEC"


def test_losing_time_lets_the_cars_behind_through() -> None:
    frame = _frame(
        _driver(1, "LEC", 1, 0.0),
        _driver(44, "VER", 2, 9.9),
        _driver(4, "NOR", 3, 13.6),
    )
    driver = frame.drivers[1]
    option = _option(20.0)  # a slow stop
    PitWallEngine._project_position(frame, driver, option)
    # 9.9 + 20 = 29.9, which is behind NOR's 13.6, so NOR comes through
    # and the driver is now third, with NOR immediately ahead.
    assert option.position_after == 3
    assert option.ahead_of == "NOR"
    assert option.behind_of is None


def test_a_driver_who_stopped_reporting_is_not_in_the_race() -> None:
    """A driver whose timing feed stopped cannot be overtaken."""
    frame = _frame(
        _driver(1, "LEC", 1, 0.0),
        _driver(44, "VER", 2, 9.9),
        SimpleNamespace(
            driver_number=4,
            code="NOR",
            position=3,
            gap_to_leader=13.6,
            reporting=False,
        ),
    )
    driver = frame.drivers[1]
    option = _option(20.0)
    PitWallEngine._project_position(frame, driver, option)
    # NOR is not counted, so nobody comes through.
    assert option.position_after == 2


def test_without_a_gap_there_is_no_forecast() -> None:
    frame = _frame(_driver(44, "VER", 2, None))
    driver = frame.drivers[0]
    option = _option(0.0)
    PitWallEngine._project_position(frame, driver, option)
    assert option.position_after is None
    assert option.ahead_of is None


@pytest.mark.parametrize("delta,expected", [(0.0, 2), (-12.0, 1), (20.0, 3)])
def test_the_forecast_is_a_function_of_the_time_gained(delta: float, expected: int) -> None:
    frame = _frame(
        _driver(1, "LEC", 1, 0.0),
        _driver(44, "VER", 2, 9.9),
        _driver(4, "NOR", 3, 13.6),
    )
    option = _option(delta)
    PitWallEngine._project_position(frame, frame.drivers[1], option)
    assert option.position_after == expected
