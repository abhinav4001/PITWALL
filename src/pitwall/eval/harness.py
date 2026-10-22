import pandas as pd


def truncate_at_data_layer(race_df: pd.DataFrame, cutoff_lap: int) -> pd.DataFrame:
    lap_col = "lap_number" if "lap_number" in race_df.columns else "LapNumber"
    if race_df.empty:
        return race_df.copy()

    max_lap_in_data = int(race_df[lap_col].max())
    assert max_lap_in_data > cutoff_lap, (
        f"Cutoff lap {cutoff_lap} must be mid-race (max lap is {max_lap_in_data})"
    )

    truncated = race_df[race_df[lap_col] <= cutoff_lap].copy()
    assert int(truncated[lap_col].max()) == cutoff_lap, (
        "Truncation failed to cap at cutoff lap"
    )
    assert len(race_df[race_df[lap_col] > cutoff_lap]) > 0, (
        "No post-cutoff rows existed to truncate"
    )

    return truncated
