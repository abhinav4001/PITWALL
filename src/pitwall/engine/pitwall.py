"""The pit-wall engine.

Turns the live race state into the handful of things a race engineer actually
needs to decide:

* how the tyres are doing and when the cliff arrives,
* how the car's pace compares with the car it is fighting,
* which strategies are available and how much each one is worth,
* when the pit window is open.

Every number is produced by the simulation engine from **fitted** calibration
constants, so the results are reproducible and traceable.  When the inputs are
missing the result is an explicit ``available: false`` payload rather than a
plausible-looking number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pitwall.data.models import FlagState
from pitwall.engine.calibration import CircuitCalibration
from pitwall.engine.simulator import (
    CalibrationData,
    RemainingStint,
    simulate_remaining,
)
from pitwall.realtime.triggers import TriggerEngine, TriggerEvent

logger = logging.getLogger(__name__)

#: Compounds the model can project.
COMPOUNDS = ("SOFT", "MEDIUM", "HARD")

#: How many laps ahead the optimiser looks for a pit stop.
PIT_LOOKAHEAD = 12


@dataclass
class TyreState:
    """The state of the tyres a driver is currently running."""

    compound: str | None
    age_laps: int | None
    stint_number: int | None
    laps_remaining: int | None
    degradation_s: float | None = None
    projected_cliff_lap: int | None = None
    cliff_laps_away: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "compound": self.compound,
            "age_laps": self.age_laps,
            "stint_number": self.stint_number,
            "laps_remaining": self.laps_remaining,
            "degradation_s": self.degradation_s,
            "projected_cliff_lap": self.projected_cliff_lap,
            "cliff_laps_away": self.cliff_laps_away,
        }


@dataclass
class PaceState:
    """How the car is doing compared with the car it is fighting."""

    rival_code: str | None = None
    rival_driver_number: int | None = None
    gap_to_rival_s: float | None = None
    pace_delta_s_per_lap: float | None = None
    reference: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rival_code": self.rival_code,
            "rival_driver_number": self.rival_driver_number,
            "gap_to_rival_s": self.gap_to_rival_s,
            "pace_delta_s_per_lap": self.pace_delta_s_per_lap,
            "reference": self.reference,
        }


@dataclass
class StrategyOption:
    """One candidate strategy and what it is worth."""

    strategy_id: str
    label: str
    pit_lap: int | None
    compound: str | None
    expected_total_s: float
    p10_s: float
    p50_s: float
    p90_s: float
    delta_vs_current_s: float
    p_beat_rival: float | None = None
    #: Where the driver would end up, and who would be either side of them.
    position_after: int | None = None
    ahead_of: str | None = None
    behind_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "label": self.label,
            "pit_lap": self.pit_lap,
            "compound": self.compound,
            "expected_total_s": self.expected_total_s,
            "p10_s": self.p10_s,
            "p50_s": self.p50_s,
            "p90_s": self.p90_s,
            "delta_vs_current_s": self.delta_vs_current_s,
            "p_beat_rival": self.p_beat_rival,
            "position_after": self.position_after,
            "ahead_of": self.ahead_of,
            "behind_of": self.behind_of,
        }


@dataclass
class PitWallReport:
    """The complete pit-wall picture for one driver."""

    driver_number: int
    circuit: str
    year: int
    lap: int | None
    total_laps: int | None
    flag_state: FlagState
    tyre: TyreState
    pace: PaceState
    options: list[StrategyOption] = field(default_factory=list)
    optimal_pit_lap: int | None = None
    optimal_window: tuple[int, int] | None = None
    recommendation: str = "HOLD"
    recommendation_reason: str = ""
    alerts: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    modelled_compounds: list[str] = field(default_factory=list)
    compound_tradeoff: dict[str, Any] = field(default_factory=dict)
    source: str = "simulation"
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "source": self.source,
            "driver_number": self.driver_number,
            "circuit": self.circuit,
            "year": self.year,
            "lap": self.lap,
            "total_laps": self.total_laps,
            "flag_state": self.flag_state.value,
            "tyre": self.tyre.to_dict(),
            "pace": self.pace.to_dict(),
            "options": [option.to_dict() for option in self.options],
            "optimal_pit_lap": self.optimal_pit_lap,
            "optimal_window": list(self.optimal_window) if self.optimal_window else None,
            "recommendation": self.recommendation,
            "recommendation_reason": self.recommendation_reason,
            "alerts": self.alerts,
            "messages": self.messages,
            "modelled_compounds": self.modelled_compounds,
            "compound_tradeoff": self.compound_tradeoff,
        }


class PitWallEngine:
    """Builds pit-wall reports from live race state."""

    def __init__(
        self,
        bundle: CircuitCalibration,
        *,
        n_sims: int = 2000,
        seed: int = 7,
    ) -> None:
        self.bundle = bundle
        self.n_sims = n_sims
        self.seed = seed
        self.triggers = TriggerEngine()
        #: The last call made for each driver, so the pit wall does not
        #: change its mind on noise.  See :meth:`_recommend`.
        self._calls: dict[int, dict[str, Any]] = {}
        #: The driver's own measured pace, filled in per report.
        self._driver_pace: float | None = None
        self._driver_compound: str | None = None

    # ------------------------------------------------------------------
    def _calibration(
        self, driver_pace: float | None = None, driver_compound: str | None = None
    ) -> CalibrationData:
        def degradation(compound: str, age: float) -> float:
            entry = (self.bundle.degradation or {}).get(str(compound).upper())
            if not entry:
                return 0.0
            b1, b2, cliff = entry
            b1, b2, cliff = float(b1), float(b2), float(cliff)
            max_age = float(self.bundle.degradation_max_age)
            if age <= max_age:
                return b1 * age + b2 * max(0.0, age - cliff) ** 2
            # Beyond the range the curve was fitted over, the quadratic term
            # would explode.  The wear is continued linearly at the slope the
            # fit had reached, which is the conservative extrapolation.
            value_at_max = b1 * max_age + b2 * max(0.0, max_age - cliff) ** 2
            slope_at_max = b1 + 2 * b2 * max(0.0, max_age - cliff)
            return value_at_max + slope_at_max * (age - max_age)

        offsets = dict(self.bundle.compound_offset or {})
        base_pace = self.bundle.base_pace_s
        if driver_pace is not None:
            # Anchor the projection on the driver's own measured pace and
            # re-base the compound offsets onto the compound they run now.
            base_pace = driver_pace
            if driver_compound:
                reference = float(offsets.get(driver_compound.upper(), 0.0))
                offsets = {
                    compound: value - reference for compound, value in offsets.items()
                }
        if not self.compound_offset_reliable:
            # The measured compound offset is not separable from the
            # strategy choice, so it is not used to shift anybody's pace.
            # Left in place it would tilt the ranking between compounds on
            # the strength of noise.
            offsets = {compound: 0.0 for compound in offsets}

        return CalibrationData(
            base_pace=base_pace,
            pit_loss=self.bundle.pit_loss_s,
            noise_sigma=0.5,
            fuel_burn_rate_kg_per_lap=self.bundle.fuel_burn_rate_kg_per_lap,
            fuel_effect_s_per_kg=self.bundle.fuel_effect_s_per_kg,
            degradation_model=degradation,
            compound_offset=offsets,
        )

    @property
    def modelled_compounds(self) -> list[str]:
        """The compounds the fitted model can actually project."""
        return sorted(
            compound
            for compound in COMPOUNDS
            if compound in (self.bundle.degradation or {})
        )

    @property
    def compound_offset_reliable(self) -> bool:
        """Whether the compound offset can be trusted to rank a switch.

        The compound a driver runs is tied to their strategy and the fuel load
        differs between stints, so the offset is only weakly separable.  When
        the drivers who ran two compounds do not agree, the model refuses to
        rank a compound switch rather than making a confident-looking call.
        """
        return (
            self.bundle.compound_offset_agreement >= 0.8
            and self.bundle.compound_offset_samples >= 5
        )

    # ------------------------------------------------------------------
    def _cliff_lap(self, compound: str | None, age: int | None) -> int | None:
        """The lap on which the fitted cliff is expected to arrive.

        A compound whose wear fitted as purely linear has no cliff; the
        calibration stores a cliff far beyond any race to say so.
        """
        if not compound or age is None:
            return None
        entry = (self.bundle.degradation or {}).get(str(compound).upper())
        if not entry or float(entry[1]) < 0.005:
            return None
        cliff = float(entry[2])
        if cliff > 200:
            return None
        return int(age) + int(cliff)

    def build_report(
        self,
        *,
        frame: Any,
        driver_number: int,
        elapsed_s: float,
        stint: dict[str, Any] | None,
        rival: Any = None,
        messages: list[dict[str, Any]] | None = None,
        driver_pace: float | None = None,
        rival_pace: float | None = None,
    ) -> PitWallReport:
        """Build the report for one driver."""
        driver = next(
            (d for d in frame.drivers if d.driver_number == driver_number), None
        )
        if driver is None:
            return PitWallReport(
                driver_number=driver_number,
                circuit=frame.circuit,
                year=frame.year,
                lap=frame.lap,
                total_laps=frame.total_laps,
                flag_state=frame.flag_state,
                tyre=TyreState(None, None, None, None),
                pace=PaceState(),
                available=False,
            )

        current_lap = driver.lap or 1
        total_laps = frame.total_laps or current_lap
        laps_remaining = max(0, total_laps - current_lap + 1)

        compound = driver.compound
        age = driver.tyre_age
        calibration = self._calibration(driver_pace, compound)
        tyre = TyreState(
            compound=compound,
            age_laps=age,
            stint_number=(int(stint.get("stint_number")) if stint else None),
            laps_remaining=laps_remaining,
        )
        if compound and age is not None:
            tyre.degradation_s = calibration.degradation(compound, age)
            cliff_lap = self._cliff_lap(compound, age)
            tyre.projected_cliff_lap = cliff_lap
            tyre.cliff_laps_away = None if cliff_lap is None else cliff_lap - current_lap

        # --- pace against the car we are fighting ------------------------
        pace = PaceState()
        if rival is not None:
            pace = PaceState(
                rival_code=getattr(rival, "code", None),
                rival_driver_number=getattr(rival, "driver_number", None),
                gap_to_rival_s=getattr(rival, "gap_to_leader", None),
                reference="model_expected",
            )
            delta = self._pace_delta(frame, driver, rival)
            pace.pace_delta_s_per_lap = delta

        # --- strategy options -------------------------------------------
        options = self._rank_strategies(
            frame=frame,
            driver=driver,
            calibration=calibration,
            elapsed_s=elapsed_s,
            current_lap=current_lap,
            total_laps=total_laps,
            compound=compound,
            age=age,
            rival=rival,
            rival_pace=rival_pace,
        )
        window = self._optimal_window(options)

        # --- alerts -------------------------------------------------------
        alerts = self._evaluate_alerts(frame, driver, options, tyre, rival)

        # --- the call -----------------------------------------------------
        recommendation, reason, best_pit_lap = self._recommend(
            options,
            current_lap,
            driver_number,
            noise_sigma=calibration.noise_sigma,
        )

        return PitWallReport(
            driver_number=driver_number,
            circuit=frame.circuit,
            year=frame.year,
            lap=current_lap,
            total_laps=total_laps,
            flag_state=frame.flag_state,
            tyre=tyre,
            pace=pace,
            options=options,
            optimal_pit_lap=best_pit_lap,
            optimal_window=window,
            recommendation=recommendation,
            recommendation_reason=reason,
            alerts=alerts,
            messages=messages or [],
            modelled_compounds=self.modelled_compounds,
            compound_tradeoff=self._compound_tradeoff(),
        )

    def _recommend(
        self,
        options: list[StrategyOption],
        current_lap: int,
        driver_number: int,
        noise_sigma: float = 0.5,
    ) -> tuple[str, str, int | None]:
        """The pit-wall call: hold, wait, or stop now.

        The call is made against **staying out**, and it is measured
        against the model's own noise: a stop that is quicker than running
        to the end by less than the noise is not a stop.

        **The call is sticky.**  The model is re-run every second, and two
        candidate stops that are within the noise of each other can swap
        places between one run and the next, which would make the card
        flicker between "box on lap 27" and "box on lap 28" while the race
        has not moved a metre.  So once a pit lap has been called it is kept
        until the lap changes.
        """
        if not options:
            return "HOLD", "No strategy could be projected.", None
        # The caller sorts, but the decision must not depend on it.
        options = sorted(options, key=lambda option: option.expected_total_s)

        stay = next((option for option in options if option.pit_lap is None), None)
        pitting = [option for option in options if option.pit_lap is not None]

        # --- keep the call we made on this lap ---------------------------
        previous = self._calls.get(driver_number)
        same_lap = previous is not None and previous.get("lap") == current_lap
        best_pit = (
            min(pitting, key=lambda option: option.expected_total_s)
            if pitting
            else None
        )
        if same_lap and best_pit is not None:
            chosen = next(
                (
                    option
                    for option in pitting
                    if option.pit_lap == previous.get("pit_lap")
                ),
                None,
            )
            if chosen is not None:
                best_pit = chosen

        if best_pit is None:
            self._calls[driver_number] = {
                "lap": current_lap,
                "call": "HOLD",
                "pit_lap": None,
            }
            return "HOLD", "No further stops are projected.", None

        if stay is None:
            # There is no "stay out" option to compare against, so the best
            # stop is simply the call.
            call = "PIT" if best_pit.pit_lap <= current_lap else "WAIT"
            self._calls[driver_number] = {
                "lap": current_lap,
                "call": call,
                "pit_lap": best_pit.pit_lap,
            }
            return (
                call,
                f"The best stop is on lap {best_pit.pit_lap}.",
                best_pit.pit_lap,
            )

        # What the stop costs, in seconds, against running to the end.  A
        # negative number means the stop is quicker.
        cost = best_pit.expected_total_s - stay.expected_total_s
        if cost > -noise_sigma:
            # The stop does not beat staying out by more than the model's own
            # noise, so it is the same answer and there is nothing in it.
            self._calls[driver_number] = {
                "lap": current_lap,
                "call": "HOLD",
                "pit_lap": best_pit.pit_lap,
            }
            if cost >= 0:
                reason = (
                    f"Staying out is {cost:.1f}s quicker than stopping on "
                    f"lap {best_pit.pit_lap}."
                )
            else:
                reason = (
                    f"Stopping on lap {best_pit.pit_lap} is within "
                    f"{abs(cost):.1f}s of staying out, so there is nothing "
                    "in it."
                )
            return "HOLD", reason, best_pit.pit_lap

        if best_pit.pit_lap <= current_lap:
            self._calls[driver_number] = {
                "lap": current_lap,
                "call": "PIT",
                "pit_lap": best_pit.pit_lap,
            }
            return (
                "PIT",
                f"Pitting now is {abs(cost):.1f}s quicker than staying out.",
                best_pit.pit_lap,
            )
        self._calls[driver_number] = {
            "lap": current_lap,
            "call": "WAIT",
            "pit_lap": best_pit.pit_lap,
        }
        return (
            "WAIT",
            f"The best stop is on lap {best_pit.pit_lap}, "
            f"{abs(cost):.1f}s quicker than staying out.",
            best_pit.pit_lap,
        )

    def _compound_tradeoff(self) -> dict[str, Any]:
        """The measured compound offset, and how far to trust it."""
        return {
            "offset_s_per_lap": {
                compound: round(value, 3)
                for compound, value in (self.bundle.compound_offset or {}).items()
            },
            "agreement": self.bundle.compound_offset_agreement,
            "samples": self.bundle.compound_offset_samples,
            "reliable": self.compound_offset_reliable,
            "note": (
                "Compound choice is tied to strategy and to the fuel load, so "
                "the offset is only weakly separable. It is used to rank a "
                "switch only when the drivers who ran both compounds agree."
            ),
        }

    # ------------------------------------------------------------------
    def _pace_delta(self, frame: Any, driver: Any, rival: Any) -> float | None:
        """Observed pace difference against the rival, in seconds per lap.

        The comparison uses the two cars' real lap times over the laps both
        have completed, so it reflects what actually happened on track.
        """
        ours = self._recent_lap_times(frame, driver.driver_number)
        theirs = self._recent_lap_times(frame, rival.driver_number)
        if not ours or not theirs:
            return None
        common = sorted(set(ours) & set(theirs))
        if not common:
            return None
        diffs = [ours[lap] - theirs[lap] for lap in common[-6:]]
        return float(np.mean(diffs))

    @staticmethod
    def _recent_lap_times(frame: Any, driver_number: int) -> dict[int, float]:
        """Lap times of a driver, taken from the live frame."""
        times: dict[int, float] = {}
        for driver in frame.drivers:
            if driver.driver_number != driver_number:
                continue
            if driver.lap and driver.last_lap_s:
                times[int(driver.lap)] = float(driver.last_lap_s)
        return times

    # ------------------------------------------------------------------
    def _rank_strategies(
        self,
        *,
        frame: Any,
        driver: Any,
        calibration: CalibrationData,
        elapsed_s: float,
        current_lap: int,
        total_laps: int,
        compound: str | None,
        age: int | None,
        rival: Any,
        rival_pace: float | None = None,
    ) -> list[StrategyOption]:
        """Project every sensible strategy and rank them by expected time."""
        laps_remaining = max(0, total_laps - current_lap + 1)
        if laps_remaining <= 0:
            return []

        current_age = age or 0
        current_compound = compound or "MEDIUM"
        modelled = self.modelled_compounds
        if not modelled:
            return []

        # Only project a compound switch when the measured offset is reliable
        # enough to rank one; otherwise the comparison would be between two
        # numbers the data does not support.
        #
        # When it is *not* reliable the strategy is about **when** to stop,
        # not **what** to stop on: the driver is projected to stop and
        # continue on the compound they are on.  Offering another compound
        # here is what made the recommendation flip between softs and
        # mediums from one driver to the next.
        if self.compound_offset_reliable:
            switchable = modelled
        elif current_compound in modelled:
            switchable = [current_compound]
        else:
            # The compound they are on was never fitted, so nothing about a
            # switch can be projected.
            switchable = []

        # The rival's projected remaining time, from their tyre state and pace.
        rival_remaining = self._rival_remaining(
            frame, rival, laps_remaining, calibration, rival_pace
        )

        candidates: list[tuple[str, int | None, str | None, list[RemainingStint], int]] = []

        # Strategy 1: stay out to the end on the current tyre.
        candidates.append(
            (
                "stay",
                None,
                current_compound,
                [RemainingStint(current_compound, current_age, laps_remaining)],
                0,
            )
        )

        # Strategies 2..n: pit after k laps, on a compound the model covers.
        for offset in range(0, min(PIT_LOOKAHEAD, max(0, laps_remaining - 1)) + 1):
            pit_lap = current_lap + offset
            first_laps = offset + 1  # including the lap we pit on
            for new_compound in switchable:
                if first_laps >= laps_remaining:
                    continue
                stints = [
                    RemainingStint(current_compound, current_age, first_laps),
                    RemainingStint(new_compound, 0, laps_remaining - first_laps),
                ]
                candidates.append(
                    (
                        f"pit-{offset}-{new_compound[0]}",
                        pit_lap,
                        new_compound,
                        stints,
                        1,
                    )
                )

        projected: list[StrategyOption] = []
        for index, (strategy_id, pit_lap, new_compound, stints, pit_stops) in enumerate(candidates):
            projection = simulate_remaining(
                stints,
                calibration,
                pit_stops=pit_stops,
                start_lap=current_lap,
                n_sims=self.n_sims,
                seed=self.seed + index,
            )
            total = elapsed_s + projection.expected_s
            p_beat = None
            if rival_remaining is not None:
                # The rival's lead over us, in seconds.  ``gap_to_leader`` is
                # measured against the race leader, so the difference between
                # the two drivers' gaps is the actual margin between them.
                our_gap = getattr(driver, "gap_to_leader", None)
                rival_gap = getattr(rival, "gap_to_leader", None)
                if our_gap is not None and rival_gap is not None:
                    margin = rival_gap - our_gap
                    # We win when our remaining time beats theirs by more than
                    # the margin they currently hold.
                    advantage = rival_remaining - projection.samples
                    p_beat = float(np.mean(advantage > margin))
            projected.append(
                StrategyOption(
                    strategy_id=strategy_id,
                    label=_label(strategy_id, pit_lap, new_compound, current_compound),
                    pit_lap=pit_lap,
                    compound=new_compound,
                    expected_total_s=total,
                    p10_s=elapsed_s + projection.p10_s,
                    p50_s=elapsed_s + projection.p50_s,
                    p90_s=elapsed_s + projection.p90_s,
                    delta_vs_current_s=0.0,
                    p_beat_rival=p_beat,
                )
            )

        if not projected:
            return []
        baseline = projected[0].expected_total_s
        for option in projected:
            option.delta_vs_current_s = option.expected_total_s - baseline
        projected.sort(key=lambda option: option.expected_total_s)
        for option in projected:
            self._project_position(frame, driver, option)
        return projected

    @staticmethod
    def _project_position(frame: Any, driver: Any, option: StrategyOption) -> None:
        """Work out where the driver would end up under this strategy.

        This is the question a fan actually asks: *if I stop now, do I come
        out in front of that car or behind it?*

        The answer is worked out from the **real gaps**.  The strategy's
        time difference is applied to the gap the driver has to the leader,
        the whole field is then sorted on the gaps, and the driver's place
        in that order - and the cars either side of them - follow from it.

        A driver whose timing feed has stopped is left out: they are not in
        the race any more, so they cannot be overtaken or overtake.
        """
        our_gap = getattr(driver, "gap_to_leader", None)
        if our_gap is None:
            return

        new_gap = our_gap + option.delta_vs_current_s
        entries: list[tuple[float, str | None]] = []
        for other in frame.drivers:
            if other.driver_number == driver.driver_number:
                continue
            if not getattr(other, "reporting", True):
                continue
            their_gap = getattr(other, "gap_to_leader", None)
            if their_gap is None:
                continue
            entries.append((float(their_gap), getattr(other, "code", None)))
        entries.append((new_gap, None))
        entries.sort(key=lambda entry: entry[0])

        our_index = next(
            index for index, entry in enumerate(entries) if entry[1] is None
        )
        option.position_after = our_index + 1
        option.ahead_of = entries[our_index - 1][1] if our_index > 0 else None
        option.behind_of = (
            entries[our_index + 1][1] if our_index < len(entries) - 1 else None
        )

    def _rival_remaining(
        self,
        frame: Any,
        rival: Any,
        laps_remaining: int,
        calibration: CalibrationData,
        rival_pace: float | None = None,
    ) -> float | None:
        """The rival's projected remaining race time.

        The rival is projected with **their own** measured pace and tyre
        state.  Using our pace for them would hide the very pace difference
        the pit wall needs to see.
        """
        if rival is None:
            return None
        compound = getattr(rival, "compound", None)
        age = getattr(rival, "tyre_age", None)
        if not compound or age is None:
            return None

        rival_calibration = self._calibration(rival_pace, compound)
        projection = simulate_remaining(
            [RemainingStint(compound, age, laps_remaining)],
            rival_calibration,
            pit_stops=0,
            start_lap=1,
            n_sims=400,
            seed=self.seed,
        )
        return projection.expected_s

    @staticmethod
    def _optimal_window(options: list[StrategyOption]) -> tuple[int, int] | None:
        """The lap range around the best pit stop."""
        pitting = [option for option in options if option.pit_lap is not None]
        if not pitting:
            return None
        best = min(pitting, key=lambda option: option.expected_total_s)
        within = [
            option.pit_lap
            for option in pitting
            if option.expected_total_s <= best.expected_total_s + 2.0
        ]
        return (min(within), max(within))

    # ------------------------------------------------------------------
    def _evaluate_alerts(
        self,
        frame: Any,
        driver: Any,
        options: list[StrategyOption],
        tyre: TyreState,
        rival: Any,
    ) -> list[dict[str, Any]]:
        """Evaluate the trigger thresholds for one driver."""
        events: list[TriggerEvent] = []

        window = self._optimal_window(options)
        if window and window[0] <= (driver.lap or 0) <= window[1]:
            event = self.triggers.check_and_fire(
                lap=driver.lap or 0,
                driver_number=driver.driver_number,
                alert_type="PIT_WINDOW_OPEN",
                payload={
                    "optimal_window": list(window),
                    "optimal_lap": window[0],
                },
            )
            if event:
                events.append(event)

        if (
            tyre.cliff_laps_away is not None
            and 0 <= tyre.cliff_laps_away <= 3
        ):
            event = self.triggers.check_and_fire(
                lap=driver.lap or 0,
                driver_number=driver.driver_number,
                alert_type="CLIFF_APPROACHING",
                payload={
                    "tyre_age": tyre.age_laps,
                    "cliff_lap": tyre.projected_cliff_lap,
                },
            )
            if event:
                events.append(event)

        gap = getattr(rival, "gap_to_leader", None) if rival is not None else None
        if gap is not None and rival is not None:
            # The car ahead of us is the one we want to undercut: our gap to
            # the leader is larger than theirs.
            rival_gap = getattr(rival, "gap_to_leader", None)
            our_gap = getattr(driver, "gap_to_leader", None)
            if our_gap is not None and rival_gap is not None:
                gap_ahead = our_gap - rival_gap
                if gap_ahead > 0:
                    event = self.triggers.check_and_fire(
                        lap=driver.lap or 0,
                        driver_number=driver.driver_number,
                        alert_type="UNDERCUT_THREAT",
                        payload={"gap_ahead_s": gap_ahead},
                    )
                    if event:
                        events.append(event)

        return [
            {
                "type": event.alert_type,
                "lap": event.lap,
                "driver_number": event.driver_number,
                "payload": event.payload,
            }
            for event in events
        ]


COMPOUND_WORD = {
    "SOFT": "Softs",
    "MEDIUM": "Mediums",
    "HARD": "Hards",
    "INTERMEDIATE": "Intermediates",
    "WET": "Wets",
}


def _label(
    strategy_id: str, pit_lap: int | None, compound: str | None, current: str
) -> str:
    """The plain words for a strategy.

    ``current`` is the compound the driver is on now.  When the strategy
    keeps them on it, the label says so, so a card that reads "Box on lap 26
    and stay on the mediums" is not mistaken for a recommendation to change
    to mediums.
    """
    if pit_lap is None:
        return f"Stay on {COMPOUND_WORD.get(current.upper(), current)}"
    if compound and compound.upper() == current.upper():
        word = COMPOUND_WORD.get(compound.upper(), compound)
        return f"Box on lap {pit_lap}, stay on {word}"
    word = COMPOUND_WORD.get((compound or "").upper(), compound or "unknown tyres")
    return f"Box on lap {pit_lap} onto {word}"
