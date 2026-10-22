"""Tests for the pit-wall strategy engine.

Synthetic fixtures only: the engine is exercised against a hand-built race
frame, never against the live API (GR8).
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from pitwall.data.models import FlagState
from pitwall.engine.calibration import CircuitCalibration
from pitwall.engine.pitwall import PitWallEngine


@dataclass
class FakeDriver:
    driver_number: int
    code: str
    name: str = ""
    team: str = ""
    team_colour: str = "#ffffff"
    position: int | None = None
    speed: float | None = None
    compound: str | None = None
    tyre_age: int | None = None
    gap_to_leader: float | None = None
    lap: int | None = None
    last_lap_s: float | None = None
    on_track: bool = True


@dataclass
class FakeFrame:
    circuit: str = "Test"
    year: int = 2024
    flag_state: FlagState = FlagState.GREEN
    lap: int | None = 10
    total_laps: int | None = 50
    drivers: list[FakeDriver] = field(default_factory=list)


def _bundle(**overrides: Any) -> CircuitCalibration:
    values: dict[str, Any] = {
        "circuit": "Test",
        "year": 2024,
        "pit_loss_s": 21.0,
        "base_pace_s": 99.0,
        "fuel_burn_rate_kg_per_lap": 1.5,
        "fuel_effect_s_per_kg": 0.05,
        "sc_probability": 0.0,
        "tyre_cliff_soft": 18.0,
        "tyre_cliff_medium": 28.0,
        "tyre_cliff_hard": 40.0,
        "degradation": {"MEDIUM": [0.03, 0.0, 999.0], "HARD": [0.028, 0.0, 999.0]},
        "compound_offset": {"MEDIUM": 1.0, "HARD": 0.0},
        "compound_offset_agreement": 0.95,
        "compound_offset_samples": 12,
        "degradation_max_age": 30,
        "source": "unit test fixture",
    }
    values.update(overrides)
    return CircuitCalibration(**values)


def _frame() -> FakeFrame:
    return FakeFrame(
        drivers=[
            FakeDriver(
                driver_number=1,
                code="AAA",
                position=1,
                compound="MEDIUM",
                tyre_age=8,
                gap_to_leader=0.0,
                lap=10,
                last_lap_s=99.0,
            ),
            FakeDriver(
                driver_number=2,
                code="BBB",
                position=2,
                compound="MEDIUM",
                tyre_age=8,
                gap_to_leader=1.5,
                lap=10,
                last_lap_s=99.8,
            ),
        ]
    )


def test_report_contains_real_tyre_and_pace_state() -> None:
    engine = PitWallEngine(_bundle())
    report = engine.build_report(
        frame=_frame(),
        driver_number=2,
        elapsed_s=990.0,
        stint={"stint_number": 1, "lap_start": 1, "compound": "MEDIUM"},
        rival=None,
        driver_pace=99.8,
    )

    assert report.available
    assert report.tyre.compound == "MEDIUM"
    assert report.tyre.age_laps == 8
    assert report.tyre.degradation_s is not None
    assert report.tyre.degradation_s == pytest.approx(0.24, abs=0.01)
    # A linear fit means there is no cliff to report.
    assert report.tyre.projected_cliff_lap is None


def test_staying_out_is_ranked_when_switching_gains_nothing() -> None:
    """With no compound offset, a stop only costs time, so hold."""
    engine = PitWallEngine(_bundle(compound_offset={"MEDIUM": 0.0, "HARD": 0.0}), n_sims=300)
    report = engine.build_report(
        frame=_frame(),
        driver_number=1,
        elapsed_s=990.0,
        stint={"stint_number": 1, "lap_start": 1, "compound": "MEDIUM"},
        rival=None,
        driver_pace=99.0,
    )

    assert report.options
    best = report.options[0]
    assert best.pit_lap is None, "the best option should be to stay out"
    assert best.delta_vs_current_s == pytest.approx(0.0)

    pitting = [o for o in report.options if o.pit_lap is not None]
    assert pitting, "pitting options must be offered"
    # Pitting has to pay the stop, so it cannot be free.
    assert min(o.delta_vs_current_s for o in pitting) > 0


def test_switching_to_a_faster_compound_is_recommended() -> None:
    """A measured compound advantage must show up in the call."""
    engine = PitWallEngine(
        _bundle(compound_offset={"MEDIUM": 1.0, "HARD": 0.0}), n_sims=300
    )
    report = engine.build_report(
        frame=_frame(),
        driver_number=1,
        elapsed_s=990.0,
        stint={"stint_number": 1, "lap_start": 1, "compound": "MEDIUM"},
        rival=None,
        driver_pace=99.0,
    )
    compounds = {o.compound for o in report.options}
    assert "HARD" in compounds, "a reliable offset must allow a switch to be ranked"
    assert report.recommendation in ("PIT", "WAIT")
    assert report.recommendation_reason


def test_pitting_gains_when_the_tyre_is_past_its_window() -> None:
    """A tyre that has gone off should make a stop attractive."""
    engine = PitWallEngine(_bundle(degradation={"MEDIUM": [0.12, 0.0, 999.0]}))
    report = engine.build_report(
        frame=_frame(),
        driver_number=1,
        elapsed_s=990.0,
        stint={"stint_number": 1, "lap_start": 1, "compound": "MEDIUM"},
        rival=None,
        driver_pace=99.0,
    )
    pitting = [o for o in report.options if o.pit_lap is not None]
    assert pitting
    assert min(o.delta_vs_current_s for o in pitting) < 0, "a stop should now pay"


def test_unreliable_compound_offset_stops_the_model_ranking_a_switch() -> None:
    """With no agreement between drivers, a compound switch is not ranked."""
    engine = PitWallEngine(_bundle(compound_offset_agreement=0.4, compound_offset_samples=3))
    report = engine.build_report(
        frame=_frame(),
        driver_number=1,
        elapsed_s=990.0,
        stint={"stint_number": 1, "lap_start": 1, "compound": "MEDIUM"},
        rival=None,
        driver_pace=99.0,
    )
    compounds = {o.compound for o in report.options}
    assert compounds == {"MEDIUM"}, "only the current compound may be ranked"
    assert report.compound_tradeoff["reliable"] is False
    assert report.modelled_compounds == ["HARD", "MEDIUM"]


def test_undercut_alert_needs_a_car_ahead() -> None:
    engine = PitWallEngine(_bundle())
    report = engine.build_report(
        frame=_frame(),
        driver_number=2,
        elapsed_s=990.0,
        stint={"stint_number": 1, "lap_start": 1, "compound": "MEDIUM"},
        rival=_frame().drivers[0],
        driver_pace=99.8,
    )
    kinds = [alert["type"] for alert in report.alerts]
    assert "UNDERCUT_THREAT" in kinds


def test_alerts_fire_only_once_per_driver() -> None:
    engine = PitWallEngine(_bundle())
    frame = _frame()
    kwargs: dict[str, Any] = {
        "frame": frame,
        "driver_number": 2,
        "elapsed_s": 990.0,
        "stint": {"stint_number": 1, "lap_start": 1, "compound": "MEDIUM"},
        "rival": frame.drivers[0],
        "driver_pace": 99.8,
    }
    first = engine.build_report(**kwargs)
    second = engine.build_report(**kwargs)
    assert [a["type"] for a in first.alerts]
    assert second.alerts == [], "an alert must not fire twice for the same driver"


def test_recommendation_is_hold_when_staying_is_best() -> None:
    engine = PitWallEngine(
        _bundle(compound_offset={"MEDIUM": 0.0, "HARD": 0.0}), n_sims=300
    )
    report = engine.build_report(
        frame=_frame(),
        driver_number=1,
        elapsed_s=990.0,
        stint={"stint_number": 1, "lap_start": 1, "compound": "MEDIUM"},
        rival=None,
        driver_pace=99.0,
    )
    assert report.recommendation == "HOLD"
    assert report.recommendation_reason
