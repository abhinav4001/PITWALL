import pandas as pd
import pytest

from pitwall.eval.harness import truncate_at_data_layer


def test_truncate_at_data_layer_success() -> None:
    df = pd.DataFrame({
        "driver_number": [1, 1, 1, 1, 1],
        "lap_number": [1, 2, 3, 4, 5],
        "lap_time_s": [90.0, 89.5, 89.2, 89.0, 88.8],
    })
    truncated = truncate_at_data_layer(df, cutoff_lap=3)

    assert len(truncated) == 3
    assert truncated["lap_number"].max() == 3
    assert not (df["lap_number"] > 3).empty


def test_truncate_at_data_layer_raises_on_invalid_cutoff() -> None:
    df = pd.DataFrame({
        "lap_number": [1, 2, 3],
    })
    with pytest.raises(AssertionError):
        truncate_at_data_layer(df, cutoff_lap=5)
