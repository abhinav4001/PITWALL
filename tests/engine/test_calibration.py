from pathlib import Path

import pytest

from pitwall.engine.calibration import (
    CircuitCalibration,
    calibration_exists,
    load_calibration,
    save_calibration,
)


def _bundle(circuit: str = "silverstone", year: int = 2026) -> CircuitCalibration:
    return CircuitCalibration(
        circuit=circuit,
        year=year,
        pit_loss_s=24.1,
        base_pace_s=92.5,
        fuel_burn_rate_kg_per_lap=1.45,
        fuel_effect_s_per_kg=0.032,
        sc_probability=0.4,
        tyre_cliff_soft=16.0,
        tyre_cliff_medium=26.0,
        tyre_cliff_hard=38.0,
        degradation={"MEDIUM": [0.04, 0.001, 24.0]},
        source="unit test fixture",
    )


def test_calibration_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pitwall.engine.calibration.get_calibration_dir", lambda: tmp_path)

    calib = _bundle()
    path = save_calibration(calib)
    assert path.exists()
    assert calibration_exists("silverstone", 2026)

    loaded = load_calibration("silverstone", 2026)
    assert loaded.circuit == "silverstone"
    assert loaded.year == 2026
    assert loaded.pit_loss_s == 24.1
    assert loaded.tyre_cliff_soft == 16.0
    assert loaded.base_pace_s == 92.5
    assert loaded.degradation["MEDIUM"] == [0.04, 0.001, 24.0]


def test_missing_calibration_raises_instead_of_returning_fake_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncalibrated circuit must not look like a fitted one."""
    monkeypatch.setattr("pitwall.engine.calibration.get_calibration_dir", lambda: tmp_path)

    assert not calibration_exists("monza", 2026)
    with pytest.raises(FileNotFoundError):
        load_calibration("monza", 2026)
