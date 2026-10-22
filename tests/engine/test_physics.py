from pathlib import Path

import numpy as np
import pandas as pd

from pitwall.engine.degradation import fit_degradation
from pitwall.engine.pit_loss import compute_empirical_pit_loss


def test_compute_empirical_pit_loss_fallback(tmp_path: Path) -> None:
    loss = compute_empirical_pit_loss(parquet_dir=tmp_path)
    assert loss == 22.0


def test_compute_empirical_pit_loss_from_data(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    parquet_dir.mkdir()

    df = pd.DataFrame({
        "lap_time_s": [90.0, 90.5, 112.0, 90.2],
        "is_pit_in": [False, False, True, False],
        "IsAccurate": [True, True, False, True],
    })
    df.to_parquet(parquet_dir / "laps_test.parquet", index=False)

    loss = compute_empirical_pit_loss(parquet_dir=parquet_dir)
    assert 20.0 <= loss <= 25.0


def test_fit_degradation_synthetic() -> None:
    t = np.linspace(1, 35, 35)
    res = 0.03 * t + 0.01 * np.maximum(0, t - 20) ** 2
    b1, b2, t_cliff = fit_degradation(t, res)

    assert 0.01 <= b1 <= 0.05
    assert 0.001 <= b2 <= 0.05
    assert 15.0 <= t_cliff <= 30.0
