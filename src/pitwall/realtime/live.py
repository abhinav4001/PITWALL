"""Live OpenF1 session source.

Fetching "the position of every driver, every second" is the single most
demanding thing this app does against the OpenF1 API, and the obvious
implementation is also the one that breaks.

Measured behaviour of the OpenF1 free tier (2026-10-21):

* **20 concurrent per-driver requests => HTTP 429.**  The limiter allows
  about four requests per second; the fifth rapid request is rejected.
* **One date-windowed request returns every driver at once.**  For the 2024
  Austin race a two second window returns all 20 cars in 140 rows and takes
  0.73 s.

So the correct polling strategy is *not* concurrency, it is **windowing**: ask
for every driver in a single request instead of asking for each driver
separately.  This module therefore issues exactly two requests per tick (one
for positions, one for telemetry), each covering the whole field, and honours
the throttle inside :class:`~pitwall.data.openf1.OpenF1Client`.

The adapter presents the same surface as
:class:`~pitwall.realtime.session_replay.SessionReplayer`, so the pit wall
does not care whether it is watching a recording or the real thing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pitwall.data.models import FlagState
from pitwall.data.openf1 import OpenF1Client

logger = logging.getLogger(__name__)

#: OpenF1 reports the team colour as a bare hex string, without the "#".
DEFAULT_TEAM_COLOUR = "#71717a"


@dataclass
class LiveSnapshot:
    """One poll of a live session."""

    timestamp: datetime
    positions: list[dict[str, Any]] = field(default_factory=list)
    telemetry: list[dict[str, Any]] = field(default_factory=list)
    intervals: list[dict[str, Any]] = field(default_factory=list)
    order: list[dict[str, Any]] = field(default_factory=list)
    source: str = "openf1"

    @property
    def driver_numbers(self) -> list[int]:
        return sorted({int(row["driver_number"]) for row in self.positions})


class OpenF1LiveSource:
    """Polls a live OpenF1 session for the whole field at once."""

    def __init__(
        self,
        client: OpenF1Client,
        session_key: int,
        *,
        window: timedelta = timedelta(seconds=2),
        lookback: timedelta = timedelta(seconds=8),
    ) -> None:
        self.client = client
        self.session_key = session_key
        self.window = window
        self.lookback = lookback
        self.last_error: str | None = None

    async def poll(self, now: datetime | None = None) -> LiveSnapshot:
        """Fetch the latest positions, telemetry and gaps for every driver."""
        moment = now or datetime.now(UTC)
        start = (moment - self.lookback).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (moment + self.window).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Two windowed requests cover the whole field.
        positions = await self.client.get_location(
            self.session_key, date_gt=start, date_lt=end, ttl=1.0
        )
        telemetry = await self.client.get_car_data(
            self.session_key, date_gt=start, date_lt=end, ttl=1.0
        )
        intervals = await self.client.get_intervals(
            self.session_key, date_gt=start, date_lt=end, ttl=1.0
        )
        order = await self.client.get_position(self.session_key, ttl=5.0)

        if not positions and not telemetry:
            self.last_error = "no live data returned"
        else:
            self.last_error = None
        return LiveSnapshot(
            timestamp=moment,
            positions=positions,
            telemetry=telemetry,
            intervals=intervals,
            order=order,
        )


def latest_per_driver(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Keep the newest row for each driver."""
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:
        number = int(row["driver_number"])
        current = latest.get(number)
        if current is None or row["date"] > current["date"]:
            latest[number] = row
    return latest


def _parse_utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


async def find_live_session(
    client: OpenF1Client, *, circuit: str | None = None
) -> dict[str, Any] | None:
    """The session that is running right now, if there is one.

    A session is live when the current time falls between its start and end
    and it has not been cancelled.  This is what lets the pit wall switch
    itself from the replay to the real thing without anyone touching a
    setting.
    """
    now = datetime.now(UTC)
    sessions = await client.get_sessions(circuit_short_name=circuit)
    live = [
        session
        for session in sessions
        if session.get("date_start")
        and session.get("date_end")
        and not session.get("is_cancelled")
        and _parse_utc(session["date_start"]) <= now <= _parse_utc(session["date_end"])
    ]
    if not live:
        return None
    live.sort(key=lambda session: session["date_start"])
    return live[-1]


def team_colour(raw: Any) -> str:
    """Normalise a team colour to a CSS colour."""
    if not raw:
        return DEFAULT_TEAM_COLOUR
    text = str(raw).strip()
    if not text:
        return DEFAULT_TEAM_COLOUR
    if not text.startswith("#"):
        text = f"#{text}"
    if len(text) == 4:
        text = "#" + "".join(character * 2 for character in text[1:])
    return text if len(text) == 7 else DEFAULT_TEAM_COLOUR


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class OpenF1LiveAdapter:
    """Serves the tick loop from a live OpenF1 session.

    It presents the same surface as
    :class:`~pitwall.realtime.session_replay.SessionReplayer` so the rest of
    the app does not care whether it is watching a recording or the real
    thing.
    """

    def __init__(
        self,
        client: OpenF1Client,
        session_key: int,
        session_meta: dict[str, Any] | None = None,
        *,
        window: timedelta = timedelta(seconds=2),
        lookback: timedelta = timedelta(seconds=8),
    ) -> None:
        self.client = client
        self.session_key = session_key
        self.meta = session_meta or {}
        self.circuit = str(self.meta.get("circuit_short_name") or "Unknown")
        self.year = int(self.meta.get("year") or 0)
        self.source = OpenF1LiveSource(
            client, session_key, window=window, lookback=lookback
        )
        self.last_error: str | None = None
        self._drivers: dict[int, dict[str, Any]] = {}
        self._laps: list[dict[str, Any]] = []
        self._stints: list[dict[str, Any]] = []
        self._race_control: list[dict[str, Any]] = []
        self._snapshot: LiveSnapshot | None = None
        self._loaded = False

    async def refresh(self) -> bool:
        """Pull the latest data for the field.  Returns True on success."""
        snapshot = await self.source.poll()
        self._snapshot = snapshot
        if not snapshot.positions and not snapshot.telemetry:
            self.last_error = self.source.last_error or "no live data"
            return False
        self.last_error = None

        if not self._loaded:
            self._drivers = {
                int(d["driver_number"]): d
                for d in await self.client.get_drivers(self.session_key)
            }
            self._laps = await self.client.get_laps(self.session_key)
            self._stints = await self.client.get_stints(self.session_key)
            self._race_control = await self.client.get_race_control(self.session_key)
            self._loaded = True
        return True

    # -- the replay-compatible surface ---------------------------------
    def _driver_row(self, number: int) -> dict[str, Any]:
        return self._drivers.get(number, {})

    def _compound_and_age(self, number: int) -> tuple[str | None, int | None]:
        """The compound and tyre age from the stint table."""
        lap = self._current_lap(number)
        if lap is None:
            return None, None
        for stint in self._stints:
            if int(stint.get("lap_start") or 0) <= lap <= int(stint.get("lap_end") or 0):
                return (
                    str(stint.get("compound")),
                    int(stint.get("tyre_age_at_start") or 0)
                    + lap
                    - int(stint.get("lap_start") or 0),
                )
        return None, None

    def _current_lap(self, number: int) -> int | None:
        laps = [row for row in self._laps if int(row["driver_number"]) == number]
        if not laps:
            return None
        return max(int(row.get("lap_number") or 0) for row in laps)

    def _flag_state(self) -> FlagState:
        state = FlagState.GREEN
        for row in self._race_control:
            flag = (row.get("flag") or "").strip().upper()
            if flag in ("YELLOW", "DOUBLE YELLOW"):
                state = FlagState.YELLOW
            elif flag in ("VSC",):
                state = FlagState.VSC
            elif flag in ("SAFETY CAR", "SC"):
                state = FlagState.SAFETY_CAR
            elif flag == "RED":
                state = FlagState.RED
            elif flag == "GREEN":
                state = FlagState.GREEN
        return state

    def frame(self, replay_speed: float = 1.0) -> Any:
        """Build a race frame from the last poll."""
        from pitwall.realtime.session_replay import DriverFrame, RaceFrame

        snapshot = self._snapshot
        positions = latest_per_driver(snapshot.positions if snapshot else [])
        telemetry = latest_per_driver(snapshot.telemetry if snapshot else [])
        gaps = latest_per_driver(snapshot.intervals if snapshot else [])
        order = latest_per_driver(snapshot.order if snapshot else [])

        drivers: list[DriverFrame] = []
        for number, meta in self._drivers.items():
            position = positions.get(number)
            tel = telemetry.get(number, {})
            gap = gaps.get(number, {})
            compound, tyre_age = self._compound_and_age(number)
            drivers.append(
                DriverFrame(
                    driver_number=number,
                    code=str(meta.get("name_acronym") or f"D{number}"),
                    name=str(meta.get("full_name") or ""),
                    team=str(meta.get("team_name") or ""),
                    team_colour=team_colour(meta.get("team_colour")),
                    position=_position_of(order, number),
                    x=None if position is None else float(position.get("x") or 0.0),
                    y=None if position is None else float(position.get("y") or 0.0),
                    z=None if position is None else float(position.get("z") or 0.0),
                    speed=_number(tel.get("speed")),
                    throttle=_number(tel.get("throttle")),
                    brake=_number(tel.get("brake")),
                    gear=None if tel.get("n_gear") is None else int(tel["n_gear"]),
                    rpm=_number(tel.get("rpm")),
                    drs=None if tel.get("drs") is None else int(tel["drs"]),
                    drs_open=bool(tel.get("drs") is not None and int(tel["drs"]) > 9),
                    gap_to_leader=_number(gap.get("gap_to_leader")),
                    interval=_number(gap.get("interval")),
                    compound=compound,
                    tyre_age=tyre_age,
                    lap=self._current_lap(number),
                    last_lap_s=None,
                    on_track=position is not None,
                )
            )

        drivers.sort(key=lambda d: (d.position if d.position else 999, d.code))
        return RaceFrame(
            session_key=self.session_key,
            circuit=self.circuit,
            year=self.year,
            timestamp=datetime.now(UTC),
            elapsed_s=0.0,
            flag_state=self._flag_state(),
            drivers=drivers,
            source="openf1.live",
            replay_speed=replay_speed,
        )

    def recent_messages(self, limit: int = 8) -> list[dict[str, Any]]:
        """The most recent race-control messages."""
        out: list[dict[str, Any]] = []
        for row in self._race_control:
            message = (row.get("message") or "").strip()
            if not message:
                continue
            out.append(
                {
                    "date": row.get("date"),
                    "category": row.get("category"),
                    "flag": row.get("flag"),
                    "lap_number": row.get("lap_number"),
                    "message": message,
                }
            )
        return out[-limit:]


def _position_of(orders: dict[int, dict[str, Any]], number: int) -> int | None:
    value = orders.get(number, {}).get("position")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
