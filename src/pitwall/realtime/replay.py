from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pitwall.data.models import DriverState, FlagState, WorldState, WorldStateDelta
from pitwall.realtime.source import DataSource


def _as_int(value: object, default: int | None = None) -> int | None:
    """Convert a value to ``int``, returning ``default`` for NaN/None."""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class FastF1ReplaySource(DataSource):
    """Replays lap data from a FastF1 parquet export."""

    def __init__(
        self, session_key: str, parquet_dir: str | Path = "data/parquet"
    ) -> None:
        self.session_key = session_key
        self.parquet_dir = Path(parquet_dir)
        self.laps_df: pd.DataFrame = pd.DataFrame()
        self.current_lap: int = 0
        self.max_lap: int = 0
        self.drivers: list[int] = []
        self._is_started: bool = False

    async def start(self) -> None:
        laps_path = self.parquet_dir / f"laps_{self.session_key}.parquet"
        if not laps_path.exists():
            matches = list(
                self.parquet_dir.glob(f"laps_*{self.session_key}*.parquet")
            )
            if not matches:
                raise FileNotFoundError(
                    f"No parquet lap file for session {self.session_key}"
                )
            laps_path = matches[0]

        self.laps_df = pd.read_parquet(laps_path)
        if self.laps_df.empty:
            raise ValueError(f"Lap data for session {self.session_key} is empty")

        if "lap_number" in self.laps_df.columns:
            self.max_lap = int(self.laps_df["lap_number"].max())
        else:
            self.max_lap = int(self.laps_df["LapNumber"].max())

        driver_col = (
            "driver_number"
            if "driver_number" in self.laps_df.columns
            else "DriverNumber"
        )
        self.drivers = sorted(
            self.laps_df[driver_col].dropna().unique().astype(int).tolist()
        )
        self.current_lap = 0
        self._is_started = True

    async def next_state(self) -> WorldState | WorldStateDelta:
        if not self._is_started:
            raise RuntimeError(
                "FastF1ReplaySource must be started before calling next_state()"
            )

        if self.current_lap >= self.max_lap:
            raise StopAsyncIteration("End of replay session")

        self.current_lap += 1
        timestamp = datetime.now(UTC)

        lap_col = (
            "lap_number"
            if "lap_number" in self.laps_df.columns
            else "LapNumber"
        )
        driver_col = (
            "driver_number"
            if "driver_number" in self.laps_df.columns
            else "DriverNumber"
        )

        lap_subset = self.laps_df[self.laps_df[lap_col] == self.current_lap]

        driver_states: dict[int, DriverState] = {}
        for _, row in lap_subset.iterrows():
            d_num = int(row[driver_col])
            pos = _as_int(row.get("Position", row.get("position")))
            compound = row.get("Compound", row.get("compound"))
            compound = None if compound is None or pd.isna(compound) else str(compound)
            tyre_life = _as_int(row.get("TyreLife", row.get("tyre_life")))

            lap_time_key = (
                "lap_time_s"
                if "lap_time_s" in row
                else ("LapTime" if "LapTime" in row else None)
            )
            last_lap_s = _as_float(row[lap_time_key]) if lap_time_key else None

            driver_states[d_num] = DriverState(
                driver_number=d_num,
                position=pos if pos and pos > 0 else None,
                compound=compound,
                tyre_age_laps=tyre_life,
                last_lap_s=last_lap_s if last_lap_s and last_lap_s > 0 else None,
            )

        return WorldState(
            session_key=self.session_key,
            timestamp=timestamp,
            lap=self.current_lap,
            total_laps=self.max_lap,
            flag_state=FlagState.GREEN,
            drivers=driver_states,
        )

    async def close(self) -> None:
        self._is_started = False
        self.laps_df = pd.DataFrame()
