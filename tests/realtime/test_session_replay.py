"""Tests for the session replay engine.

Synthetic fixtures only — the replay engine is exercised against a hand-built
session pack, never against the live API (GR8).
"""

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from pitwall.data.models import FlagState
from pitwall.realtime.session_replay import (
    LAP_GAP_SECONDS,
    SessionReplayer,
    _parse_gap,
    project_frame,
)


def _msg(text: str, offset: float = 0.0) -> dict:
    """A race-control message, ``offset`` seconds after the session start."""
    stamp = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset)
    return {
        "date": stamp.isoformat(),
        "message": text,
    }


def _iso(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()


def _build_pack(directory: Path) -> None:
    """Write a small but complete session pack to disk."""
    directory.mkdir(parents=True, exist_ok=True)
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    drivers = [
        {
            "driver_number": 1,
            "name_acronym": "AAA",
            "full_name": "Driver One",
            "team_name": "Team One",
            "team_colour": "FF0000",
        },
        {
            "driver_number": 44,
            "name_acronym": "BBB",
            "full_name": "Driver Two",
            "team_name": "Team Two",
            "team_colour": "00FF00",
        },
    ]

    # A square circuit, driven by both drivers.
    for number in (1, 44):
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
            speed=np.full(len(ts), 300.0, dtype=np.float32),
            throttle=np.full(len(ts), 100.0, dtype=np.float32),
            brake=np.zeros(len(ts), dtype=np.float32),
            n_gear=np.full(len(ts), 8, dtype=np.int16),
            rpm=np.full(len(ts), 11000.0, dtype=np.float32),
            drs=np.full(len(ts), 12, dtype=np.int16),
        )

    (directory / "meta.json").write_text(
        json.dumps(
            {
                "session": {
                    "session_key": 1,
                    "circuit_short_name": "Test",
                    "year": 2024,
                    "session_name": "Race",
                },
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
                    "date_start": _iso(base, 0.0),
                    "lap_duration": 95.0,
                }
                for number in (1, 44)
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
                for number in (1, 44)
            ]
        ),
        encoding="utf-8",
    )
    (directory / "intervals.json").write_text(
        json.dumps(
            [
                {
                    "driver_number": 1,
                    "date": _iso(base, 1.0),
                    "interval": 0.0,
                    "gap_to_leader": 0.0,
                },
                {
                    "driver_number": 44,
                    "date": _iso(base, 1.0),
                    "interval": 1.5,
                    "gap_to_leader": 1.5,
                },
                {
                    "driver_number": 44,
                    "date": _iso(base, 50.0),
                    "interval": 2.0,
                    "gap_to_leader": "+1 LAP",
                },
            ]
        ),
        encoding="utf-8",
    )
    (directory / "position.json").write_text(
        json.dumps(
            [
                {"driver_number": 1, "date": _iso(base, 1.0), "position": 1},
                {"driver_number": 44, "date": _iso(base, 1.0), "position": 2},
            ]
        ),
        encoding="utf-8",
    )
    (directory / "race_control.json").write_text(
        json.dumps(
            [
                {
                    "date": _iso(base, 0.0),
                    "flag": "GREEN",
                    "category": "Flag",
                    "message": "GREEN LIGHT - PIT EXIT OPEN",
                },
                {
                    "date": _iso(base, 20.0),
                    "flag": "YELLOW",
                    "category": "Flag",
                    "message": "YELLOW IN TRACK SECTOR 3",
                },
                {
                    "date": _iso(base, 40.0),
                    "flag": "GREEN",
                    "category": "Flag",
                    "message": "CLEAR IN TRACK SECTOR 3",
                },
                # A message from another day of the weekend, which the
                # session filter has to drop.
                {
                    "date": _iso(base, -86400.0),
                    "flag": "GREEN",
                    "category": "Flag",
                    "message": "RISK OF RAIN FOR F1 RACE IS 0 %",
                },
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def replayer(tmp_path: Path) -> SessionReplayer:
    from pitwall.data.session_pack import SessionPack

    directory = tmp_path / "1"
    _build_pack(directory)
    return SessionReplayer(SessionPack.load(1, tmp_path))


def test_parse_gap_handles_lapped_string() -> None:
    assert _parse_gap(1.5) == pytest.approx(1.5)
    assert _parse_gap(None) != _parse_gap(None)  # NaN
    assert _parse_gap("+1 LAP") == LAP_GAP_SECONDS
    assert _parse_gap("garbage") != _parse_gap("garbage")


def test_replay_frame_contains_real_driver_data(replayer: SessionReplayer) -> None:
    replayer.seek(10.0)
    frame = replayer.frame()

    assert frame.session_key == 1
    assert frame.circuit == "Test"
    assert len(frame.drivers) == 2

    leader = frame.drivers[0]
    assert leader.driver_number == 1
    assert leader.position == 1
    assert leader.speed == pytest.approx(300.0)
    assert leader.gear == 8
    assert leader.throttle == pytest.approx(100.0)
    assert leader.drs_open is True
    assert leader.compound == "MEDIUM"
    # The stint starts on a fresh tyre, so the tyre age at the start of lap 1
    # is zero, matching the OpenF1 ``tyre_age_at_start`` convention.
    assert leader.tyre_age == 0
    assert leader.lap == 1
    assert leader.last_lap_s == pytest.approx(95.0)
    assert leader.x is not None and leader.y is not None


def test_replay_flag_follows_race_control(replayer: SessionReplayer) -> None:
    replayer.seek(10.0)
    assert replayer.frame().flag_state is FlagState.GREEN

    replayer.seek(30.0)
    assert replayer.frame().flag_state is FlagState.YELLOW

    replayer.seek(60.0)
    assert replayer.frame().flag_state is FlagState.GREEN


def test_replay_lapped_gap_is_reported(replayer: SessionReplayer) -> None:
    replayer.seek(60.0)
    frame = replayer.frame()
    second = next(d for d in frame.drivers if d.driver_number == 44)
    assert second.gap_to_leader == LAP_GAP_SECONDS


def test_projected_frame_adds_view_coordinates(replayer: SessionReplayer) -> None:
    from pitwall.data.track import TrackProjection

    replayer.seek(10.0)
    frame = replayer.frame()
    projection = TrackProjection.from_outline([(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0)])

    payload = project_frame(frame, projection)
    for driver in payload["drivers"]:
        assert "view_x" in driver and "view_y" in driver
        if driver["x"] is not None:
            assert driver["view_x"] is not None


def test_replay_seek_and_advance(replayer: SessionReplayer) -> None:
    replayer.seek(0.0)
    assert replayer.clock.elapsed_s == pytest.approx(0.0)
    replayer.advance(5.0)
    assert replayer.clock.elapsed_s == pytest.approx(5.0)
    replayer.seek(1_000_000.0)
    assert replayer.clock.elapsed_s <= replayer.clock.duration_s


def test_race_control_from_other_sessions_is_filtered_out(
    replayer: SessionReplayer,
) -> None:
    """The race-control feed is a meeting feed, so it leaks other sessions.

    The pre-race briefing given before the session starts is kept, but
    anything from a different day of the weekend is not.
    """
    messages = replayer.recent_messages(10**19, limit=500)
    assert messages
    for message in messages:
        stamp = datetime.fromisoformat(message["date"])
        # Only this session's own messages survive; the one from the previous
        # day of the weekend is dropped.
        assert stamp >= datetime(2024, 1, 1, 11, 30, 0, tzinfo=UTC)
    assert not any("RISK OF RAIN" in m["message"] for m in messages)


def test_rain_risk_ignores_the_digits_in_f1(replayer: SessionReplayer) -> None:
    """"RISK OF RAIN FOR F1 RACE IS 0 %" is a 0% risk, not 10%."""
    replayer._messages = [  # noqa: SLF001 - exercising the parser directly
        _msg("RISK OF RAIN FOR F1 RACE IS 0 %", 0.0)
    ]
    summary = replayer.weather_summary()
    assert summary["rain_risk_percent"] == 0.0


def test_weather_condition_uses_the_majority_of_reports(
    replayer: SessionReplayer,
) -> None:
    """A couple of slippery reports do not make a dry race wet."""
    replayer._messages = [  # noqa: SLF001
        _msg("TRACK SURFACE SLIPPERY IN TRACK SECTOR 4", 0.0),
        _msg("TRACK SURFACE SLIPPERY IN TRACK SECTOR 3", 1.0),
        _msg("CLEAR IN TRACK SECTOR 3", 2.0),
        _msg("CLEAR IN TRACK SECTOR 4", 3.0),
        _msg("TRACK CLEAR", 4.0),
    ]
    summary = replayer.weather_summary()
    assert summary["condition"] == "clear"
    assert summary["clear_reports"] == 3
    assert summary["slippery_reports"] == 2


def test_weather_condition_reports_slippery_when_that_is_the_majority(
    replayer: SessionReplayer,
) -> None:
    replayer._messages = [  # noqa: SLF001
        _msg("TRACK SURFACE SLIPPERY IN TRACK SECTOR 4", 0.0),
        _msg("TRACK SURFACE SLIPPERY IN TRACK SECTOR 3", 1.0),
        _msg("CLEAR IN TRACK SECTOR 3", 2.0),
    ]
    summary = replayer.weather_summary()
    assert summary["condition"] == "slippery"
