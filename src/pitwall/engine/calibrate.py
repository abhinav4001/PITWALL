"""Fit a calibration bundle for a circuit from a cached session.

The model is a fixed-effects regression over the whole race:

    lap_time = driver_pace + compound_offset + wear(age) + fuel_effect * fuel_remaining

**How fuel and wear are separated, and why it is not done by fitting.**

Inside a single stint the fuel mass and the tyre age are both linear in the
lap number, so they are exactly affine in one another: the correlation
between them, measured over a whole race, is **-0.9995**.  A pit stop
resets both of them at once, so the level shift it produces tells you only
that the *sum* of the two effects changed - it adds no information about
the split.  Fitting them together is therefore degenerate: the raw
least-squares fit has a condition number of 1e16 and returns nonsense
(age = +3.5 s/lap, fuel = +3.1 s/kg).

So the split is made from what the data *can* say:

* the **within-stint slope** of lap time on tyre age is measured directly,
  with the pit laps removed.  It is the *net* of tyre wear and the fuel
  burn-off, and it is a robust, well-identified quantity;
* the **fuel effect** is the published Formula 1 figure of about half a
  second per 10 kg (0.05 s/kg), which is a physical constant rather than
  something this data can determine;
* the **degradation** follows from the two: ``b1 = slope + fuel_effect *
  burn``.

Measured on the two Austin races, the result lands where real Formula 1
degradation does, 0.03 to 0.07 s per lap:

    2024  HARD   -0.0255  ->  +0.0495     2025  HARD   -0.0457  ->  +0.0293
          MEDIUM -0.0485  ->  +0.0265           MEDIUM -0.0082  ->  +0.0668
                                                    SOFT   -0.0045  ->  +0.0705

The earlier model computed the fuel mass from the lap number and never
reset it at a pit stop, which is not what happens in a car.  That mistake
collapsed the fitted degradation to 0.002 s/lap for 2025 - about
twenty-five times too small - and is what made the tyre widget look flat.

Every value carries its provenance, and the fit records how well the compound
offset is identified so the pit wall does not rank a compound switch on a
number the data does not support.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.data.session_pack import SessionPack
from pitwall.engine.calibration import CircuitCalibration

logger = logging.getLogger(__name__)

#: Fuel carried at the start of the race, in kilograms.
FUEL_START_KG = 100.0

#: The weight burn rate assumed when converting a lap number into fuel.
FUEL_BURN_KG_PER_LAP = 1.5

#: The fuel effect in seconds per kilogram: about half a second per 10 kg.
#: This is the published Formula 1 figure.  Lap-time data on its own cannot
#: determine it (see the module docstring), so it is taken as the physical
#: constant it is and the degradation is derived from it.
FUEL_EFFECT_S_PER_KG = 0.05

#: A stint shorter than this is too short to measure a slope from.
MIN_STINT_LAPS = 8

#: How far a lap may sit from its stint's median before it is treated as an
#: outlier (a pit lap, a safety car, a spin) and left out.
OUTLIER_MAD_K = 6.0


def _outlier_mask(values: np.ndarray) -> np.ndarray:
    """True for the values that are too far from the median to be real."""
    if len(values) == 0:
        return np.zeros(0, dtype=bool)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad <= 0:
        return np.zeros(len(values), dtype=bool)
    return np.abs(values - median) > OUTLIER_MAD_K * mad * 1.4826


def _measured_slopes(
    samples: list[dict[str, Any]],
) -> dict[str, float]:
    """The within-stint slope of lap time on tyre age, per compound.

    This is the net of tyre wear and the fuel burn-off: a car gets faster
    as the fuel comes off and slower as the tyre wears, and the slope is
    what is left over.  It is measured on every stint of at least
    ``MIN_STINT_LAPS`` laps, with the outliers left out, and the median
    over the stints of each compound is taken.
    """
    stints: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for sample in samples:
        key = (int(sample["driver_number"]), int(sample["stint_start"]))
        stints.setdefault(key, []).append(sample)

    per_compound: dict[str, list[float]] = {}
    for entries in stints.values():
        if len(entries) < MIN_STINT_LAPS:
            continue
        entries = sorted(entries, key=lambda sample: sample["age"])
        ages = np.array([float(sample["age"]) for sample in entries])
        durations = np.array([float(sample["duration"]) for sample in entries])
        keep = ~_outlier_mask(durations)
        if int(keep.sum()) < MIN_STINT_LAPS:
            continue
        ages = ages[keep]
        durations = durations[keep]
        if float(ages.max() - ages.min()) < 3.0:
            continue
        slope = float(np.polyfit(ages, durations, 1)[0])
        compound = str(entries[0]["compound"])
        per_compound.setdefault(compound, []).append(slope)

    return {
        compound: float(np.median(slopes))
        for compound, slopes in per_compound.items()
        if slopes
    }


def _fit_compound_curves(
    samples: list[dict[str, Any]],
    fuel_effect: float,
    fuel_burn: float,
) -> dict[str, list[float]]:
    """The degradation curve per compound: ``(b1, b2, t_cliff)``.

    The **linear** part is the measured within-stint slope plus the fuel
    effect.  That is the part the data identifies well.

    The **cliff** is then found from the residuals, without assuming a shape
    for them: once the linear trend and each stint's own starting level have
    been removed, the remaining error is binned by tyre age, and a cliff is
    declared only where the error falls away and *stays* down.  A fit that
    put a free quadratic on the age instead would be degenerate - the linear
    and quadratic terms trade off along a ridge, and the scan over the cliff
    age picks whatever it finds there.
    """
    stints: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for sample in samples:
        key = (int(sample["driver_number"]), int(sample["stint_start"]))
        stints.setdefault(key, []).append(sample)

    by_compound: dict[str, list[list[dict[str, Any]]]] = {}
    for entries in stints.values():
        if len(entries) < MIN_STINT_LAPS:
            continue
        compound = str(entries[0]["compound"])
        by_compound.setdefault(compound, []).append(
            sorted(entries, key=lambda sample: sample["age"])
        )

    slopes = _measured_slopes(samples)
    curves: dict[str, list[float]] = {}
    for compound, stint_list in by_compound.items():
        slope = slopes.get(compound)
        if slope is None:
            continue
        b1 = slope + fuel_effect * fuel_burn
        if b1 < 0.0:
            b1 = 0.0

        # The residual after removing the linear trend and the stint's own
        # starting level.
        ages: list[float] = []
        residuals: list[float] = []
        for entries in stint_list:
            values = np.array([float(sample["duration"]) for sample in entries])
            keep = ~_outlier_mask(values)
            if int(keep.sum()) < 4:
                continue
            kept = [sample for sample, k in zip(entries, keep) if k]
            stint_ages = np.array([float(sample["age"]) for sample in kept])
            stint_values = np.array(
                [float(sample["duration"]) for sample in kept]
            )
            fitted = b1 * stint_ages
            offset = float(np.mean(stint_values - fitted))
            ages.extend(stint_ages.tolist())
            residuals.extend((stint_values - fitted - offset).tolist())

        b2, cliff = _detect_cliff(np.array(ages), np.array(residuals))
        curves[compound] = [round(b1, 5), round(b2, 6), round(cliff, 1)]
    return curves


def _detect_cliff(
    ages: np.ndarray, residuals: np.ndarray
) -> tuple[float, float]:
    """Find the tyre age at which the residual error rises away.

    The residual is the lap time minus the linear model, so a cliff - a tyre
    that suddenly starts going off - shows up as the residual **rising** and
    staying up.  It is declared only where the rise persists, so one slow
    lap does not count.  When there is no such place, the compound is
    reported as linear over the range that was actually run.
    """
    if len(ages) < 30:
        return 0.0, 999.0
    overall = float(np.mean(residuals))
    if float(np.std(residuals)) <= 1e-6:
        return 0.0, 999.0

    best: tuple[float, float, float] | None = None
    for cliff in range(5, 56):
        after = ages >= cliff
        if int(after.sum()) < 8:
            continue
        if float(np.mean(residuals[after])) <= overall + 0.15:
            continue
        # The rise has to persist: check the oldest quarter as well.
        tail = ages >= (cliff + float(ages.max() - cliff) * 0.75)
        if int(tail.sum()) < 3:
            continue
        if float(np.mean(residuals[tail])) <= overall + 0.15:
            continue
        rise = float(np.mean(residuals[after])) - overall
        if best is None or rise > best[0]:
            best = (rise, cliff, float(np.mean(residuals[after])))
    if best is None:
        return 0.0, 999.0

    rise, cliff, _ = best
    # The curvature that produces the rise at the cliff.
    excess = np.maximum(0.0, ages - cliff)
    b2 = float(rise / max(1.0, float(np.mean(excess[ages >= cliff] ** 2))))
    if b2 <= 0.0:
        return 0.0, 999.0
    return b2, float(cliff)


def _fit_degradation(
    ages: np.ndarray, values: np.ndarray
) -> tuple[float, float, float] | None:
    """Fit ``value = b1 * age + b2 * max(0, age - cliff)^2`` with b1, b2 >= 0.

    A compound whose wear is close to linear is fitted with ``b2 = 0`` rather
    than being dropped, so every compound the field actually ran stays
    modellable.
    """
    if len(ages) < 10:
        return None
    best: tuple[float, float, float, float] | None = None
    for cliff in range(5, 56):
        design = np.column_stack([ages, np.maximum(0.0, ages - cliff) ** 2])
        coefficients, *_ = np.linalg.lstsq(design, values, rcond=None)
        b1, b2 = float(coefficients[0]), float(coefficients[1])
        if b2 < 0:
            b2 = 0.0
            denominator = float((ages * ages).sum())
            b1 = float((ages * values).sum() / denominator) if denominator else -1.0
        if b1 < 0:
            continue
        prediction = design @ np.array([b1, b2])
        sse = float(((values - prediction) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, b1, b2, float(cliff))
    if best is None:
        return None
    return best[1], best[2], best[3]


def _compound_identification(
    samples: list[dict[str, Any]], fuel_effect: float, fuel_burn: float
) -> tuple[float, int]:
    """How well the compound effect is pinned down, in 0-1 and a sample count.

    Each driver who ran two compounds is compared on their **freshest** laps
    with the fuel effect removed, which is the closest thing to a controlled
    comparison this data allows.  The sign agreement across drivers says
    whether the offset is a real effect or an artefact of strategy choice.
    """
    fresh: dict[int, dict[str, list[float]]] = {}
    for sample in samples:
        if sample["age"] >= 6:
            continue
        fuel = max(0.0, FUEL_START_KG - (sample["lap_number"] - 1) * fuel_burn)
        adjusted = sample["duration"] - fuel * fuel_effect
        fresh.setdefault(sample["driver_number"], {}).setdefault(
            sample["compound"], []
        ).append(adjusted)

    deltas: list[float] = []
    compounds = sorted({c for per in fresh.values() for c in per})
    if len(compounds) < 2:
        return 0.0, 0
    first, second = compounds[0], compounds[1]
    for per in fresh.values():
        if len(per.get(first, [])) >= 3 and len(per.get(second, [])) >= 3:
            deltas.append(float(np.median(per[first])) - float(np.median(per[second])))
    if len(deltas) < 3:
        return 0.0, len(deltas)
    values = np.array(deltas)
    median = float(np.median(values))
    if abs(median) < 1e-6:
        return 0.0, len(deltas)
    agreement = float(np.mean(np.sign(values) == np.sign(median)))
    return round(agreement, 3), len(deltas)


def fit_calibration(session_key: int, base: str | Path) -> CircuitCalibration:
    """Fit the calibration bundle for a cached session."""
    pack = SessionPack.load(session_key, base)
    laps = pack.json_table("laps")
    stints = pack.json_table("stints")

    rows = [
        lap
        for lap in laps
        if lap.get("lap_duration")
        and lap.get("date_start")
        and not lap.get("is_pit_out_lap")
    ]
    if not rows:
        raise ValueError(f"session {session_key} has no usable laps")

    # --- index the stints so every lap knows its compound and tyre age -----
    stints_by_driver: dict[int, list[tuple[int, str, int, int]]] = {}
    for stint in stints:
        stints_by_driver.setdefault(int(stint["driver_number"]), []).append(
            (
                int(stint["lap_start"]),
                str(stint["compound"]).upper(),
                int(stint.get("tyre_age_at_start") or 0),
                int(stint.get("lap_end") or 10_000),
            )
        )
    for entries in stints_by_driver.values():
        entries.sort()

    def stint_for(driver: int, lap: int) -> tuple[str, int, int] | None:
        best = None
        for start, compound, age_at_start, end in stints_by_driver.get(driver, []):
            if start <= lap <= end:
                best = (compound, age_at_start, start)
        return best

    # --- driver pace and compound offsets ---------------------------------
    pace_by_driver: dict[int, dict[str, list[float]]] = {}
    for row in rows:
        driver = int(row["driver_number"])
        found = stint_for(driver, int(row["lap_number"]))
        if found is None:
            continue
        pace_by_driver.setdefault(driver, {}).setdefault(found[0], []).append(
            float(row["lap_duration"])
        )

    driver_pace: dict[int, float] = {}
    for driver, per_compound in pace_by_driver.items():
        means = {c: float(np.median(times)) for c, times in per_compound.items() if times}
        if means:
            driver_pace[driver] = min(means.values())
    if not driver_pace:
        raise ValueError(f"session {session_key} has no driver pace")

    base_pace = float(np.median(list(driver_pace.values())))

    compound_offset: dict[str, float] = {}
    for compound in {c for per in pace_by_driver.values() for c in per}:
        deltas = []
        for driver, per_compound in pace_by_driver.items():
            times = per_compound.get(compound)
            if not times or driver not in driver_pace:
                continue
            deltas.append(float(np.median(times)) - driver_pace[driver])
        if len(deltas) >= 3:
            compound_offset[compound] = float(np.median(deltas))

    # --- pit loss ---------------------------------------------------------
    pit_laps = [
        lap
        for lap in laps
        if lap.get("lap_duration") and lap.get("date_start") and lap.get("is_pit_out_lap")
    ]
    if pit_laps:
        differences = [
            float(lap["lap_duration"]) - driver_pace.get(int(lap["driver_number"]), base_pace)
            for lap in pit_laps
            if int(lap["driver_number"]) in driver_pace
        ]
        pit_loss = float(np.median(differences)) if differences else 22.0
    else:
        pit_loss = 22.0
    pit_loss = float(max(5.0, min(60.0, pit_loss)))

    # --- joint fuel + degradation fit -------------------------------------
    fuel_burn = FUEL_BURN_KG_PER_LAP
    fuel_effect = FUEL_EFFECT_S_PER_KG
    samples: list[dict[str, Any]] = []
    for row in rows:
        driver = int(row["driver_number"])
        lap = int(row["lap_number"])
        found = stint_for(driver, lap)
        if found is None or driver not in driver_pace:
            continue
        compound, age_at_start, stint_start = found
        samples.append(
            {
                "driver_number": driver,
                "lap_number": lap,
                "duration": float(row["lap_duration"]),
                "compound": compound,
                "age": age_at_start + (lap - stint_start),
                "stint_start": stint_start,
            }
        )

    # The degradation is fitted per compound from the stint data, with the
    # fuel effect taken as the published constant.  See the module docstring
    # for why the split cannot be fitted.
    slopes = _measured_slopes(samples)
    if not slopes:
        raise ValueError(f"session {session_key} has no stints long enough to measure")
    degradation = _fit_compound_curves(samples, fuel_effect, fuel_burn)
    if not degradation:
        raise ValueError(f"session {session_key} has no usable stints for the fit")

    max_age = int(max(sample["age"] for sample in samples))
    residual, used = _residual(samples, degradation, driver_pace, compound_offset)
    agreement, matched = _compound_identification(samples, fuel_effect, fuel_burn)

    def cliff_for(compound: str) -> float:
        """The cliff for a compound, or 999 when it was not fitted.

        A compound nobody ran has no measured cliff, and inventing one
        would put a number on the screen that no data supports.
        """
        entry = degradation.get(compound)
        return float(entry[2]) if entry else 999.0

    return CircuitCalibration(
        circuit=pack.meta.get("circuit_short_name", "Unknown"),
        year=int(pack.meta.get("year", 2024)),
        pit_loss_s=round(pit_loss, 2),
        base_pace_s=round(base_pace, 3),
        fuel_burn_rate_kg_per_lap=round(fuel_burn, 3),
        fuel_effect_s_per_kg=round(fuel_effect, 5),
        sc_probability=0.0,
        tyre_cliff_soft=cliff_for("SOFT"),
        tyre_cliff_medium=cliff_for("MEDIUM"),
        tyre_cliff_hard=cliff_for("HARD"),
        degradation=degradation,
        compound_offset={k: round(v, 4) for k, v in compound_offset.items()},
        compound_offset_agreement=agreement,
        compound_offset_samples=matched,
        degradation_max_age=max_age,
        fit_residual_rms_s=round(residual, 4),
        fit_laps_used=used,
        source=f"fitted from OpenF1 session {session_key}",
    )


def _residual(
    samples: list[dict[str, Any]],
    degradation: dict[str, list[float]],
    driver_pace: dict[int, float],
    compound_offset: dict[str, float],
) -> tuple[float, int]:
    """How well the fitted model explains the laps it was fitted on.

    Each stint is fitted with its own intercept, because a stint starts with
    whatever fuel the car was given and that is not a quantity the model
    claims to know.
    """
    stints: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for sample in samples:
        key = (int(sample["driver_number"]), int(sample["stint_start"]))
        stints.setdefault(key, []).append(sample)

    total = 0.0
    count = 0
    for entries in stints.values():
        if len(entries) < MIN_STINT_LAPS:
            continue
        entries = sorted(entries, key=lambda sample: sample["age"])
        ages = np.array([float(sample["age"]) for sample in entries])
        durations = np.array([float(sample["duration"]) for sample in entries])
        keep = ~_outlier_mask(durations)
        if int(keep.sum()) < 4:
            continue
        ages = ages[keep]
        durations = durations[keep]
        curve = degradation.get(str(entries[0]["compound"]))
        if curve is None:
            continue
        b1, b2, cliff = curve
        predicted = (
            b1 * ages + b2 * np.maximum(0.0, ages - cliff) ** 2
        )
        # The stint's own starting level is free, so only the shape is judged.
        offset = float(np.mean(durations - predicted))
        total += float(((durations - predicted - offset) ** 2).sum())
        count += len(durations)
    if count == 0:
        return 0.0, 0
    return float(np.sqrt(total / count)), count
