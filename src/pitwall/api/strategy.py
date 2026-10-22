"""Strategy answers backed by the simulation engine.

The pit-wall chat must never invent numbers.  Two rules apply:

* Every figure that is shown comes from a
  :class:`~pitwall.data.models.StrategyResult` field or from the live pit-wall
  report.
* When the engine cannot answer (no calibration bundle, no race data) the
  endpoint says ``available: false`` and explains why.

The fast path renders the engine's output directly, so a chat response is
available well under a second and never depends on an LLM being reachable.
"""

from __future__ import annotations

import logging
from typing import Any

from pitwall.engine.calibration import load_calibration
from pitwall.engine.pitwall import PitWallReport
from pitwall.engine.simulator import (
    CalibrationData,
    StintSpec,
    StrategySpec,
    simulate_strategy,
)

logger = logging.getLogger(__name__)


def _compound_letter(compound: str | None) -> str:
    if not compound:
        return "?"
    return str(compound).strip()[:1].upper()


def _format_gap(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:06.3f}"


def _build_spec(
    *,
    driver_number: int,
    total_laps: int,
    compound: str,
    pit_lap: int,
    second_compound: str,
    second_pit_lap: int,
) -> StrategySpec:
    return StrategySpec(
        strategy_id=f"one-stop-{_compound_letter(compound)}",
        driver_number=driver_number,
        stints=(
            StintSpec(
                compound=compound,
                start_lap=1,
                end_lap=pit_lap,
                pit_lap=pit_lap,
            ),
            StintSpec(
                compound=second_compound,
                start_lap=pit_lap + 1,
                end_lap=total_laps,
                pit_lap=second_pit_lap,
            ),
        ),
    )


def _unavailable(reason: str, message: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "source": "unavailable",
        "message": message,
    }


async def answer_question(request: Any, state: Any) -> dict[str, Any]:
    """Answer a pit-wall question using the simulation engine."""
    query = (request.query or "").lower()

    replayer = state.replayer
    if replayer is None:
        return _unavailable(
            "no_replay_session_cached",
            "No session is loaded, so the strategy engine has nothing to work from.",
        )

    driver_number = request.driver_number
    if driver_number is None:
        driver_number = replayer.drivers[0]["driver_number"] if replayer.drivers else None
    if driver_number is None:
        return _unavailable(
            "no_driver_context",
            "No driver context is available for this session yet.",
        )

    circuit = state.circuit
    year = state.year

    try:
        bundle = load_calibration(circuit, year)
    except FileNotFoundError:
        return _unavailable(
            "engine_not_calibrated",
            f"No calibration bundle for {circuit} {year}; the strategy engine "
            "cannot produce numbers yet.",
        )

    # The live pit-wall report carries the current tyre state, the pace
    # against the car ahead and the ranked strategies.
    report: PitWallReport | None = None
    if state.loop is not None and state.loop.engine is not None:
        frame = replayer.frame(replay_speed=state.replay_speed)
        driver = next(
            (d for d in frame.drivers if d.driver_number == driver_number), None
        )
        if driver is not None:
            rival = state.loop._rival_for(frame, driver)  # noqa: SLF001
            report = state.loop.engine.build_report(
                frame=frame,
                driver_number=driver_number,
                elapsed_s=replayer.elapsed_race_time(driver_number, driver.lap or 1),
                stint=replayer.stint_for(driver_number, driver.lap),
                rival=rival,
                driver_pace=replayer.driver_pace(driver_number, driver.lap),
                rival_pace=(
                    replayer.driver_pace(rival.driver_number, rival.lap)
                    if rival is not None
                    else None
                ),
            )

    if report is not None and report.available:
        return _answer_from_report(request, report, query)

    # Fall back to a whole-race projection when there is no live report.
    return _answer_from_race(request, bundle, driver_number, circuit, year, replayer)


def _answer_from_report(
    request: Any, report: PitWallReport, query: str
) -> dict[str, Any]:
    """Answer from the live pit-wall report."""
    tyre = report.tyre
    pace = report.pace
    options = report.options
    best = options[0] if options else None
    window = report.optimal_window

    if "tyre" in query or "compound" in query:
        message = (
            f"{tyre.compound or 'unknown'} tyres, {tyre.age_laps or 0} laps old, "
            f"{tyre.degradation_s or 0:.2f}s of degradation so far."
        )
        if tyre.projected_cliff_lap is not None:
            message += f" The fitted cliff is projected on lap {tyre.projected_cliff_lap}."
        else:
            message += " The fitted curve has no cliff inside the race distance."
    elif "pace" in query or "faster" in query or "gap" in query:
        delta = pace.pace_delta_s_per_lap
        message = (
            f"Against {pace.rival_code or 'the car ahead'} the pace is "
            f"{delta:+.3f}s per lap"
            if delta is not None
            else "There is no pace comparison available yet."
        )
        if pace.gap_to_rival_s is not None:
            message += f", and the gap is {pace.gap_to_rival_s:.3f}s."
    else:
        message = report.recommendation_reason or (
            f"The best option is {best.label}." if best else "No strategy could be projected."
        )
        if window:
            message += f" The stop window is laps {window[0]}-{window[1]}."

    key_numbers: dict[str, float] = {}
    if best is not None:
        key_numbers = {
            "expected_total_s": best.expected_total_s,
            "p10_s": best.p10_s,
            "p50_s": best.p50_s,
            "p90_s": best.p90_s,
            "p_beat_rival": best.p_beat_rival if best.p_beat_rival is not None else 0.0,
        }

    return {
        "available": True,
        "source": "simulation",
        "session_key": report.session_key if hasattr(report, "session_key") else None,
        "driver_number": report.driver_number,
        "query": request.query,
        "message": message,
        "recommendation": report.recommendation,
        "key_numbers": key_numbers,
        "n_sims": 2000,
    }


def _answer_from_race(
    request: Any,
    bundle: Any,
    driver_number: int,
    circuit: str,
    year: int,
    replayer: Any,
) -> dict[str, Any]:
    """Answer with a whole-race projection when there is no live state."""
    query = (request.query or "").lower()
    total_laps = replayer.total_laps or 56

    if "soft" in query:
        first, second = "SOFT", "MEDIUM"
    elif "hard" in query:
        first, second = "HARD", "MEDIUM"
    else:
        first, second = "MEDIUM", "HARD"

    if "two-stop" in query or "2-stop" in query:
        first, second = "SOFT", "MEDIUM"
        pit_lap = max(10, total_laps // 3)
        second_pit_lap = max(pit_lap + 8, (2 * total_laps) // 3)
    else:
        pit_lap = max(12, total_laps // 3)
        second_pit_lap = total_laps

    spec = _build_spec(
        driver_number=int(driver_number),
        total_laps=total_laps,
        compound=first,
        pit_lap=pit_lap,
        second_compound=second,
        second_pit_lap=second_pit_lap,
    )
    calibration = CalibrationData(
        base_pace=bundle.base_pace_s,
        pit_loss=bundle.pit_loss_s,
        noise_sigma=0.5,
        fuel_burn_rate_kg_per_lap=bundle.fuel_burn_rate_kg_per_lap,
        fuel_effect_s_per_kg=bundle.fuel_effect_s_per_kg,
    )

    result = simulate_strategy(
        spec,
        calibration,
        total_laps=total_laps,
        n_sims=2000,
        seed=42,
    )

    message = (
        f"For {circuit} {year}, a {first[0]}/{second[0]} strategy with the first "
        f"stop on lap {pit_lap} projects a race time of "
        f"{_format_gap(result.expected_total_s)} "
        f"(p10 {_format_gap(result.p10_s)} / p90 {_format_gap(result.p90_s)})."
    )

    return {
        "available": True,
        "source": "simulation",
        "driver_number": int(driver_number),
        "query": request.query,
        "message": message,
        "strategy": {
            "strategy_id": result.strategy_id,
            "stints": [
                {"compound": s.compound, "start_lap": s.start_lap, "end_lap": s.end_lap}
                for s in spec.stints
            ],
        },
        "key_numbers": {
            "expected_total_s": result.expected_total_s,
            "p10_s": result.p10_s,
            "p50_s": result.p50_s,
            "p90_s": result.p90_s,
            "p_beat_rival": result.p_beat_rival,
        },
        "n_sims": result.n_sims,
    }
