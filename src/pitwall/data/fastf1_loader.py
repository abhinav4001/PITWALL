from pathlib import Path

import fastf1
import pandas as pd


def get_cache_dir() -> Path:
    cache_dir = Path("data/cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_parquet_dir() -> Path:
    parquet_dir = Path("data/parquet")
    parquet_dir.mkdir(parents=True, exist_ok=True)
    return parquet_dir


def enable_fastf1_cache() -> Path:
    c_dir = get_cache_dir()
    fastf1.Cache.enable_cache(str(c_dir))
    return c_dir


def clean_lap_data(
    laps: pd.DataFrame, session_key: str, year: int, round_num: int
) -> pd.DataFrame:
    if laps.empty:
        return pd.DataFrame()

    df = laps.copy()

    time_cols = [
        "LapTime",
        "Sector1Time",
        "Sector2Time",
        "Sector3Time",
        "PitInTime",
        "PitOutTime",
    ]
    for col in time_cols:
        if col in df.columns:
            if pd.api.types.is_timedelta64_dtype(df[col].dtype):
                df[col] = df[col].dt.total_seconds()
            else:
                try:
                    parsed = pd.to_timedelta(df[col], errors="coerce")
                    df[col] = parsed.dt.total_seconds()
                except Exception:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

    df["session_key"] = str(session_key)
    df["year"] = int(year)
    df["round"] = int(round_num)

    if "DriverNumber" in df.columns:
        df["driver_number"] = pd.to_numeric(df["DriverNumber"], errors="coerce")
    if "LapNumber" in df.columns:
        df["lap_number"] = pd.to_numeric(df["LapNumber"], errors="coerce")

    df["is_pit_in"] = df["PitInTime"].notna() & (df["PitInTime"] > 0)
    df["is_pit_out"] = df["PitOutTime"].notna() & (df["PitOutTime"] > 0)

    return df


def load_and_cache_race_session(
    year: int, round_num: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    enable_fastf1_cache()

    session = fastf1.get_session(year, round_num, "R")
    session.load(telemetry=False, weather=True, messages=False)

    session_key = f"{year}_{round_num:02d}_R"

    laps_clean = clean_lap_data(session.laps, session_key, year, round_num)

    p_dir = get_parquet_dir()
    laps_path = p_dir / f"laps_{year}_{round_num:02d}.parquet"
    laps_clean.to_parquet(laps_path, index=False)

    meta_dict = {
        "session_key": [session_key],
        "year": [year],
        "round": [round_num],
        "circuit_short_name": [getattr(session.event, "Location", "Unknown")],
        "date": [str(session.date)],
        "session_type": ["R"],
    }
    meta_df = pd.DataFrame(meta_dict)
    meta_path = p_dir / f"meta_{year}_{round_num:02d}.parquet"
    meta_df.to_parquet(meta_path, index=False)

    return laps_clean, meta_df
