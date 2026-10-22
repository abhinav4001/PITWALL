"""Download and cache a complete OpenF1 session for replay.

A session pack holds every endpoint needed to reconstruct a race frame by
frame: car positions, telemetry, laps, intervals, stints, race order, race
control and the driver list.  It is downloaded **once** and then replayed from
disk, which is what makes the replay deterministic and keeps the upstream API
free for live use.

Layout on disk (``data/cache/sessions/<session_key>/``)::

    meta.json          session metadata + driver list
    location_<n>.npz   timestamps (int64 ns) + x/y/z for one driver
    car_data_<n>.npz   timestamps + speed/throttle/brake/gear/rpm/drs
    laps.json          lap table for every driver
    intervals.json     gap to leader over time
    stints.json        tyre stints
    position.json      race order over time
    race_control.json  flags and messages
    weather.json       the real per-minute weather over the track
    radio.json         team radio calls and their transcripts

Everything in here comes from the OpenF1 API.  Nothing is synthesised.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.data.openf1 import OpenF1Client

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("data/cache/sessions")

#: Endpoints stored as plain JSON (small, irregular shape).
_JSON_ENDPOINTS = ("laps", "intervals", "stints", "position", "race_control")


@dataclass
class SessionPack:
    """A complete, cached OpenF1 session."""

    session_key: int
    directory: Path
    meta: dict[str, Any] = field(default_factory=dict)
    drivers: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    @classmethod
    def directory_for(cls, session_key: int, base: str | Path = DEFAULT_DIR) -> Path:
        return Path(base) / str(session_key)

    @classmethod
    def exists(cls, session_key: int, base: str | Path = DEFAULT_DIR) -> bool:
        return (cls.directory_for(session_key, base) / "meta.json").exists()

    @classmethod
    def load(
        cls, session_key: int, base: str | Path = DEFAULT_DIR
    ) -> SessionPack:
        directory = cls.directory_for(session_key, base)
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        return cls(
            session_key=session_key,
            directory=directory,
            meta=meta.get("session", {}),
            drivers=meta.get("drivers", []),
        )

    # ------------------------------------------------------------------
    def _npz(self, kind: str, driver_number: int) -> dict[str, np.ndarray] | None:
        path = self.directory / f"{kind}_{driver_number}.npz"
        if not path.exists():
            return None
        with np.load(path) as data:
            return {key: data[key] for key in data.files}

    def location(self, driver_number: int) -> dict[str, np.ndarray] | None:
        return self._npz("location", driver_number)

    def car_data(self, driver_number: int) -> dict[str, np.ndarray] | None:
        return self._npz("car_data", driver_number)

    def json_table(self, name: str) -> list[dict[str, Any]]:
        path = self.directory / f"{name}.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    @property
    def driver_numbers(self) -> list[int]:
        return [int(d["driver_number"]) for d in self.drivers]

    @property
    def start_time(self) -> datetime | None:
        raw = self.meta.get("date_start")
        return datetime.fromisoformat(raw) if raw else None

    @property
    def end_time(self) -> datetime | None:
        raw = self.meta.get("date_end")
        return datetime.fromisoformat(raw) if raw else None


# ----------------------------------------------------------------------
# download
# ----------------------------------------------------------------------
def _timestamps(rows: list[dict[str, Any]]) -> np.ndarray:
    return np.array(
        [int(datetime.fromisoformat(r["date"]).timestamp() * 1_000_000_000) for r in rows],
        dtype=np.int64,
    )


async def download_session(
    client: OpenF1Client,
    session_key: int,
    base: str | Path = DEFAULT_DIR,
    *,
    drivers: list[int] | None = None,
) -> SessionPack:
    """Download a whole session and write it to the local cache."""
    directory = SessionPack.directory_for(session_key, base)
    directory.mkdir(parents=True, exist_ok=True)

    sessions = await client.get_sessions(session_key=session_key)
    session_meta = sessions[0] if sessions else {}
    driver_rows = await client.get_drivers(session_key)
    if drivers is None:
        drivers = [int(d["driver_number"]) for d in driver_rows]
    if not drivers:
        raise RuntimeError(f"no drivers reported for session {session_key}")

    for number in drivers:
        location = await client.get_location(session_key, driver_number=number)
        if location:
            np.savez_compressed(
                directory / f"location_{number}.npz",
                t=_timestamps(location),
                x=np.array([float(r["x"]) for r in location], dtype=np.float64),
                y=np.array([float(r["y"]) for r in location], dtype=np.float64),
                z=np.array([float(r.get("z") or 0) for r in location], dtype=np.float64),
            )
        else:
            logger.warning("session %s: no location for driver %s", session_key, number)

        car_data = await client.get_car_data(session_key, driver_number=number)
        if car_data:
            np.savez_compressed(
                directory / f"car_data_{number}.npz",
                t=_timestamps(car_data),
                speed=np.array([float(r["speed"]) for r in car_data], dtype=np.float32),
                throttle=np.array([float(r["throttle"]) for r in car_data], dtype=np.float32),
                brake=np.array([float(r["brake"]) for r in car_data], dtype=np.float32),
                n_gear=np.array([int(r["n_gear"]) for r in car_data], dtype=np.int16),
                rpm=np.array([float(r["rpm"]) for r in car_data], dtype=np.float32),
                drs=np.array([int(r["drs"]) for r in car_data], dtype=np.int16),
            )

    for name in _JSON_ENDPOINTS:
        if name == "position":
            rows = await client.get_position(session_key)
        elif name == "race_control":
            rows = await client.get_race_control(session_key)
        else:
            rows = await client._rows(name, {"session_key": session_key})  # noqa: SLF001
        (directory / f"{name}.json").write_text(
            json.dumps(rows, separators=(",", ":")), encoding="utf-8"
        )

    # The weather feed is keyed on the *meeting*, so one request covers the
    # whole weekend; keep only the readings that belong to this session.
    meeting_key = session_meta.get("meeting_key")
    weather_rows: list[dict[str, Any]] = []
    if meeting_key is not None:
        try:
            weather_rows = await client.get_weather(meeting_key=int(meeting_key))
        except Exception as exc:  # noqa: BLE001 - the weather is optional
            logger.warning("session %s: no weather feed (%s)", session_key, exc)
    session_weather = [
        row for row in weather_rows if row.get("session_key") == session_key
    ]
    (directory / "weather.json").write_text(
        json.dumps(session_weather, separators=(",", ":")), encoding="utf-8"
    )
    if not session_weather and weather_rows:
        logger.info(
            "session %s: the meeting has %d weather readings but none for this "
            "session",
            session_key,
            len(weather_rows),
        )

    # Team radio, where F1 published it for this session.
    radio_rows: list[dict[str, Any]] = []
    try:
        radio_rows = await client.get_team_radio(session_key)
    except Exception as exc:  # noqa: BLE001 - the radio is optional
        logger.warning("session %s: no team radio (%s)", session_key, exc)
    (directory / "radio_raw.json").write_text(
        json.dumps(radio_rows, separators=(",", ":")), encoding="utf-8"
    )

    meta = {
        "session": session_meta,
        "drivers": driver_rows,
        "downloaded_at": datetime.now().isoformat(),
    }
    (directory / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return SessionPack(
        session_key=session_key,
        directory=directory,
        meta=session_meta,
        drivers=driver_rows,
    )
