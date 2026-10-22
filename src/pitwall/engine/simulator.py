from dataclasses import dataclass

import numpy as np

from pitwall.data.models import StrategyResult


@dataclass(frozen=True)
class StintSpec:
    compound: str
    start_lap: int
    end_lap: int
    pit_lap: int


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    driver_number: int
    stints: tuple[StintSpec, ...]


@dataclass(frozen=True)
class CalibrationData:
    """Physical constants used by the simulator.

    ``degradation`` is a callable so that per-circuit, per-year calibration
    bundles (see :mod:`pitwall.engine.calibration`) can supply real fitted
    values instead of hard-coded constants.
    """

    base_pace: float
    pit_loss: float
    noise_sigma: float = 0.5
    fuel_burn_rate_kg_per_lap: float = 1.5
    fuel_effect_s_per_kg: float = 0.03
    degradation_model: object | None = None
    #: Pace offset per compound in seconds, relative to the base pace.
    compound_offset: dict[str, float] | None = None

    def compound_pace(self, compound: str) -> float:
        """The base pace for a compound, including its fitted offset."""
        if not self.compound_offset:
            return self.base_pace
        return self.base_pace + float(self.compound_offset.get(str(compound).upper(), 0.0))

    def degradation(self, compound: str, tyre_age: int) -> float:
        """Seconds lost per lap to tyre wear at the given age."""
        age = float(tyre_age)
        if self.degradation_model is not None:
            return float(self.degradation_model(compound, age))
        # Fallback two-regime model.  Used only when no fitted bundle exists.
        b1, b2, t_cliff = (
            (0.03, 0.01, 20.0) if compound.upper() == "SOFT" else (0.02, 0.005, 25.0)
        )
        return float(b1 * age + b2 * max(0.0, age - t_cliff) ** 2)

    def fuel_effect(self, lap_number: int) -> float:
        """Seconds lost per lap due to the fuel still in the car."""
        fuel_mass = max(0.0, 100.0 - (lap_number - 1) * self.fuel_burn_rate_kg_per_lap)
        return float(fuel_mass * self.fuel_effect_s_per_kg)


def simulate_strategy(
    strategy: StrategySpec,
    calib: CalibrationData,
    total_laps: int,
    n_sims: int = 1000,
    seed: int | None = None,
    rival_spec: StrategySpec | None = None,
) -> StrategyResult:
    """Monte-Carlo a strategy and return its projected race time.

    ``rival_spec`` lets the rival be simulated in the same draw, which is what
    makes ``p_beat_rival`` meaningful instead of a placeholder.
    """
    rng = np.random.default_rng(seed)
    totals = np.empty(n_sims)
    rival_totals = np.empty(n_sims) if rival_spec is not None else None

    for i in range(n_sims):
        t = 0.0
        current_fuel_lap = 1
        for stint in strategy.stints:
            for lap in range(stint.start_lap, stint.end_lap + 1):
                tyre_age = lap - stint.start_lap + 1
                t += calib.compound_pace(stint.compound)
                t += calib.fuel_effect(current_fuel_lap)
                t += calib.degradation(stint.compound, tyre_age)
                t += rng.normal(0.0, calib.noise_sigma)

                # A pit stop costs time only when the driver actually pits in
                # and then continues racing: the final stint never ends with a
                # stop.  Comparing against ``total_laps`` alone double-counted
                # the stop of the last stint whenever the strategy finished
                # before the end of the race.
                is_last_stint = stint is strategy.stints[-1]
                if lap == stint.pit_lap and not is_last_stint:
                    t += calib.pit_loss
                current_fuel_lap += 1
        totals[i] = t

        if rival_totals is not None:
            r = 0.0
            rival_fuel_lap = 1
            for stint in rival_spec.stints:
                for lap in range(stint.start_lap, stint.end_lap + 1):
                    tyre_age = lap - stint.start_lap + 1
                    r += calib.compound_pace(stint.compound)
                    r += calib.fuel_effect(rival_fuel_lap)
                    r += calib.degradation(stint.compound, tyre_age)
                    r += rng.normal(0.0, calib.noise_sigma)
                    is_last_stint = stint is rival_spec.stints[-1]
                    if lap == stint.pit_lap and not is_last_stint:
                        r += calib.pit_loss
                    rival_fuel_lap += 1
            rival_totals[i] = r

    if rival_totals is not None:
        p_beat_rival = float(np.mean(totals < rival_totals))
    else:
        p_beat_rival = 0.5

    return StrategyResult(
        driver_number=strategy.driver_number,
        strategy_id=strategy.strategy_id,
        expected_total_s=float(totals.mean()),
        p10_s=float(np.percentile(totals, 10)),
        p50_s=float(np.percentile(totals, 50)),
        p90_s=float(np.percentile(totals, 90)),
        p_beat_rival=p_beat_rival,
        n_sims=n_sims,
    )


@dataclass(frozen=True)
class RemainingStint:
    """A stint still to be run, starting from a known tyre age."""

    compound: str
    start_age: int
    laps: int


@dataclass(frozen=True)
class RemainingProjection:
    """Distribution of the time still to be run."""

    expected_s: float
    p10_s: float
    p50_s: float
    p90_s: float
    samples: np.ndarray
    n_sims: int


def simulate_remaining(
    stints: list[RemainingStint],
    calib: CalibrationData,
    *,
    pit_stops: int = 0,
    pit_loss: float | None = None,
    start_lap: int = 1,
    n_sims: int = 2000,
    seed: int | None = None,
) -> RemainingProjection:
    """Simulate the laps still to be run.

    This is the pit-wall workhorse: it projects the *remaining* race from the
    tyre state the car is in right now, so a strategy comparison is always
    anchored on real, already-run laps rather than a hypothetical race.

    The simulation is vectorised over the Monte-Carlo draws, so a strategy
    search stays fast enough to run on every tick.
    """
    rng = np.random.default_rng(seed)
    loss = calib.pit_loss if pit_loss is None else pit_loss

    total_laps = sum(stint.laps for stint in stints)
    if total_laps <= 0:
        zeros = np.zeros(n_sims)
        return RemainingProjection(0.0, 0.0, 0.0, 0.0, zeros, n_sims)

    totals = np.zeros(n_sims)
    lap_number = start_lap
    for stint in stints:
        ages = np.arange(stint.start_age, stint.start_age + stint.laps, dtype=float)
        pace = calib.compound_pace(stint.compound)
        degradation = np.array(
            [calib.degradation(stint.compound, age) for age in ages], dtype=float
        )
        # The fuel burns off lap by lap, so the effect shrinks as the race
        # goes on rather than staying at the opening-lap value.
        fuel = np.array(
            [calib.fuel_effect(lap_number + offset) for offset in range(stint.laps)],
            dtype=float,
        )
        totals += pace * stint.laps + degradation.sum() + fuel.sum()
        lap_number += stint.laps

    # The lap-to-lap noise accumulates as the square root of the lap count.
    totals += rng.normal(0.0, calib.noise_sigma * np.sqrt(total_laps), n_sims)

    # A stop is paid at the end of every stint except the last.
    totals += loss * max(0, min(pit_stops, len(stints) - 1))

    return RemainingProjection(
        expected_s=float(totals.mean()),
        p10_s=float(np.percentile(totals, 10)),
        p50_s=float(np.percentile(totals, 50)),
        p90_s=float(np.percentile(totals, 90)),
        samples=totals,
        n_sims=n_sims,
    )
