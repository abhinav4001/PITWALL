import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class CircuitCalibration(BaseModel):
    """Fitted physical constants for one circuit and season.

    The bounds are physically sensible ranges, not fitted values: a real fit
    may legitimately land outside a narrow guess, and clamping it would hide
    the fit rather than report it.
    """

    model_config = ConfigDict(frozen=True)

    circuit: str
    year: int = Field(ge=2023, le=2026)
    pit_loss_s: float = Field(ge=5.0, le=60.0)
    fuel_burn_rate_kg_per_lap: float = Field(ge=0.1, le=4.0)
    fuel_effect_s_per_kg: float = Field(ge=0.001, le=0.2)
    sc_probability: float = Field(ge=0.0, le=1.0)
    tyre_cliff_soft: float = Field(ge=3.0, le=999.0)
    tyre_cliff_medium: float = Field(ge=3.0, le=999.0)
    tyre_cliff_hard: float = Field(ge=3.0, le=999.0)
    #: Representative clean-lap pace for this circuit, in seconds.
    base_pace_s: float = Field(ge=30.0, le=240.0)
    #: Fitted degradation coefficients per compound: ``(b1, b2, t_cliff)``.
    degradation: dict[str, list[float]] = Field(default_factory=dict)
    #: Fitted pace offset per compound in seconds, relative to a driver's own
    #: best compound.  Used to model a compound the driver has not run yet.
    compound_offset: dict[str, float] = Field(default_factory=dict)
    #: How consistently the drivers who ran two compounds agreed on that
    #: offset, from 0 (no agreement) to 1 (unanimous).  Below 0.8 the offset
    #: is not treated as reliable and compound switching is not projected.
    compound_offset_agreement: float = Field(default=0.0, ge=0.0, le=1.0)
    #: How many drivers contributed to that agreement check.
    compound_offset_samples: int = Field(default=0, ge=0)
    #: The highest tyre age the degradation curve was fitted over.  Beyond it
    #: the model is linearised rather than extrapolated.
    degradation_max_age: int = Field(default=30, ge=1, le=100)
    #: RMS residual of the fit, in seconds per lap.
    fit_residual_rms_s: float = Field(default=0.0, ge=0.0, le=10.0)
    #: How many real laps the fit used.
    fit_laps_used: int = Field(default=0, ge=0)
    #: Provenance of the numbers, so the UI can show where they came from.
    source: str = "uncalibrated"


def get_calibration_dir() -> Path:
    calib_dir = Path("calibration")
    calib_dir.mkdir(parents=True, exist_ok=True)
    return calib_dir


def save_calibration(calib: CircuitCalibration) -> Path:
    c_dir = get_calibration_dir() / calib.circuit.lower()
    c_dir.mkdir(parents=True, exist_ok=True)
    file_path = c_dir / f"{calib.year}.json"
    file_path.write_text(calib.model_dump_json(indent=2), encoding="utf-8")
    return file_path


def calibration_exists(circuit: str, year: int) -> bool:
    return (get_calibration_dir() / circuit.lower() / f"{year}.json").exists()


def load_calibration(circuit: str, year: int) -> CircuitCalibration:
    """Load a calibration bundle.

    Raises :class:`FileNotFoundError` when no bundle exists.  Returning a
    default bundle here would make an uncalibrated circuit indistinguishable
    from a fitted one, which is exactly the failure mode the project forbids.
    """
    file_path = get_calibration_dir() / circuit.lower() / f"{year}.json"
    if not file_path.exists():
        raise FileNotFoundError(
            f"No calibration bundle for {circuit} {year} at {file_path}. "
            "Run the calibration step before relying on simulation output."
        )
    data = json.loads(file_path.read_text(encoding="utf-8"))
    return CircuitCalibration(**data)
