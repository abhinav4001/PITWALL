from pathlib import Path

import pandas as pd


def compute_empirical_pit_loss(
    parquet_dir: str | Path = "data/parquet", session_key: str | None = None
) -> float:
    """Median time lost to a pit stop, from real lap data.

    ``session_key`` is matched against the file stem, e.g. ``2024_19_R``
    matches ``laps_2024_19_R.parquet``.
    """
    p_dir = Path(parquet_dir)
    if not p_dir.exists():
        return 22.0

    parquet_files = list(p_dir.glob("laps_*.parquet"))
    if not parquet_files:
        return 22.0

    if session_key:
        parquet_files = [f for f in parquet_files if session_key in f.stem]
        if not parquet_files:
            return 22.0

    dfs = [pd.read_parquet(f) for f in parquet_files]
    df = pd.concat(dfs, ignore_index=True)

    if df.empty or "is_pit_in" not in df.columns:
        return 22.0

    # The loader keeps the original FastF1 column name ("LapTime"); accept a
    # pre-converted "lap_time_s" as well.
    time_column = "lap_time_s" if "lap_time_s" in df.columns else "LapTime"
    if time_column not in df.columns:
        return 22.0

    pit_mask = df.get("is_pit_in", False) | df.get("is_pit_out", False)
    time_series = pd.to_numeric(df[time_column], errors="coerce")
    pit_laps = df[pit_mask & time_series.notna()]
    if pit_laps.empty:
        return 22.0

    accurate_mask = df.get("IsAccurate", True)
    clean_laps = df[accurate_mask & time_series.notna()]
    reference_pace = (
        float(time_series[accurate_mask].median())
        if not clean_laps.empty
        else 90.0
    )

    diffs = time_series[pit_mask] - reference_pace
    valid_diffs = diffs[(diffs >= 12.0) & (diffs <= 45.0)]

    if valid_diffs.empty:
        return 22.0

    return float(valid_diffs.median())
