"""Replay a cached OpenF1 session as a live race.

The Pit Wall "live" view is driven by this module.  It replays the **real**
recorded data of a past session (for example the 2024 United States Grand Prix
at Austin) at a controllable speed, so the app behaves like a live race even
when nothing is actually running.

Everything shown to the user comes from the cached OpenF1 response: positions
come from ``/location``, telemetry from ``/car_data``, gaps from
``/intervals``, tyre compounds and ages from ``/laps`` and ``/stints``, and
flags from ``/race_control``.  Nothing is invented.

The driver state is emitted in the same coordinate system as the circuit
outline (``pitwall.data.track``), so the frontend can place a dot on the track
with a single projection.
"""

from __future__ import annotations

import bisect
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from pitwall.data.models import FlagState
from pitwall.data.session_pack import DEFAULT_DIR, SessionPack
from pitwall.data.track import TrackProjection

logger = logging.getLogger(__name__)

#: Placeholder gap (in seconds) used for a driver who has been lapped.  The
#: upstream feed reports ``"+1 LAP"`` for those drivers.
LAP_GAP_SECONDS = 999.0

#: Mapping of OpenF1 ``/race_control`` flags to the pit-wall flag states.
_FLAG_MAP: dict[str, FlagState] = {
    "GREEN": FlagState.GREEN,
    "YELLOW": FlagState.YELLOW,
    "YELLOW 1": FlagState.YELLOW,
    "YELLOW 2": FlagState.YELLOW,
    "YELLOW 3": FlagState.YELLOW,
    "DOUBLE YELLOW": FlagState.YELLOW,
    "VSC": FlagState.VSC,
    "VSC 1": FlagState.VSC,
    "VSC 2": FlagState.VSC,
    "SAFETY CAR": FlagState.SAFETY_CAR,
    "SC": FlagState.SAFETY_CAR,
    "RED": FlagState.RED,
}


def _flag_from(raw: str | None) -> FlagState:
    if not raw:
        return FlagState.GREEN
    return _FLAG_MAP.get(raw.strip().upper(), FlagState.GREEN)


def _sector_of(message: str) -> int | None:
    """Extract the track sector number from a race-control message."""
    marker = "TRACK SECTOR "
    index = message.find(marker)
    if index < 0:
        return None
    tail = message[index + len(marker) :]
    digits = ""
    for character in tail:
        if character.isdigit():
            digits += character
        else:
            break
    return int(digits) if digits else None


def _team_colour(raw: Any) -> str:
    """Normalise a team colour to a CSS colour.

    OpenF1 reports the colour as a bare hex string (``"3671C6"``) without the
    leading ``#``.  Passing that straight to an SVG ``fill`` is invalid, so the
    browser ignores it and the marker falls back to the default colour.  The
    ``#`` is added here so every consumer gets a usable value.
    """
    if not raw:
        return "#71717a"
    text = str(raw).strip()
    if not text:
        return "#71717a"
    if not text.startswith("#"):
        text = f"#{text}"
    if len(text) == 4:  # #abc -> #aabbcc
        text = "#" + "".join(character * 2 for character in text[1:])
    if len(text) == 7:
        return text
    return "#71717a"


def _parse_gap(value: Any) -> float:
    """Parse an OpenF1 gap value.

    The feed returns a float in seconds while the driver is on the lead lap
    and the string ``"+1 LAP"`` once they have been lapped.  Lapped drivers
    are reported with a large finite gap so the timing tower can still order
    them; the raw text is kept separately for display.
    """
    if value is None:
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("LAP") or text.endswith("LAPS"):
        return LAP_GAP_SECONDS
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _optional_float(value: Any) -> float | None:
    """Convert a telemetry value to ``float`` or ``None``."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compound_at(stint: dict[str, Any] | None, lap_row: dict[str, Any] | None) -> str | None:
    """The compound currently being run."""
    if stint and stint.get("compound"):
        return str(stint["compound"])
    if lap_row and lap_row.get("Compound"):
        return str(lap_row["Compound"])
    return None


def _tyre_age_at(
    stint: dict[str, Any] | None, lap_row: dict[str, Any] | None, lap: int | None
) -> int | None:
    """How many laps the current tyre has been running for.

    This is a whole number of laps on purpose: the tyre age is what the
    degradation is measured against, so it steps once per lap and never
    wobbles in between.

    When the stint rows do not cover the lap being run - which happens for
    a driver whose timing feed stopped part way through the session - the
    age is left at the age the stint began with.  Extrapolating it would
    invent a number that no data supports.
    """
    if stint is not None:
        age_at_start = int(stint.get("tyre_age_at_start") or 0)
        start = int(stint.get("lap_start") or 0)
        end = int(stint.get("lap_end") or 0)
        if lap is None or not (start <= lap <= (end or 10_000)):
            return age_at_start
        return age_at_start + (lap - start)
    if lap_row and lap_row.get("TyreLife") is not None:
        return int(lap_row["TyreLife"])
    return None


@dataclass
class ReplayClock:
    """Virtual race clock."""

    start_ns: int
    end_ns: int
    position_ns: int = 0

    def __post_init__(self) -> None:
        # The bounds come from numpy arrays, and a numpy comparison yields a
        # numpy bool that the JSON encoder cannot serialise.
        self.start_ns = int(self.start_ns)
        self.end_ns = int(self.end_ns)
        self.position_ns = int(self.position_ns)

    @property
    def duration_s(self) -> float:
        return (self.end_ns - self.start_ns) / 1e9

    @property
    def elapsed_s(self) -> float:
        return (self.position_ns - self.start_ns) / 1e9

    @property
    def finished(self) -> bool:
        return bool(self.position_ns >= self.end_ns)

    def advance(self, seconds: float) -> None:
        self.position_ns = min(self.end_ns, self.position_ns + int(seconds * 1e9))


@dataclass
class DriverFrame:
    """Everything known about one driver at a point in the replay."""

    driver_number: int
    code: str
    name: str
    team: str
    team_colour: str
    position: int | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None
    speed: float | None = None
    throttle: float | None = None
    brake: float | None = None
    gear: int | None = None
    rpm: float | None = None
    drs: int | None = None
    drs_open: bool = False
    gap_to_leader: float | None = None
    interval: float | None = None
    compound: str | None = None
    tyre_age: int | None = None
    lap: int | None = None
    last_lap_s: float | None = None
    #: The driver's fastest completed lap so far.  F1's live timing sends
    #: this separately from ``LastLapTime``, which can include a pit stop or
    #: an out-lap and therefore looks implausibly slow next to a flying lap.
    best_lap_s: float | None = None
    #: Speed at the feed's speed trap, in km/h.  This is real data that the
    #: timing feed publishes even when continuous ``CarData`` is unavailable.
    speed_trap: float | None = None
    on_track: bool = False
    #: Whether the timing feed is still reporting for this driver.  Some
    #: drivers stop reporting part way through a session - in the 2024
    #: United States Grand Prix OpenF1 has only two laps for Hamilton - and
    #: showing their last known gap as if it were current is misleading.
    reporting: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver_number": self.driver_number,
            "code": self.code,
            "name": self.name,
            "team": self.team,
            "team_colour": self.team_colour,
            "position": self.position,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "speed": self.speed,
            "throttle": self.throttle,
            "brake": self.brake,
            "gear": self.gear,
            "rpm": self.rpm,
            "drs": self.drs,
            "drs_open": self.drs_open,
            "gap_to_leader": self.gap_to_leader,
            "interval": self.interval,
            "compound": self.compound,
            "tyre_age": self.tyre_age,
            "lap": self.lap,
            "last_lap_s": self.last_lap_s,
            "best_lap_s": self.best_lap_s,
            "speed_trap": self.speed_trap,
            "on_track": self.on_track,
            "reporting": self.reporting,
        }


@dataclass
class RaceFrame:
    """A single instant of a replayed session."""

    session_key: int
    circuit: str
    year: int
    timestamp: datetime
    elapsed_s: float
    flag_state: FlagState
    drivers: list[DriverFrame]
    source: str = "openf1"
    replay_speed: float = 1.0
    total_laps: int | None = None
    lap: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "circuit": self.circuit,
            "year": self.year,
            "timestamp": self.timestamp.isoformat(),
            "elapsed_s": round(self.elapsed_s, 3),
            "flag_state": self.flag_state.value,
            "replay_speed": self.replay_speed,
            "lap": self.lap,
            "total_laps": self.total_laps,
            "drivers": [d.to_dict() for d in self.drivers],
            "source": self.source,
            "metadata": self.metadata,
        }


class _Series:
    """Time-indexed per-driver series with O(log n) lookups."""

    __slots__ = ("t", "values")

    def __init__(self, t: np.ndarray, values: dict[str, np.ndarray]) -> None:
        self.t = t
        self.values = values

    def at(self, when_ns: int) -> dict[str, Any]:
        if len(self.t) == 0:
            return {}
        index = int(np.searchsorted(self.t, when_ns, side="right")) - 1
        if index < 0:
            index = 0
        # Do not report a sample that is implausibly far in the future or past.
        if abs(int(self.t[index]) - when_ns) > 30_000_000_000:
            return {}
        return {key: value[index] for key, value in self.values.items()}

    def position_at(self, when_ns: int) -> tuple[float, float, float] | None:
        """Return ``(x, y, z)`` or ``None`` when the feed has no position.

        OpenF1 reports ``(0, 0)`` as a sentinel for "no data available", which
        must never be rendered as a position on the circuit.
        """
        if len(self.t) == 0:
            return None
        index = int(np.searchsorted(self.t, when_ns, side="right")) - 1
        if index < 0:
            return None
        if abs(int(self.t[index]) - when_ns) > 30_000_000_000:
            return None
        x = float(self.values["x"][index])
        y = float(self.values["y"][index])
        if x == 0.0 and y == 0.0:
            return None
        return x, y, float(self.values["z"][index])


class SessionReplayer:
    """Replays a cached OpenF1 session frame by frame."""

    def __init__(self, pack: SessionPack) -> None:
        self.pack = pack
        self.session_key = pack.session_key
        self.meta = pack.meta
        self.circuit = str(pack.meta.get("circuit_short_name") or "Unknown")
        self.year = int(pack.meta.get("year") or 0)

        self.drivers: dict[int, dict[str, Any]] = {
            int(d["driver_number"]): d for d in pack.drivers
        }
        self._location: dict[int, _Series] = {}
        self._car_data: dict[int, _Series] = {}
        for number in pack.driver_numbers:
            loc = pack.location(number)
            if loc and len(loc["t"]):
                self._location[number] = _Series(loc["t"], {k: loc[k] for k in ("x", "y", "z")})
            car = pack.car_data(number)
            if car and len(car["t"]):
                self._car_data[number] = _Series(
                    car["t"],
                    {
                        k: car[k]
                        for k in ("speed", "throttle", "brake", "n_gear", "rpm", "drs")
                    },
                )

        self._intervals = self._time_series(pack.json_table("intervals"))
        # When each driver's timing feed last said anything.  A driver whose
        # timing stops part way through a session is still shown, but is not
        # treated as still being in the race.
        self._last_timing_ns: dict[int, int] = {}
        for rows in (pack.json_table("position"), pack.json_table("intervals")):
            for row in rows:
                stamp = row.get("date")
                if not stamp:
                    continue
                when = int(datetime.fromisoformat(stamp).timestamp() * 1e9)
                number = int(row["driver_number"])
                if when > self._last_timing_ns.get(number, 0):
                    self._last_timing_ns[number] = when
        # Race control is a *meeting* feed: it also carries the messages from
        # the earlier sessions of the weekend.  Keeping only the messages
        # inside this session's own window stops the pit wall from showing,
        # for example, a rain-risk briefing given before the race.
        race_control = self._within_session(pack.json_table("race_control"))
        self._race_control = self._load_race_control(race_control)
        self._messages = self._load_messages(race_control)
        self._weather = self._load_weather(pack.json_table("weather"))
        self._laps = self._load_laps(pack.json_table("laps"))
        self._stints = self._load_stints(pack.json_table("stints"))
        self._positions = self._load_positions(pack.json_table("position"))

        starts = [s.t[0] for s in self._location.values() if len(s.t)]
        ends = [s.t[-1] for s in self._location.values() if len(s.t)]
        # The session starts with the formation lap and the garage, so anchor
        # the replay clock on the first *racing* lap instead of the first GPS
        # sample; otherwise the app opens on a grid of stationary cars.
        race_starts = [
            int(datetime.fromisoformat(row["date_start"]).timestamp() * 1e9)
            for rows in self._laps.values()
            for row in rows
            if isinstance(row.get("date_start"), str)
        ]
        start_ns = min(race_starts) if race_starts else (min(starts) if starts else 0)

        # The session does not end when the last GPS sample is taken: after
        # the flag the cars slow down and come round one more time, and those
        # laps are not racing.  The replay therefore ends with the last
        # racing lap, so the pit wall is not left showing a car doing 60 km/h
        # on the cool-down lap as if it were still racing.
        race_ends: list[int] = []
        for rows in self._laps.values():
            for row in rows:
                if not isinstance(row.get("date_start"), str):
                    continue
                when = int(datetime.fromisoformat(row["date_start"]).timestamp() * 1e9)
                duration = row.get("lap_duration")
                race_ends.append(
                    when + int(float(duration) * 1e9) if duration else when
                )
        end_ns = max(race_ends) if race_ends else (max(ends) if ends else start_ns)
        if ends:
            # Never run past the data that exists.
            end_ns = min(end_ns, max(ends))
        self.clock = ReplayClock(start_ns=start_ns, end_ns=end_ns)
        self.total_laps = self._max_lap()

    # ------------------------------------------------------------------
    @staticmethod
    def _time_series(rows: Iterable[dict[str, Any]]) -> dict[int, list[tuple[int, float, float]]]:
        """Group the ``/intervals`` feed by driver.

        ``gap_to_leader`` is a float while the driver is on the lead lap and
        the string ``"+1 LAP"`` once they have been lapped, so both forms are
        parsed here.
        """
        out: dict[int, list[tuple[int, float, float]]] = {}
        for row in rows:
            if not row.get("date"):
                continue
            when = int(datetime.fromisoformat(row["date"]).timestamp() * 1e9)
            number = int(row["driver_number"])
            out.setdefault(number, []).append(
                (
                    when,
                    _parse_gap(row.get("gap_to_leader")),
                    _parse_gap(row.get("interval")),
                )
            )
        for values in out.values():
            values.sort()
        return out

    def _within_session(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only the rows that belong to this session.

        The race-control feed is a *meeting* feed: it also carries the
        messages from the earlier sessions of the weekend, which is why a
        briefing given at Saturday's sprint used to show up in Sunday's race.

        The window is the span of the session's own data rather than the
        scheduled start and end, because a session really begins when the pit
        exit opens — that is where the pre-race briefings are given.
        """
        starts = [series.t[0] for series in self._location.values() if len(series.t)]
        ends = [series.t[-1] for series in self._location.values() if len(series.t)]
        if not starts or not ends:
            return rows
        # A small margin covers the pre-session briefing.
        start = datetime.fromtimestamp(
            (min(starts) - 30 * 60 * 1_000_000_000) / 1e9, tz=UTC
        )
        end = datetime.fromtimestamp(
            (max(ends) + 60 * 1_000_000_000) / 1e9, tz=UTC
        )

        kept: list[dict[str, Any]] = []
        for row in rows:
            stamp = row.get("date")
            if not stamp:
                continue
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if start <= when <= end:
                kept.append(row)
        return kept

    def weather_summary(self, at_ns: int | None = None) -> dict[str, Any]:
        """The real weather over the track.

        OpenF1 publishes a per-minute weather feed for every session:
        air and track temperature, humidity, pressure, wind speed and
        direction, and rainfall.  That is what is shown here.

        The race-control broadcasts (the rain-risk briefing and the track
        surface reports) are used as the **fallback** for sessions whose
        weather feed was not retained, and they are still the only source for
        the rain-risk figure, which race control states as a percentage.
        """
        readings = self._weather
        rain_risk: float | None = None
        clear = 0
        slippery = 0
        for message in self._messages:
            text = str(message.get("message") or "").upper()
            if "RISK OF RAIN" in text:
                # Take the number that sits next to the percentage sign: the
                # text also contains "F1 RACE", whose "1" would otherwise be
                # read as part of the risk figure.
                match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
                rain_risk = float(match.group(1)) if match else None
            elif "SLIPPERY" in text or "WET" in text:
                slippery += 1
            elif "CLEAR IN TRACK SECTOR" in text or "TRACK CLEAR" in text:
                clear += 1

        if not readings:
            # No weather feed for this session: fall back to what race control
            # broadcast, taking the condition from the majority of the
            # surface reports so one stale message cannot make the race look
            # wet.
            total = clear + slippery
            condition = "slippery" if total > 0 and slippery > clear else "clear"
            return {
                "available": rain_risk is not None or total > 0,
                "source": "openf1.race_control",
                "rain_risk_percent": rain_risk,
                "condition": condition,
                "clear_reports": clear,
                "slippery_reports": slippery,
                "readings": [],
                "note": (
                    "Race control broadcast the rain risk and the track "
                    "surface reports. The condition is taken from the majority "
                    "of the surface reports, because a sector that was briefly "
                    "slippery and then cleared does not make the race wet."
                ),
            }

        # The reading for the moment being replayed, else the last one.
        chosen = readings[-1]
        if at_ns is not None:
            for reading in readings:
                stamp = reading.get("date")
                if not stamp:
                    continue
                when = int(datetime.fromisoformat(stamp).timestamp() * 1e9)
                if when <= at_ns:
                    chosen = reading
                else:
                    break

        raining = bool(chosen.get("rainfall"))
        condition = "wet" if raining else "clear"

        return {
            "available": True,
            "source": "openf1.weather",
            "rain_risk_percent": rain_risk,
            "condition": condition,
            "clear_reports": clear,
            "slippery_reports": slippery,
            "readings": [
                {
                    "date": reading.get("date"),
                    "air_temperature": reading.get("air_temperature"),
                    "track_temperature": reading.get("track_temperature"),
                    "humidity": reading.get("humidity"),
                    "pressure": reading.get("pressure"),
                    "wind_speed": reading.get("wind_speed"),
                    "wind_direction": reading.get("wind_direction"),
                    "rainfall": reading.get("rainfall"),
                }
                for reading in readings
            ],
            "current": {
                "date": chosen.get("date"),
                "air_temperature": chosen.get("air_temperature"),
                "track_temperature": chosen.get("track_temperature"),
                "humidity": chosen.get("humidity"),
                "pressure": chosen.get("pressure"),
                "wind_speed": chosen.get("wind_speed"),
                "wind_direction": chosen.get("wind_direction"),
                "rainfall": chosen.get("rainfall"),
            },
            "note": (
                "The real weather feed over the track, published every minute "
                "by the timing data."
            ),
        }

    @staticmethod
    def _load_weather(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The per-minute weather readings, in order.

        OpenF1 publishes these for every session of a meeting, keyed on the
        session, so a pack carries only the readings for its own session.
        """
        readings: list[dict[str, Any]] = []
        for row in rows:
            stamp = row.get("date")
            if not stamp:
                continue
            readings.append(
                {
                    "date": stamp,
                    "air_temperature": row.get("air_temperature"),
                    "track_temperature": row.get("track_temperature"),
                    "humidity": row.get("humidity"),
                    "pressure": row.get("pressure"),
                    "wind_speed": row.get("wind_speed"),
                    "wind_direction": row.get("wind_direction"),
                    "rainfall": row.get("rainfall"),
                }
            )
        readings.sort(key=lambda reading: str(reading.get("date") or ""))
        return readings

    @staticmethod
    def _load_race_control(rows: list[dict[str, Any]]) -> list[tuple[int, FlagState]]:
        """Track-level flag state over time.

        Race control reports flags per *sector*, so a "YELLOW IN TRACK SECTOR
        3" is only cleared by "CLEAR IN TRACK SECTOR 3".  A single yellow in
        any sector keeps the track yellow; the track returns to green only
        when every yellow sector has been cleared or "TRACK CLEAR" is sent.
        """
        events: list[tuple[int, FlagState]] = []
        yellow_sectors: set[int] = set()
        for row in sorted(rows, key=lambda r: r.get("date") or ""):
            if not row.get("date"):
                continue
            when = int(datetime.fromisoformat(row["date"]).timestamp() * 1e9)
            flag = (row.get("flag") or "").strip().upper()
            message = (row.get("message") or "").strip().upper()

            if "DOUBLE YELLOW" in flag or "DOUBLE YELLOW" in message:
                sector = _sector_of(message)
                if sector is not None:
                    yellow_sectors.add(sector)
                events.append((when, FlagState.YELLOW))
            elif flag == "YELLOW" or message.startswith("YELLOW IN TRACK SECTOR"):
                sector = _sector_of(message)
                if sector is not None:
                    yellow_sectors.add(sector)
                events.append((when, FlagState.YELLOW))
            elif flag == "VSC" or "VSC" in flag:
                events.append((when, FlagState.VSC))
            elif flag in ("SAFETY CAR", "SC"):
                events.append((when, FlagState.SAFETY_CAR))
            elif flag == "RED" or "RED FLAG" in message:
                events.append((when, FlagState.RED))
            elif flag == "CHEQUERED":
                events.append((when, FlagState.GREEN))
            elif message.startswith("CLEAR IN TRACK SECTOR"):
                sector = _sector_of(message)
                if sector is not None:
                    yellow_sectors.discard(sector)
                events.append(
                    (when, FlagState.YELLOW if yellow_sectors else FlagState.GREEN)
                )
            elif message.startswith("TRACK CLEAR") or flag == "GREEN":
                yellow_sectors.clear()
                events.append((when, FlagState.GREEN))
        return events

    @staticmethod
    def _load_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Race-control messages worth showing on the pit wall."""
        interesting: list[dict[str, Any]] = []
        for row in rows:
            message = (row.get("message") or "").strip()
            if not message:
                continue
            interesting.append(
                {
                    "date": row.get("date"),
                    "category": row.get("category"),
                    "flag": row.get("flag"),
                    "lap_number": row.get("lap_number"),
                    "message": message,
                }
            )
        return interesting

    @staticmethod
    def _load_laps(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        out: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            number = int(row["driver_number"])
            out.setdefault(number, []).append(row)
        for values in out.values():
            values.sort(key=lambda r: int(r.get("lap_number") or 0))
        return out

    @staticmethod
    def _load_stints(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        out: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(int(row["driver_number"]), []).append(row)
        return out

    @staticmethod
    def _load_positions(rows: list[dict[str, Any]]) -> dict[int, list[tuple[int, int]]]:
        out: dict[int, list[tuple[int, int]]] = {}
        for row in rows:
            when = int(datetime.fromisoformat(row["date"]).timestamp() * 1e9)
            out.setdefault(int(row["driver_number"]), []).append((when, int(row["position"])))
        for values in out.values():
            values.sort()
        return out

    def _max_lap(self) -> int | None:
        laps = [int(r.get("lap_number") or 0) for rows in self._laps.values() for r in rows]
        return max(laps) if laps else None

    # ------------------------------------------------------------------
    def seek(self, elapsed_s: float) -> None:
        self.clock.position_ns = int(
            self.clock.start_ns + max(0.0, elapsed_s) * 1e9
        )
        self.clock.position_ns = min(self.clock.position_ns, self.clock.end_ns)

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    @property
    def finished(self) -> bool:
        return self.clock.finished

    # ------------------------------------------------------------------
    def _flag_at(self, when_ns: int) -> FlagState:
        state = FlagState.GREEN
        for event_when, event_flag in self._race_control:
            if event_when > when_ns:
                break
            state = event_flag
        return state

    def _gap_at(self, number: int, when_ns: int) -> tuple[float | None, float | None]:
        rows = self._intervals.get(number)
        if not rows:
            return None, None
        times = [r[0] for r in rows]
        index = bisect.bisect_right(times, when_ns) - 1
        if index < 0:
            return None, None
        _, gap, interval = rows[index]
        return (
            None if np.isnan(gap) else gap,
            None if np.isnan(interval) else interval,
        )

    def _position_at(self, number: int, when_ns: int) -> int | None:
        rows = self._positions.get(number)
        if not rows:
            return None
        times = [r[0] for r in rows]
        index = bisect.bisect_right(times, when_ns) - 1
        if index < 0:
            return None
        return rows[index][1]

    def _lap_at(self, number: int, when_ns: int) -> dict[str, Any] | None:
        rows = self._laps.get(number)
        if not rows:
            return None
        timed = [r for r in rows if isinstance(r.get("date_start"), str)]
        if not timed:
            return None
        times = [int(datetime.fromisoformat(r["date_start"]).timestamp() * 1e9) for r in timed]
        index = bisect.bisect_right(times, when_ns) - 1
        if index < 0:
            return None
        return timed[index]

    def _first_stint(self, number: int) -> dict[str, Any] | None:
        """The stint a driver started the session on."""
        stints = self._stints.get(number) or []
        if not stints:
            return None
        return min(stints, key=lambda stint: int(stint.get("lap_start") or 0))

    def _stint_at(self, number: int, lap: int | None) -> dict[str, Any] | None:
        """The stint a driver is on at a given lap.

        The stint rows do not always cover the lap being run: OpenF1 has
        only one stint for Hamilton in the 2024 United States Grand Prix,
        covering lap one, so a lookup for lap twenty finds nothing.  The
        tyre a driver is on is then the one whose stint started most
        recently before that lap, which is what this falls back to.
        """
        if lap is None:
            return self._first_stint(number)
        latest: dict[str, Any] | None = None
        for stint in self._stints.get(number, []):
            start = int(stint.get("lap_start") or 0)
            end = int(stint.get("lap_end") or 0)
            if start <= lap <= (end or 10_000):
                return stint
            if start <= lap and (latest is None or start > int(latest.get("lap_start") or 0)):
                latest = stint
        return latest if latest is not None else self._first_stint(number)

    # ------------------------------------------------------------------
    # accessors used by the pit-wall engine
    # ------------------------------------------------------------------
    def lap_times(self, driver_number: int) -> dict[int, float]:
        """Real lap times of a driver, keyed by lap number."""
        times: dict[int, float] = {}
        for row in self._laps.get(driver_number, []):
            lap = row.get("lap_number")
            duration = row.get("lap_duration")
            if lap is None or duration is None:
                continue
            times[int(lap)] = float(duration)
        return times

    def elapsed_race_time(self, driver_number: int, at_lap: int) -> float:
        """Race time actually run by a driver up to (and including) a lap."""
        times = self.lap_times(driver_number)
        return sum(duration for lap, duration in times.items() if lap <= at_lap)

    def driver_pace(self, driver_number: int, at_lap: int | None = None) -> float | None:
        """The driver's own median lap time, from real laps.

        Projecting a driver with the circuit-wide average pace would be wrong
        by seconds per lap, so the strategy engine anchors on this instead.
        """
        times = self.lap_times(driver_number)
        if at_lap is not None:
            times = {lap: value for lap, value in times.items() if lap <= at_lap}
        if len(times) < 3:
            return None
        return float(np.median(list(times.values())))

    def stint_for(self, driver_number: int, lap: int | None) -> dict[str, Any] | None:
        return self._stint_at(driver_number, lap)

    def recent_messages(self, when_ns: int, limit: int = 8) -> list[dict[str, Any]]:
        """Race-control messages emitted so far, newest last."""
        out: list[dict[str, Any]] = []
        for message in self._messages:
            if not message.get("date"):
                continue
            stamp = int(datetime.fromisoformat(message["date"]).timestamp() * 1e9)
            if stamp <= when_ns:
                out.append(message)
        return out[-limit:]

    def positions_at(self, when_ns: int) -> dict[int, tuple[float, float]]:
        """Every driver's position at a given instant.

        The tick loop uses this to publish where each car will be one tick
        from now, so the browser can interpolate between the two frames and
        the cars move smoothly instead of jumping once a second.
        """
        out: dict[int, tuple[float, float]] = {}
        for number, series in self._location.items():
            position = series.position_at(when_ns)
            if position is not None:
                out[number] = (position[0], position[1])
        return out

    def _is_reporting(self, number: int, when_ns: int) -> bool:
        """Whether the timing feed is still reporting for this driver.

        The window is generous - a driver is only marked as gone once their
        last timing entry is well over a minute old, which is far longer
        than the four-second gap feed would ever be.
        """
        last = self._last_timing_ns.get(number)
        if last is None:
            return True
        return when_ns - last <= 60 * 1_000_000_000

    # ------------------------------------------------------------------
    def frame(self, replay_speed: float = 1.0) -> RaceFrame:
        """Build the frame for the current clock position."""
        when = self.clock.position_ns
        flag = self._flag_at(when)
        frames: list[DriverFrame] = []

        for number, driver in self.drivers.items():
            meta = self.drivers[number]
            location = self._location.get(number)
            position = location.position_at(when) if location else None
            telemetry = self._car_data.get(number).at(when) if number in self._car_data else {}

            lap_row = self._lap_at(number, when)
            lap = int(lap_row["lap_number"]) if lap_row else None
            stint = self._stint_at(number, lap)
            if stint is None and lap is None:
                # At the start of a session a driver may not have crossed the
                # line yet, so there is no lap row to match the stint
                # against.  They are on their first stint all the same.
                stint = self._first_stint(number)
            gap, interval = self._gap_at(number, when)
            position_order = self._position_at(number, when)

            speed = telemetry.get("speed")
            drs = telemetry.get("drs")
            frames.append(
                DriverFrame(
                    driver_number=number,
                    code=str(meta.get("name_acronym") or f"D{number}"),
                    name=str(meta.get("full_name") or meta.get("broadcast_name") or ""),
                    team=str(meta.get("team_name") or ""),
                    team_colour=_team_colour(meta.get("team_colour")),
                    position=position_order,
                    x=None if position is None else position[0],
                    y=None if position is None else position[1],
                    z=None if position is None else position[2],
                    speed=None if speed is None else float(speed),
                    throttle=_optional_float(telemetry.get("throttle")),
                    brake=_optional_float(telemetry.get("brake")),
                    gear=None if telemetry.get("n_gear") is None else int(telemetry["n_gear"]),
                    rpm=_optional_float(telemetry.get("rpm")),
                    drs=None if drs is None else int(drs),
                    drs_open=bool(drs is not None and int(drs) > 9),
                    gap_to_leader=gap,
                    interval=interval,
                    compound=_compound_at(stint, lap_row),
                    tyre_age=_tyre_age_at(stint, lap_row, lap),
                    lap=lap,
                    last_lap_s=(
                        float(lap_row["lap_duration"])
                        if lap_row and lap_row.get("lap_duration") is not None
                        else None
                    ),
                    on_track=position is not None and (speed is None or float(speed) > 1.0),
                    reporting=self._is_reporting(number, when),
                )
            )

        frames.sort(key=lambda f: (f.position if f.position else 999, f.code))
        return RaceFrame(
            session_key=self.session_key,
            circuit=self.circuit,
            year=self.year,
            timestamp=datetime.fromtimestamp(when / 1e9, tz=UTC),
            elapsed_s=self.clock.elapsed_s,
            flag_state=flag,
            drivers=frames,
            replay_speed=replay_speed,
            total_laps=self.total_laps,
            lap=max((f.lap or 0) for f in frames) if frames else None,
        )


def open_replayer(
    session_key: int, base: str | Path = DEFAULT_DIR
) -> SessionReplayer:
    """Open a cached session for replay."""
    if not SessionPack.exists(session_key, base):
        raise FileNotFoundError(
            f"session {session_key} is not cached; run scripts/download_session.py first"
        )
    return SessionReplayer(SessionPack.load(session_key, base))


def project_frame(frame: RaceFrame, projection: TrackProjection) -> dict[str, Any]:
    """Add viewBox coordinates to every driver so the UI can draw them."""
    data = frame.to_dict()
    for driver in data["drivers"]:
        if driver["x"] is None or driver["y"] is None:
            driver["view_x"] = None
            driver["view_y"] = None
            continue
        view_x, view_y = projection.project(float(driver["x"]), float(driver["y"]))
        driver["view_x"] = view_x
        driver["view_y"] = view_y
    return data
