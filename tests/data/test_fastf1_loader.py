import pandas as pd

from pitwall.data.fastf1_loader import clean_lap_data


def test_clean_lap_data_converts_timedeltas_and_adds_metadata() -> None:
    raw_data = {
        "DriverNumber": ["1", "44"],
        "LapNumber": [1, 2],
        "LapTime": [pd.Timedelta(seconds=90.5), pd.Timedelta(seconds=88.2)],
        "Sector1Time": [pd.Timedelta(seconds=30.1), pd.Timedelta(seconds=29.8)],
        "Sector2Time": [pd.Timedelta(seconds=30.2), pd.Timedelta(seconds=29.9)],
        "Sector3Time": [pd.Timedelta(seconds=30.2), pd.Timedelta(seconds=28.5)],
        "PitInTime": [pd.NaT, pd.Timedelta(seconds=45.0)],
        "PitOutTime": [pd.NaT, pd.NaT],
        "Compound": ["MEDIUM", "HARD"],
        "TyreLife": [1, 15],
        "FreshTyre": [True, False],
        "IsAccurate": [True, True],
        "Position": [1, 2],
    }
    df = pd.DataFrame(raw_data)
    cleaned = clean_lap_data(df, session_key="2024_07_R", year=2024, round_num=7)

    assert "LapTime" in cleaned.columns
    assert bool(cleaned.loc[cleaned["driver_number"] == 44, "is_pit_in"].iloc[0]) is True
    assert bool(cleaned.loc[cleaned["driver_number"] == 1, "is_pit_in"].iloc[0]) is False
    assert cleaned["session_key"].iloc[0] == "2024_07_R"
    assert cleaned["year"].iloc[0] == 2024
    assert cleaned["round"].iloc[0] == 7
