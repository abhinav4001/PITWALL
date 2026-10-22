from pathlib import Path

import pandas as pd
import pytest

from pitwall.data.models import FlagState, WorldState
from pitwall.realtime.replay import FastF1ReplaySource


@pytest.mark.asyncio
async def test_fastf1_replay_source(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "parquet"
    parquet_dir.mkdir()

    # Create dummy parquet lap file
    dummy_laps = pd.DataFrame({
        "driver_number": [1, 55, 1, 55],
        "lap_number": [1, 1, 2, 2],
        "lap_time_s": [90.1, 91.0, 89.5, 90.0],
        "Compound": ["MEDIUM", "HARD", "MEDIUM", "HARD"],
        "TyreLife": [1, 1, 2, 2],
        "Position": [1, 2, 1, 2],
    })
    dummy_laps.to_parquet(parquet_dir / "laps_2024_07_R.parquet", index=False)

    source = FastF1ReplaySource(session_key="2024_07_R", parquet_dir=parquet_dir)
    await source.start()

    assert source.max_lap == 2
    assert source.drivers == [1, 55]

    state1 = await source.next_state()
    assert isinstance(state1, WorldState)
    assert state1.lap == 1
    assert state1.total_laps == 2
    assert state1.flag_state == FlagState.GREEN
    assert 1 in state1.drivers
    assert state1.drivers[1].last_lap_s == 90.1
    assert state1.drivers[1].compound == "MEDIUM"

    state2 = await source.next_state()
    assert state2.lap == 2
    assert state2.drivers[55].last_lap_s == 90.0

    with pytest.raises(StopAsyncIteration):
        await source.next_state()

    await source.close()
