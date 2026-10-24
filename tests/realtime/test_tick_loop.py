"""Tests for the tick loop.

The loop is the spine of the pit wall: it advances the replay, evaluates the
triggers and publishes the state.  These tests use a synthetic session pack.
"""

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from pitwall.data.session_pack import SessionPack
from pitwall.data.track import TrackProjection
from pitwall.realtime.session_replay import SessionReplayer
from pitwall.realtime.tick_loop import TickLoop


def _build_pack(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    drivers = [
        {
            "driver_number": number,
            "name_acronym": code,
            "full_name": f"Driver {number}",
            "team_name": "Team",
            "team_colour": "#ff0000",
        }
        for number, code in ((1, "AAA"), (2, "BBB"))
    ]

    for number in (1, 2):
        ts: list[int] = []
        xs: list[float] = []
        ys: list[float] = []
        for i in range(400):
            angle = 2 * math.pi * i / 400
            ts.append(int((base + timedelta(seconds=i * 0.25)).timestamp() * 1e9))
            xs.append(1000.0 * math.cos(angle))
            ys.append(1000.0 * math.sin(angle))
        np.savez_compressed(
            directory / f"location_{number}.npz",
            t=np.array(ts, dtype=np.int64),
            x=np.array(xs, dtype=np.float64),
            y=np.array(ys, dtype=np.float64),
            z=np.zeros(len(xs)),
        )
        np.savez_compressed(
            directory / f"car_data_{number}.npz",
            t=np.array(ts, dtype=np.int64),
            speed=np.full(len(ts), 250.0, dtype=np.float32),
            throttle=np.full(len(ts), 100.0, dtype=np.float32),
            brake=np.zeros(len(ts), dtype=np.float32),
            n_gear=np.full(len(ts), 7, dtype=np.int16),
            rpm=np.full(len(ts), 10000.0, dtype=np.float32),
            drs=np.zeros(len(ts), dtype=np.int16),
        )

    (directory / "meta.json").write_text(
        json.dumps(
            {
                "session": {"session_key": 1, "circuit_short_name": "Test", "year": 2024},
                "drivers": drivers,
            }
        ),
        encoding="utf-8",
    )
    (directory / "laps.json").write_text(
        json.dumps(
            [
                {
                    "driver_number": number,
                    "lap_number": 1,
                    "date_start": (base + timedelta(seconds=i * 2)).isoformat(),
                    "lap_duration": 99.0 + i,
                }
                for number in (1, 2)
                for i in range(20)
            ]
        ),
        encoding="utf-8",
    )
    (directory / "stints.json").write_text(
        json.dumps(
            [
                {
                    "driver_number": number,
                    "stint_number": 1,
                    "lap_start": 1,
                    "lap_end": 50,
                    "compound": "MEDIUM",
                    "tyre_age_at_start": 0,
                }
                for number in (1, 2)
            ]
        ),
        encoding="utf-8",
    )
    (directory / "intervals.json").write_text(
        json.dumps(
            [
                {
                    "driver_number": 1,
                    "date": (base + timedelta(seconds=1)).isoformat(),
                    "interval": 0.0,
                    "gap_to_leader": 0.0,
                },
                {
                    "driver_number": 2,
                    "date": (base + timedelta(seconds=1)).isoformat(),
                    "interval": 1.0,
                    "gap_to_leader": 1.0,
                },
            ]
        ),
        encoding="utf-8",
    )
    (directory / "position.json").write_text(
        json.dumps(
            [
                {
                    "driver_number": 1,
                    "date": (base + timedelta(seconds=1)).isoformat(),
                    "position": 1,
                },
                {
                    "driver_number": 2,
                    "date": (base + timedelta(seconds=1)).isoformat(),
                    "position": 2,
                },
            ]
        ),
        encoding="utf-8",
    )
    (directory / "race_control.json").write_text(
        json.dumps([{"date": base.isoformat(), "flag": "GREEN", "category": "Flag"}]),
        encoding="utf-8",
    )


@pytest.fixture()
def loop(tmp_path: Path) -> TickLoop:
    directory = tmp_path / "1"
    _build_pack(directory)
    pack = SessionPack.load(1, tmp_path)
    replayer = SessionReplayer(pack)
    projection = TrackProjection.from_outline(
        [
            (1000.0 * math.cos(2 * math.pi * i / 100), 1000.0 * math.sin(2 * math.pi * i / 100))
            for i in range(100)
        ]
    )
    return TickLoop(replayer, projection, None, snapshot_dir=tmp_path / "snapshots")


def test_tick_advances_the_clock_and_publishes_state(loop: TickLoop) -> None:
    before = loop.replayer.clock.elapsed_s
    result = loop.tick()
    assert loop.replayer.clock.elapsed_s > before
    assert result.state["available"] is True
    assert result.state["type"] == "state"
    assert len(result.state["drivers"]) == 2
    for driver in result.state["drivers"]:
        assert "view_x" in driver and "view_y" in driver


def test_alerts_are_broadcast_only_once(loop: TickLoop) -> None:
    """An alert must not be re-announced on the next tick."""
    loop.focus_driver = 2
    first = loop.tick()
    second = loop.tick()
    assert first.alerts == [] and second.alerts == []


def test_the_browser_moves_with_the_source(tmp_path: Path) -> None:
    """Switching to a live source must carry the open browser across.

    The page subscribes to one loop.  If the new loop does not take the
    subscribers with it, the browser keeps receiving the replay's frames -
    which is how a live practice session stayed hidden behind the 2024 race.
    """
    directory = tmp_path / "1"
    _build_pack(directory)
    pack = SessionPack.load(1, tmp_path)
    projection = TrackProjection.from_outline(
        [(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0)]
    )
    old = TickLoop(SessionReplayer(pack), projection, None, snapshot_dir=tmp_path / "snap")
    new = TickLoop(SessionReplayer(pack), projection, None, snapshot_dir=tmp_path / "snap")

    queue = old.subscribe()
    new.take_clients_from(old)

    assert old._listeners == []
    assert new._listeners == [queue]


def test_snapshot_resumes_the_clock(tmp_path: Path) -> None:
    directory = tmp_path / "1"
    _build_pack(directory)
    pack = SessionPack.load(1, tmp_path)
    replayer = SessionReplayer(pack)
    projection = TrackProjection.from_outline([(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0)])
    loop = TickLoop(replayer, projection, None, snapshot_dir=tmp_path / "snapshots")
    loop.replayer.seek(42.0)
    loop._save_snapshot()  # noqa: SLF001 - the snapshot API is internal

    replayer2 = SessionReplayer(SessionPack.load(1, tmp_path))
    loop2 = TickLoop(replayer2, projection, None, snapshot_dir=tmp_path / "snapshots")
    assert loop2.replayer.clock.elapsed_s == pytest.approx(42.0, abs=0.5)
