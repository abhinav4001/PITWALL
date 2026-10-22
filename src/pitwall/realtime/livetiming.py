"""The official F1 live timing feed.

This is F1's own timing hub - the one the F1 website and the F1 app use - and
it is **free and unauthenticated**.

The important discovery, and the reason a naive attempt fails:

* F1 has a **legacy** SignalR endpoint at ``/signalr/``.  It is now behind
  authentication and answers **HTTP 401**.  LiveF1 hardcodes that endpoint
  (``livef1/utils/constants.py``: ``SIGNALR_ENDPOINT = "/signalr/"``), which
  is why LiveF1 appears to be broken.
* F1's **current** endpoint is ``/signalrcore``.  It is what the F1 website
  itself points at - the live-timing configuration embedded in the site's
  page data says so:

      NEXT_PUBLIC_GLOBAL_LIVETIMING_TIMINGHUB = https://livetiming.formula1.com/signalrcore
      NEXT_PUBLIC_GLOBAL_EVENTTRACKER_URL    = https://livetiming.formula1.com/signalrcore

  ``/signalrcore/negotiate`` answers **POST** (not GET) and hands back a
  connection token; the websocket is then opened at
  ``wss://livetiming.formula1.com/signalrcore?id=<token>``.

Verified on 2026-10-22:

    POST /signalrcore/negotiate?negotiateVersion=1  -> 200, connectionToken
    wss://.../signalrcore?id=<token>                  -> handshake "{}"
    Subscribe                                         -> accepted

The historical ``/static/.../*.jsonStream`` archive is still gated (401),
but the **audio assets** under ``/static/.../TeamRadio/*.mp3`` are not, which
is how the replayed radio is obtained.

This module speaks the hub protocol directly rather than depending on
LiveF1, so it works with the open endpoint and can be pointed at the gated
one later if F1 ever reopens it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import websockets

from pitwall.data.models import FlagState
from pitwall.realtime.live import LiveSnapshot

logger = logging.getLogger(__name__)

#: The open hub endpoint.  F1's legacy ``/signalr/`` is behind authentication.
HUB_BASE = "https://livetiming.formula1.com/signalrcore"

#: The websocket address.  The token goes in the ``id`` query parameter.
WS_BASE = "wss://livetiming.formula1.com/signalrcore"

#: The record separator of the SignalR JSON protocol.
SEPARATOR = "\x1e"

#: The hub the F1 timing data lives on.
HUB_NAME = "Streaming"

#: The topics a pit wall needs.
TOPICS = (
    "SessionInfo.z",
    "SessionStatus.z",
    "DriverList.z",
    "LiveTimingData.z",
    "CarData.z",
    "Position.z",
    "TimingData.z",
    "WeatherData.z",
    "TeamRadio.z",
    "RaceControlMessages.z",
)

#: F1 reports the team colour as a bare hex string, without the "#".
DEFAULT_TEAM_COLOUR = "#71717a"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Origin": "https://www.formula1.com",
    "Referer": "https://www.formula1.com/",
}


def _colour(value: Any) -> str:
    """Normalise a team colour to "#rrggbb"."""
    if not value:
        return DEFAULT_TEAM_COLOUR
    text = str(value).strip()
    if not text:
        return DEFAULT_TEAM_COLOUR
    if not text.startswith("#"):
        text = f"#{text}"
    return text


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class HubState:
    """Everything the hub has told us so far."""

    session: dict[str, Any] = field(default_factory=dict)
    drivers: dict[int, dict[str, Any]] = field(default_factory=dict)
    cars: dict[int, dict[str, Any]] = field(default_factory=dict)
    order: dict[int, int] = field(default_factory=dict)
    timing: dict[int, dict[str, Any]] = field(default_factory=dict)
    weather: list[dict[str, Any]] = field(default_factory=list)
    radio: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.cars or self.order or self.timing)


class LivetimingLiveSource:
    """A live session, straight from F1's own timing hub.

    The hub is a push feed: it sends whatever changes.  This keeps the
    latest value of everything in a :class:`HubState` and hands it over as a
    :class:`LiveSnapshot`, the same shape the OpenF1 live source produces, so
    the rest of the pit wall does not care which feed it is watching.
    """

    def __init__(
        self,
        *,
        topics: tuple[str, ...] = TOPICS,
        reconnect_delay: float = 15.0,
    ) -> None:
        self.topics = tuple(topics)
        self.reconnect_delay = reconnect_delay
        self.state = HubState()
        self.last_error: str | None = None
        self.connected = False
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._attempted_at: float = 0.0
        self._backoff: float = reconnect_delay

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    async def _negotiate(self) -> str:
        """Ask the hub for a connection token."""
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            response = await client.post(
                f"{HUB_BASE}/negotiate?negotiateVersion=1",
                content=b"[]",
                headers={**_HEADERS, "Content-Type": "application/json"},
            )
            response.raise_for_status()
            return str(response.json()["connectionToken"])

    async def connect(self, *, min_interval: float = 30.0) -> bool:
        """Open the websocket and subscribe.  Returns whether it worked.

        The hub **throttles**: measured on 2026-10-22, six or seven
        negotiate-and-connect cycles in quick succession are answered, and
        after that the websocket upgrade answers **HTTP 404** until the
        limit clears.  The 404 therefore means "too many connections", not
        "wrong address", and the client backs off rather than retrying.
        """
        now = time.monotonic()
        if now - self._attempted_at < min_interval and not self.connected:
            return False
        self._attempted_at = now

        try:
            token = await self._negotiate()
        except Exception as exc:  # noqa: BLE001 - the watcher must survive
            self.last_error = f"negotiate failed: {exc}"
            logger.warning("livetiming: %s", self.last_error)
            return False

        url = f"{WS_BASE}?id={quote(token, safe='')}"
        try:
            self._ws = await websockets.connect(
                url, additional_headers={"Origin": "https://www.formula1.com"}
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"websocket refused: {exc}"
            logger.warning("livetiming: %s", self.last_error)
            return False

        await self._ws.send(json.dumps({"protocol": "json", "version": 1}) + SEPARATOR)
        await self._ws.recv()  # the handshake response
        await self._ws.send(
            json.dumps(
                {"type": 1, "target": "Subscribe", "arguments": list(self.topics)}
            )
            + SEPARATOR
        )
        self.connected = True
        self.last_error = None
        self._backoff = self.reconnect_delay
        self._stop.clear()
        self._reader = asyncio.create_task(self._read())
        logger.info("livetiming: connected and subscribed to %d topics", len(self.topics))
        return True

    async def aclose(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        self.connected = False

    async def _read(self) -> None:
        """Read frames until the connection drops, then reconnect.

        The hub throttles connections, so the wait between attempts grows
        each time one fails.
        """
        while not self._stop.is_set():
            try:
                async for raw in self._ws:
                    if self._stop.is_set():
                        break
                    for frame in raw.split(SEPARATOR):
                        if frame:
                            self._handle(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"stream ended: {exc}"
                logger.warning("livetiming: %s", self.last_error)
            self.connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 300.0)
            await self.connect(min_interval=0.0)

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------
    def _handle(self, frame: str) -> None:
        try:
            message = json.loads(frame)
        except ValueError:
            return
        if message.get("type") != 1:
            return
        topic = message.get("target") or ""
        arguments = message.get("arguments") or []
        if not arguments:
            return
        payload = arguments[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return
        if not isinstance(payload, dict):
            return
        # "CarData.z" -> "_on_CarData"
        name = topic[:-2] if topic.endswith(".z") else topic
        handler = getattr(self, f"_on_{name}", None)
        if handler is None:
            return
        try:
            handler(payload)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not kill it
            logger.debug("livetiming: %s failed: %s", topic, exc)

    def _on_LiveTimingData(self, payload: dict[str, Any]) -> None:
        """The full snapshot the hub sends when you subscribe."""
        self._on_SessionInfo(payload.get("SessionInfo") or {})
        self._on_DriverList(payload.get("DriverList") or {})
        self._on_CarData(payload.get("CarData") or {})
        self._on_Position(payload.get("Position") or {})
        self._on_TimingData(payload.get("TimingData") or {})
        self._on_WeatherData(payload.get("WeatherData") or {})
        self._on_TeamRadio(payload.get("TeamRadio") or {})
        self._on_RaceControlMessages(payload.get("RaceControlMessages") or {})

    def _on_SessionInfo(self, payload: dict[str, Any]) -> None:
        if payload:
            self.state.session = payload

    def _on_SessionStatus(self, payload: dict[str, Any]) -> None:
        if payload:
            self.state.session.update(payload)

    def _on_DriverList(self, payload: dict[str, Any]) -> None:
        for row in payload.get("Entries") or []:
            number = row.get("Number")
            if number is None:
                continue
            self.state.drivers[int(number)] = {
                "driver_number": int(number),
                "code": row.get("Code"),
                "name": row.get("FullName"),
                "broadcast_name": row.get("BroadcastName"),
                "team": row.get("Team"),
                "team_colour": _colour(row.get("TeamColour")),
                "country": row.get("CountryName"),
                "number": row.get("RacingNumber"),
            }

    def _on_CarData(self, payload: dict[str, Any]) -> None:
        for key, row in (payload.get("Entries") or {}).items():
            number = row.get("Number", key)
            try:
                number = int(number)
            except (TypeError, ValueError):
                continue
            meta = (row.get("Meta") or {}).get("Driver") or {}
            existing = self.state.cars.get(number, {})
            existing.update(
                {
                    "driver_number": number,
                    "x": _number(row.get("X")),
                    "y": _number(row.get("Y")),
                    "z": _number(row.get("Z")),
                    "speed": _number(row.get("Speed")),
                    "throttle": _number(row.get("Throttle")),
                    "brake": _number(row.get("Brake")),
                    "n_gear": int(row.get("Gear") or 0),
                    "rpm": _number(row.get("RPM")),
                    "drs": int(row.get("DRS") or 0),
                    "compound": row.get("Compound"),
                    "tyre_age": row.get("TyreLife"),
                    "lap": row.get("Lap"),
                    "total_laps": row.get("TotalLaps"),
                    "position": row.get("Position"),
                    "status": row.get("Status"),
                    "code": meta.get("Code") or existing.get("code"),
                    "name": meta.get("FullName") or existing.get("name"),
                    "team": meta.get("Team") or existing.get("team"),
                    "team_colour": _colour(meta.get("TeamColour") or existing.get("team_colour")),
                }
            )
            self.state.cars[number] = existing

    def _on_Position(self, payload: dict[str, Any]) -> None:
        for key, row in (payload.get("Entries") or {}).items():
            try:
                number = int(row.get("Number", key))
                self.state.order[number] = int(row.get("Position"))
            except (TypeError, ValueError):
                continue

    def _on_TimingData(self, payload: dict[str, Any]) -> None:
        for key, row in (payload.get("Lines") or {}).items():
            try:
                number = int(row.get("Number", key))
            except (TypeError, ValueError):
                continue
            entry = self.state.timing.setdefault(number, {})
            entry.update(
                {
                    "driver_number": number,
                    "number_of_laps": row.get("NumberOfLaps"),
                    "position": row.get("Position"),
                    "retired": row.get("Retired"),
                    "stopped": row.get("Stopped"),
                    "status": row.get("Status"),
                    "last_lap": (row.get("LastLap") or {}).get("Value"),
                    "best_lap": (row.get("BestLap") or {}).get("Value"),
                    "interval_to_ahead": (row.get("IntervalToAhead") or {}).get("Value"),
                    "interval_to_leader": (row.get("IntervalToLeader") or {}).get("Value"),
                    "pit_stop": (row.get("PitStop") or {}).get("PitStop"),
                }
            )

    def _on_WeatherData(self, payload: dict[str, Any]) -> None:
        for key, row in (payload.get("Reports") or {}).items():
            self.state.weather.append(
                {
                    "date": row.get("TimeStamp"),
                    "session_key": row.get("SessionKey"),
                    "air_temperature": row.get("AirTemp"),
                    "track_temperature": row.get("TrackTemp"),
                    "humidity": row.get("Humidity"),
                    "pressure": row.get("Pressure"),
                    "wind_speed": row.get("WindSpeed"),
                    "wind_direction": row.get("WindDirection"),
                    "rainfall": row.get("Rainfall"),
                    "track_status": row.get("TrackStatus"),
                }
            )
            self.state.weather.sort(key=lambda r: str(r.get("date") or ""))

    def _on_TeamRadio(self, payload: dict[str, Any]) -> None:
        """F1 sends the radio **as text** - no transcription needed."""
        for key, row in (payload.get("Captions") or {}).items():
            captions = row.get("Captions") or ""
            if not captions:
                continue
            stamp, _, text = captions.partition(": ")
            driver_number = None
            try:
                driver_number = int(str(key).split("-")[0])
            except (TypeError, ValueError):
                driver_number = None
            self.state.radio.append(
                {
                    "date": row.get("TimeStamp") or str(key).split("-", 1)[-1],
                    "driver_number": driver_number,
                    "who": stamp,
                    "text": text or captions,
                }
            )
            self.state.radio.sort(key=lambda r: str(r.get("date") or ""))

    def _on_RaceControlMessages(self, payload: dict[str, Any]) -> None:
        for key, row in (payload.get("Messages") or {}).items():
            self.state.messages.append(
                {
                    "date": row.get("TimeStamp"),
                    "message": row.get("Message"),
                    "category": row.get("Category"),
                    "flag": row.get("Flag"),
                    "lap_number": row.get("Lap"),
                    "scope": row.get("Scope"),
                    "sector": row.get("Sector"),
                    "status": row.get("Status"),
                }
            )
            self.state.messages.sort(key=lambda r: str(r.get("date") or ""))

    # ------------------------------------------------------------------
    # the snapshot the pit wall consumes
    # ------------------------------------------------------------------
    def snapshot(self) -> LiveSnapshot:
        """The current state, in the shape the pit wall expects."""
        positions: list[dict[str, Any]] = []
        telemetry: list[dict[str, Any]] = []
        intervals: list[dict[str, Any]] = []
        order: list[dict[str, Any]] = []

        for number, car in sorted(self.state.cars.items()):
            positions.append(
                {
                    "driver_number": number,
                    "x": car.get("x"),
                    "y": car.get("y"),
                    "z": car.get("z"),
                    "date": datetime.now(UTC).isoformat(),
                }
            )
            telemetry.append(
                {
                    "driver_number": number,
                    "speed": car.get("speed"),
                    "throttle": car.get("throttle"),
                    "brake": car.get("brake"),
                    "n_gear": car.get("n_gear"),
                    "rpm": car.get("rpm"),
                    "drs": car.get("drs"),
                }
            )

        for number, timing in sorted(self.state.timing.items()):
            intervals.append(
                {
                    "driver_number": number,
                    "interval": timing.get("interval_to_ahead"),
                    "gap_to_leader": timing.get("interval_to_leader"),
                    "date": datetime.now(UTC).isoformat(),
                }
            )

        for number, position in sorted(
            self.state.order.items(), key=lambda kv: kv[1]
        ):
            order.append(
                {
                    "driver_number": number,
                    "position": position,
                    "date": datetime.now(UTC).isoformat(),
                }
            )

        return LiveSnapshot(
            timestamp=datetime.now(UTC),
            positions=positions,
            telemetry=telemetry,
            intervals=intervals,
            order=order,
            source="livetiming",
        )


class LivetimingLiveAdapter:
    """Serves the tick loop from F1's own live timing hub.

    It presents the same surface as
    :class:`~pitwall.realtime.live.OpenF1LiveAdapter`, so the pit wall does
    not care whether it is watching a recording, a paid OpenF1 live feed or
    the official hub.
    """

    def __init__(self, source: LivetimingLiveSource | None = None) -> None:
        self.source = source or LivetimingLiveSource()
        self.last_error: str | None = None
        self._snapshot: LiveSnapshot | None = None

    @property
    def session_key(self) -> int:
        """The hub's session key, when it has told us."""
        try:
            return int(self.source.state.session.get("Key") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def circuit(self) -> str:
        meeting = self.source.state.session.get("Meeting") or {}
        return str(
            meeting.get("OfficialName")
            or meeting.get("Name")
            or self.source.state.session.get("Name")
            or "Live"
        )

    @property
    def year(self) -> int:
        meeting = self.source.state.session.get("Meeting") or {}
        try:
            return int(meeting.get("Year") or 0)
        except (TypeError, ValueError):
            return 0

    async def refresh(self) -> bool:
        """Make sure the hub is connected and hand over the latest state."""
        if not self.source.connected:
            await self.source.connect()
        snapshot = self.source.snapshot()
        self._snapshot = snapshot
        if not snapshot.positions and not snapshot.telemetry:
            self.last_error = self.source.last_error or "no live data yet"
            return False
        self.last_error = None
        return True

    # -- the replay-compatible surface ---------------------------------
    def _flag_state(self) -> FlagState:
        """The track state, from the race-control messages."""
        state = FlagState.GREEN
        for row in self.source.state.messages:
            flag = (row.get("flag") or "").strip().upper()
            message = (row.get("message") or "").strip().upper()
            if "DOUBLE YELLOW" in flag or "DOUBLE YELLOW" in message:
                state = FlagState.YELLOW
            elif flag == "YELLOW" or "YELLOW IN TRACK SECTOR" in message:
                state = FlagState.YELLOW
            elif flag in ("VSC",) or "VIRTUAL SAFETY CAR" in message:
                state = FlagState.VSC
            elif flag in ("SAFETY CAR", "SC"):
                state = FlagState.SAFETY_CAR
            elif flag == "RED" or "RED FLAG" in message:
                state = FlagState.RED
            elif flag == "GREEN" or message.startswith("TRACK CLEAR"):
                state = FlagState.GREEN
        return state

    def frame(self, replay_speed: float = 1.0) -> Any:
        """Build a race frame from the hub's latest state."""
        from pitwall.realtime.session_replay import DriverFrame, RaceFrame

        state = self.source.state
        drivers: list[DriverFrame] = []
        for number, car in sorted(state.cars.items()):
            meta = state.drivers.get(number, {})
            timing = state.timing.get(number, {})
            drivers.append(
                DriverFrame(
                    driver_number=number,
                    code=str(meta.get("code") or f"D{number}"),
                    name=str(meta.get("name") or ""),
                    team=str(meta.get("team") or ""),
                    team_colour=_colour(meta.get("team_colour")),
                    position=state.order.get(number) or car.get("position"),
                    x=car.get("x"),
                    y=car.get("y"),
                    z=car.get("z"),
                    speed=car.get("speed"),
                    throttle=car.get("throttle"),
                    brake=car.get("brake"),
                    gear=car.get("n_gear"),
                    rpm=car.get("rpm"),
                    drs=car.get("drs"),
                    drs_open=bool(car.get("drs") and int(car["drs"]) > 9),
                    gap_to_leader=timing.get("interval_to_leader"),
                    interval=timing.get("interval_to_ahead"),
                    compound=car.get("compound"),
                    tyre_age=car.get("tyre_age"),
                    lap=car.get("lap"),
                    last_lap_s=timing.get("last_lap"),
                    on_track=car.get("x") is not None,
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
            source="livetiming.live",
            replay_speed=replay_speed,
        )

    def recent_messages(self, limit: int = 8) -> list[dict[str, Any]]:
        """The most recent race-control messages."""
        out: list[dict[str, Any]] = []
        for row in self.source.state.messages:
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

    def recent_radio(self, limit: int = 20) -> list[dict[str, Any]]:
        """The most recent team radio calls, with F1's own captions."""
        return self.source.state.radio[-limit:]

    def weather(self) -> list[dict[str, Any]]:
        """The weather reports the hub has sent."""
        return self.source.state.weather

