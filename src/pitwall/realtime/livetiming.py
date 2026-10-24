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
import base64
import json
import logging
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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

#: The archive the radio MP3s live on.  The JSON archive is gated, the audio
#: is not, so a live call can be played from the browser with no proxy.
STATIC_BASE = "https://livetiming.formula1.com/static"

#: The record separator of the SignalR JSON protocol.
SEPARATOR = "\x1e"

#: The hub the F1 timing data lives on.
HUB_NAME = "Streaming"

#: The topics a pit wall needs.
#:
#: Only **two** of the feed's topics are compressed (``CarData.z`` and
#: ``Position.z``); every other topic is sent as plain JSON, and asking for
#: ``SessionInfo.z`` or ``TimingData.z`` subscribes to a topic that does not
#: exist, which silently delivers nothing at all.  The list below is pinned
#: against FastF1's reference client (``fastf1/livetiming/client.py``).
TOPICS = (
    "Heartbeat",
    "SessionInfo",
    "SessionStatus",
    "DriverList",
    "TimingData",
    "TimingAppData",
    "TrackStatus",
    "LapCount",
    "WeatherData",
    "TeamRadio",
    "RaceControlMessages",
    "CarData.z",
    "Position.z",
)

#: How often the client pings the hub.  SignalR drops a connection that has
#: been silent for 30 s, so without this the socket dies between sessions.
PING_INTERVAL_S = 15.0

#: F1 reports the team colour as a bare hex string, without the "#".
DEFAULT_TEAM_COLOUR = "#71717a"

#: The telemetry channels inside ``CarData.z``, keyed by channel number.
#: Pinned against FastF1's parser, which reads the same five indices.
CAR_CHANNELS = {
    "rpm": "0",
    "speed": "2",
    "gear": "3",
    "throttle": "4",
    "brake": "5",
    "drs": "45",
}

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


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds(value: Any) -> float | None:
    """A lap time or gap as seconds.

    The hub writes times as ``"1:39.699"`` and gaps as ``"+1.234"``, while the
    replay's pack stores plain seconds.  Both arrive at the same widgets, so
    both are normalised here rather than at the point of display.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if ":" in text:
            minutes, _, rest = text.partition(":")
            return float(minutes) * 60.0 + float(rest)
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _radio_url(path: Any) -> str | None:
    """The playable address of a radio clip.

    The hub sends a path relative to its static archive (for example
    ``2026/.../TeamRadio/VER01_1_...mp3``); the browser can play that
    directly.  A path that is already absolute is left alone.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    path = path.strip()
    if path.startswith(("http://", "https://")):
        return path
    path = path.lstrip("/")
    if path.startswith("static/"):
        path = path[len("static/") :]
    return f"{STATIC_BASE}/{path}"


#: Session statuses that mean the session is no longer running.
ENDED_STATUSES = frozenset({"finalised", "finalized", "ended", "aborted", "cancelled"})

#: Session statuses that mean the session is running now.
RUNNING_STATUSES = frozenset({"started", "inprogress", "in progress"})


def session_is_running(session: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the hub is describing a session that is running now.

    The hub keeps the **last** session in its snapshot after it has finished,
    so the presence of a session key proves nothing on its own: without this
    check a finished practice would be presented as live for ever, with no
    cars on track and nothing updating.
    """
    if not session or not session.get("Key"):
        return False
    status = session.get("SessionStatus") or {}
    value = str(status.get("Status") or "").strip().lower() if isinstance(status, dict) else ""
    if value in RUNNING_STATUSES:
        return True
    if value in ENDED_STATUSES:
        return False
    # No usable status: fall back on the published window.  The margin lets a
    # session that starts early still count, and a race that overruns still
    # count while it is being run.
    moment = now or datetime.now(UTC)
    start = _parse_utc(session.get("StartDate"))
    end = _parse_utc(session.get("EndDate"))
    if start is None or end is None:
        return False
    return start - timedelta(minutes=15) <= moment <= end + timedelta(minutes=45)


def _decode(payload: Any) -> Any:
    """Decode one hub payload.

    Two wire formats arrive on this feed:

    * most topics send a **JSON string**, which is either the whole object or
      a quoted JSON string,
    * ``CarData.z`` and ``Position.z`` send **base64 of a raw-deflate stream**.

    Both are pinned against FastF1's own parser, which does
    ``zlib.decompress(base64.b64decode(text), -zlib.MAX_WBITS)`` for the
    ``.z`` topics.  Decoding is attempted by shape rather than by topic name,
    so a topic that changes format still parses.
    """
    if isinstance(payload, (dict, list)):
        return payload
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except ValueError:
            return None
    compact = "".join(text.split())
    try:
        raw = base64.b64decode(compact)
    except Exception:  # noqa: BLE001 - not a compressed payload
        return None
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS):
        try:
            decoded = zlib.decompress(raw, wbits)
        except zlib.error:
            continue
        try:
            return json.loads(decoded.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return None
    return None


@dataclass
class HubState:
    """Everything the hub has told us so far."""

    session: dict[str, Any] = field(default_factory=dict)
    drivers: dict[int, dict[str, Any]] = field(default_factory=dict)
    cars: dict[int, dict[str, Any]] = field(default_factory=dict)
    order: dict[int, int] = field(default_factory=dict)
    timing: dict[int, dict[str, Any]] = field(default_factory=dict)
    tyres: dict[int, dict[str, Any]] = field(default_factory=dict)
    weather: list[dict[str, Any]] = field(default_factory=list)
    radio: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: ``LapCount``, e.g. ``{"CurrentLap": 12, "TotalLaps": 56}``.
    lap_count: dict[str, Any] = field(default_factory=dict)
    #: ``ExtrapolatedClock``, e.g. ``{"Remaining": "1:12:30", "Extrapolating": True}``.
    clock: dict[str, Any] = field(default_factory=dict)
    #: ``TrackStatus``, e.g. ``{"Status": "1", "Message": "AllClear"}``.
    track_status: dict[str, Any] = field(default_factory=dict)
    #: How many frames of each topic have been parsed, for diagnosis.
    topics: dict[str, int] = field(default_factory=dict)
    #: When the last frame of any topic arrived.
    last_frame_at: datetime | None = None
    #: Every JSON frame received, whatever its type - including handshake and
    #: ping traffic.  Zero means the socket is open but deaf.
    frames_received: int = 0
    #: Frames that could not be decoded - the honest measure of a broken feed.
    bad_frames: int = 0

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
        #: Whether the hub accepted the subscription.  A socket can be open
        #: and still carry nothing, so this is reported separately.
        self.subscribed = False
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._pinger: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._attempted_at: float = 0.0
        self._backoff: float = reconnect_delay
        #: Per-driver position history, for the live track outline.
        self._tracks: dict[int, list[tuple[float, float, float]]] = {}
        #: min_x, max_x, min_y, max_y of every position ever seen.
        self._extent: list[float] | None = None
        self._position_count = 0

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
        await self._ws.send(self._subscribe_frame())
        self.connected = True
        self.subscribed = False
        self.last_error = None
        self._backoff = self.reconnect_delay
        self._stop.clear()
        self._reader = asyncio.create_task(self._read())
        if self._pinger is not None and not self._pinger.done():
            self._pinger.cancel()
        self._pinger = asyncio.create_task(self._ping())
        logger.info("livetiming: connected and subscribed to %d topics", len(self.topics))
        return True

    def _subscribe_frame(self) -> str:
        """The ``Subscribe`` invocation, in SignalR's argument-array form.

        ``Subscribe`` takes **one** argument - the list of topics - so the
        arguments must be nested: ``[[...]]``.  Sending the topics flat passes
        the first topic as a bare string, and the hub answers with an error
        completion that is easy to miss: the socket still looks connected, but
        nothing ever arrives.

        The ``invocationId`` matters too.  Without it SignalR treats the call
        as a fire-and-forget *send*, so the hub never returns the completion
        whose result is the whole session snapshot - the first ``SessionInfo``,
        ``DriverList`` and positions would all be lost.
        """
        return (
            json.dumps(
                {
                    "type": 1,
                    "invocationId": "0",
                    "target": "Subscribe",
                    "arguments": [list(self.topics)],
                }
            )
            + SEPARATOR
        )

    async def _ping(self) -> None:
        """Keep the socket alive.

        SignalR drops a connection that has sent nothing for 30 s.  Between
        sessions the hub has nothing to say, so without this the socket is
        closed, the client reconnects, and the reconnect budget is spent
        before the session even starts - which is exactly how a live session
        ends up never being seen.
        """
        ping = json.dumps({"type": 6}) + SEPARATOR
        while not self._stop.is_set():
            try:
                await asyncio.sleep(PING_INTERVAL_S)
                if self._ws is None:
                    return
                await self._ws.send(ping)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the reader handles a dead socket
                return

    async def aclose(self) -> None:
        self._stop.set()
        for task in (self._pinger, self._reader):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._pinger = None
        self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        self.connected = False
        self.subscribed = False

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
            self.state.bad_frames += 1
            return
        self.state.frames_received += 1
        kind = message.get("type")
        # Type 3 is the completion of our Subscribe call: its result *is* the
        # full live-timing snapshot, topic by topic.  It is the only place the
        # current session state is ever handed over, so ignoring it loses
        # SessionInfo, DriverList and the first positions entirely.
        if kind == 3:
            if message.get("error"):
                self.state.bad_frames += 1
                self.last_error = f"subscribe failed: {message['error']}"
                logger.warning("livetiming: subscribe rejected: %s", message["error"])
                return
            self.subscribed = True
            result = message.get("result")
            if isinstance(result, dict):
                for topic, value in result.items():
                    self._dispatch(str(topic), _decode(value))
            return
        if kind != 1:
            return
        topic = message.get("target") or ""
        arguments = message.get("arguments") or []
        if not arguments:
            return
        self._dispatch(topic, _decode(arguments[0]))

    def _dispatch(self, topic: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            if payload is not None:
                self.state.bad_frames += 1
            return
        # "CarData.z" -> "_on_CarData"
        name = topic[:-2] if topic.endswith(".z") else topic
        handler = getattr(self, f"_on_{name}", None)
        if handler is None:
            return
        self.state.topics[topic] = self.state.topics.get(topic, 0) + 1
        self.state.last_frame_at = datetime.now(UTC)
        try:
            handler(payload)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not kill it
            self.state.bad_frames += 1
            logger.debug("livetiming: %s failed: %s", topic, exc)

    def _on_LiveTimingData(self, payload: dict[str, Any]) -> None:
        """A whole snapshot, if the hub ever sends one as a single topic."""
        for topic, value in payload.items():
            self._dispatch(str(topic), _decode(value))

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    def _on_SessionInfo(self, payload: dict[str, Any]) -> None:
        if payload:
            # The feed sends deltas: a field that is not in this frame is
            # unchanged, so the session is merged rather than replaced.
            before = self.state.session.get("Key")
            self.state.session.update(payload)
            after = self.state.session.get("Key")
            if after and after != before:
                logger.info(
                    "livetiming: session %s %s (%s)",
                    after,
                    self.state.session.get("Name"),
                    self.state.session.get("Type"),
                )

    def _on_SessionStatus(self, payload: dict[str, Any]) -> None:
        if payload:
            self.state.session.setdefault("SessionStatus", {})
            status = self.state.session["SessionStatus"]
            if isinstance(status, dict):
                status.update(payload)
            self.state.session.update({k: v for k, v in payload.items() if k != "Status"})

    def _on_TrackStatus(self, payload: dict[str, Any]) -> None:
        if payload:
            self.state.track_status = payload

    def _on_LapCount(self, payload: dict[str, Any]) -> None:
        if payload:
            self.state.lap_count.update(payload)

    def _on_ExtrapolatedClock(self, payload: dict[str, Any]) -> None:
        if payload:
            self.state.clock.update(payload)

    def _on_Heartbeat(self, payload: dict[str, Any]) -> None:
        """The heartbeat carries the hub's clock; nothing to store."""

    def _on_SessionData(self, payload: dict[str, Any]) -> None:
        """Series/status metadata; not needed for the pit wall."""

    # ------------------------------------------------------------------
    # drivers
    # ------------------------------------------------------------------
    def _on_DriverList(self, payload: dict[str, Any]) -> None:
        for number, row in (payload.get("Entries") or {}).items():
            try:
                number = int(row.get("RacingNumber") or number)
            except (TypeError, ValueError):
                continue
            existing = self.state.drivers.get(number, {})
            existing.update(
                {
                    "driver_number": number,
                    "code": row.get("Tla") or row.get("Code") or existing.get("code"),
                    "name": row.get("FullName") or existing.get("name"),
                    "broadcast_name": row.get("BroadcastName"),
                    "team": row.get("TeamName") or row.get("Team"),
                    "team_colour": _colour(
                        row.get("TeamColour") or existing.get("team_colour")
                    ),
                    "country": row.get("CountryCode"),
                    "number": row.get("RacingNumber"),
                }
            )
            self.state.drivers[number] = existing

    def _on_CarData(self, payload: dict[str, Any]) -> None:
        """``CarData.z``: telemetry channels, no position.

        The live shape is ``{"Entries": [{"Utc": ..., "Cars": {<drv>:
        {"Channels": {"0": rpm, "2": speed, ...}}}}]}`` - a *list* of samples,
        with the values under numbered channels rather than named fields.
        """
        entries = payload.get("Entries")
        samples = entries if isinstance(entries, list) else []
        if isinstance(entries, dict):
            samples = [{"Cars": entries}]
        for sample in samples:
            cars = (sample or {}).get("Cars") or {}
            for key, row in cars.items():
                try:
                    number = int(key)
                except (TypeError, ValueError):
                    continue
                channels = row.get("Channels") or {}
                if not isinstance(channels, dict):
                    continue
                car = self.state.cars.setdefault(number, {"driver_number": number})
                car.update(
                    {
                        "rpm": _number(channels.get(CAR_CHANNELS["rpm"])),
                        "speed": _number(channels.get(CAR_CHANNELS["speed"])),
                        "n_gear": _number(channels.get(CAR_CHANNELS["gear"])),
                        "throttle": _number(channels.get(CAR_CHANNELS["throttle"])),
                        "brake": _number(channels.get(CAR_CHANNELS["brake"])),
                        "drs": _number(channels.get(CAR_CHANNELS["drs"])),
                    }
                )

    def _on_Position(self, payload: dict[str, Any]) -> None:
        """``Position.z``: where the car is, in the feed's own coordinates.

        The live shape is ``{"Position": [{"Timestamp": ..., "Entries":
        {<drv>: {"Status": "OnTrack", "X": ..., "Y": ..., "Z": ...}}}]}``.
        """
        samples = payload.get("Position")
        if isinstance(samples, list):
            for sample in samples:
                self._store_positions((sample or {}).get("Entries") or {})
        entries = payload.get("Entries")
        if isinstance(entries, dict):
            self._store_positions(entries)

    def _store_positions(self, entries: dict[str, Any]) -> None:
        for key, row in entries.items():
            try:
                number = int(row.get("Number") or key)
            except (TypeError, ValueError):
                continue
            car = self.state.cars.setdefault(number, {"driver_number": number})
            x, y = _number(row.get("X")), _number(row.get("Y"))
            car["x"] = x
            car["y"] = y
            car["z"] = _number(row.get("Z"))
            if "Status" in row:
                status = row.get("Status")
                car["status"] = status
                car["on_track"] = str(status).strip().lower() == "ontrack"
            # A car in its garage is reported off-track; those samples would
            # stretch the map over the pit lane, so only running cars build
            # the outline.
            in_garage = car.get("status") is not None and car.get("on_track") is False
            usable = x is not None and y is not None and not in_garage
            if usable:
                history = self._tracks.setdefault(number, [])
                history.append((time.time(), x, y))
                if len(history) > 4000:
                    del history[:-3000]
                if self._extent is None:
                    self._extent = [x, x, y, y]
                else:
                    self._extent[0] = min(self._extent[0], x)
                    self._extent[1] = max(self._extent[1], x)
                    self._extent[2] = min(self._extent[2], y)
                    self._extent[3] = max(self._extent[3], y)
                self._position_count += 1

    def _on_TimingData(self, payload: dict[str, Any]) -> None:
        for key, row in (payload.get("Lines") or {}).items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            entry = self.state.timing.setdefault(number, {})
            entry.update(
                {
                    "driver_number": number,
                    "number_of_laps": _int(
                        row.get("NumberOfLaps", entry.get("number_of_laps"))
                    ),
                    "position": _int(row.get("Position", entry.get("position"))),
                    "retired": row.get("Retired", entry.get("retired")),
                    "stopped": row.get("Stopped", entry.get("stopped")),
                    "status": row.get("Status", entry.get("status")),
                    "last_lap": _seconds(
                        (row.get("LastLap") or {}).get("Value", entry.get("last_lap"))
                    ),
                    "best_lap": _seconds(
                        (row.get("BestLap") or {}).get("Value", entry.get("best_lap"))
                    ),
                    "interval_to_ahead": _seconds(
                        (row.get("IntervalToAhead") or {}).get(
                            "Value", entry.get("interval_to_ahead")
                        )
                    ),
                    "interval_to_leader": _seconds(
                        (row.get("IntervalToLeader") or {}).get(
                            "Value", entry.get("interval_to_leader")
                        )
                    ),
                    "pit_stop": (row.get("PitStop") or {}).get(
                        "PitStop", entry.get("pit_stop")
                    ),
                }
            )
            position = entry.get("position")
            try:
                if position is not None:
                    self.state.order[number] = int(position)
            except (TypeError, ValueError):
                pass

    def _on_TimingAppData(self, payload: dict[str, Any]) -> None:
        """``TimingAppData`` carries the tyres, which ``TimingData`` does not.

        The shape is ``{"Lines": {<drv>: {"Stints": {<n>: {"Compound":
        "SOFT", "TotalLaps": 7, "StartLaps": 0}}}}}``.
        """
        for key, row in (payload.get("Lines") or {}).items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            stints = row.get("Stints")
            if not isinstance(stints, dict) or not stints:
                continue

            def _order(item: tuple[str, Any]) -> float:
                try:
                    return float(item[0])
                except (TypeError, ValueError):
                    return -1.0

            latest = max(stints.items(), key=_order)[1] or {}
            entry = self.state.tyres.setdefault(number, {})
            if latest.get("Compound"):
                entry["compound"] = latest["Compound"]
            for field_name, key_name in (
                ("total_laps", "TotalLaps"),
                ("start_laps", "StartLaps"),
            ):
                if latest.get(key_name) is not None:
                    entry[field_name] = _int(latest[key_name])
            entry["new"] = latest.get("New")

    def _on_WeatherData(self, payload: dict[str, Any]) -> None:
        reports = payload.get("Reports")
        if isinstance(reports, dict):
            rows = list(reports.values())
        elif isinstance(reports, list):
            rows = reports
        else:
            rows = [payload]
        for row in rows:
            if not isinstance(row, dict):
                continue
            self.state.weather.append(
                {
                    "date": row.get("Utc") or row.get("TimeStamp"),
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
        del self.state.weather[:-600]

    def _on_TeamRadio(self, payload: dict[str, Any]) -> None:
        """F1 sends each radio call with its words and the clip's path.

        The caption is normally ``{"Utc": ..., "Captions": "1: ..."}`` under a
        ``"<driver>-<utc>"`` key, but the recording's metadata has also been
        seen as a JSON string inside the caption.  Both are accepted: the
        words and the MP3 path are what matter, and a call that cannot be
        attributed is still shown rather than dropped.
        """
        captions = payload.get("Captions")
        if isinstance(captions, list):
            items = {str(i): row for i, row in enumerate(captions)}
        elif isinstance(captions, dict):
            items = captions
        else:
            return
        known: dict[tuple[str, str], dict[str, Any]] = {
            (str(r.get("date") or ""), str(r.get("text") or "")): r
            for r in self.state.radio
        }
        for key, row in items.items():
            if isinstance(row, str):
                # Either the words themselves or the recording metadata.
                try:
                    parsed = json.loads(row)
                except ValueError:
                    parsed = {"Captions": row}
                row = parsed if isinstance(parsed, dict) else {"Captions": row}
            if not isinstance(row, dict):
                continue
            text = row.get("Captions") or row.get("Text") or ""
            if isinstance(text, str) and text.lstrip().startswith("{"):
                # The caption is the recording's metadata, with the words inside.
                try:
                    inner = json.loads(text)
                except ValueError:
                    inner = None
                if isinstance(inner, dict):
                    row = {**row, **inner}
                    text = row.get("Captions") or row.get("Text") or ""
            if not isinstance(text, str) or not text.strip():
                continue
            number = row.get("RacingNumber") or row.get("DriverNumber")
            try:
                driver_number = int(str(number)) if number is not None else None
            except (TypeError, ValueError):
                driver_number = None
            if driver_number is None:
                try:
                    driver_number = int(str(key).split("-")[0])
                except (TypeError, ValueError):
                    driver_number = None
            stamp = str(row.get("Utc") or key)
            who, _, spoken = text.partition(": ")
            spoken = spoken or text
            recording_url = _radio_url(row.get("Path"))
            seen = known.get((stamp, spoken))
            if seen is not None:
                # A later snapshot may carry the audio path the first one
                # lacked; keep the call and fill the path in.
                if not seen.get("recording_url") and recording_url:
                    seen["recording_url"] = recording_url
                continue
            entry = {
                "date": stamp,
                "driver_number": driver_number,
                "who": who if spoken != text else None,
                "text": spoken,
                "recording_url": recording_url,
            }
            known[(stamp, spoken)] = entry
            self.state.radio.append(entry)
        self.state.radio.sort(key=lambda r: str(r.get("date") or ""))
        del self.state.radio[:-400]

    def _on_RaceControlMessages(self, payload: dict[str, Any]) -> None:
        messages = payload.get("Messages")
        if isinstance(messages, list):
            items = {str(i): row for i, row in enumerate(messages)}
        elif isinstance(messages, dict):
            items = messages
        else:
            return
        for key, row in items.items():
            if not isinstance(row, dict):
                continue
            self.state.messages.append(
                {
                    "date": row.get("Utc") or row.get("TimeStamp") or key,
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
        del self.state.messages[:-200]

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------
    def bounds(self) -> tuple[float, float, float, float] | None:
        """The extent of every position seen so far, in the feed's own frame."""
        if self._extent is None or self._position_count < 3:
            return None
        return self._extent[0], self._extent[1], self._extent[2], self._extent[3]

    def trace(self, seconds: float = 150.0) -> list[tuple[float, float]]:
        """One car's driven path, newest last, as a stand-in track outline.

        F1's live feed gives positions in its **own** two-dimensional frame,
        not in GPS coordinates, so there is no published way to map them onto
        a GPS circuit outline.  The honest outline is therefore the path the
        cars themselves have driven: a real lap, traced by a real car, with
        nothing invented.

        The driver with the most recent samples is used, so the trace follows
        whoever is circulating most steadily rather than a car in the pits.
        """
        cutoff = time.time() - seconds
        best: list[tuple[float, float]] = []
        for samples in self._tracks.values():
            recent = [(t, x, y) for (t, x, y) in samples if t >= cutoff]
            if len(recent) > len(best):
                best = [(x, y) for (_, x, y) in recent]
        return best

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
            if car.get("x") is not None and car.get("y") is not None:
                positions.append(
                    {
                        "driver_number": number,
                        "x": car.get("x"),
                        "y": car.get("y"),
                        "z": car.get("z"),
                        "date": datetime.now(UTC).isoformat(),
                    }
                )
            if car.get("speed") is not None:
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
        """The circuit, as the calendar names it (``Austin``, ``Monza``...)."""
        meeting = self.source.state.session.get("Meeting") or {}
        circuit = meeting.get("Circuit") or {}
        return str(
            circuit.get("ShortName")
            or meeting.get("CircuitShortName")
            or meeting.get("Name")
            or self.source.state.session.get("Name")
            or "Live"
        )

    @property
    def session_name(self) -> str | None:
        name = self.source.state.session.get("Name")
        return str(name) if name else None

    @property
    def session_type(self) -> str | None:
        value = self.source.state.session.get("Type")
        return str(value) if value else None

    @property
    def total_laps(self) -> int | None:
        value = self.source.state.lap_count.get("TotalLaps")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def meta(self) -> dict[str, Any]:
        """Session metadata, in the shape the replay's pack uses."""
        session = self.source.state.session
        status = session.get("SessionStatus") or {}
        return {
            "session_name": self.session_name,
            "session_type": self.session_type,
            "date_start": session.get("StartDate"),
            "date_end": session.get("EndDate"),
            "status": status.get("Status") if isinstance(status, dict) else None,
        }

    @property
    def year(self) -> int:
        meeting = self.source.state.session.get("Meeting") or {}
        for value in (meeting.get("Year"), self.source.state.session.get("StartDate")):
            try:
                if value is None:
                    continue
                text = str(value)
                if len(text) >= 4 and text[:4].isdigit():
                    return int(text[:4])
                return int(value)
            except (TypeError, ValueError):
                continue
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
        # Everyone the feed has named, not just the cars that have sent a
        # position yet: at the start of a session the driver list arrives
        # before the first telemetry, and the tower should not be empty.
        numbers = sorted(set(state.cars) | set(state.timing) | set(state.drivers))
        for number in numbers:
            car = state.cars.get(number, {})
            meta = state.drivers.get(number, {})
            timing = state.timing.get(number, {})
            tyre = state.tyres.get(number, {})
            tyres_total = tyre.get("total_laps")
            position = _int(state.order.get(number) or timing.get("position"))
            drivers.append(
                DriverFrame(
                    driver_number=number,
                    code=str(meta.get("code") or f"D{number}"),
                    name=str(meta.get("name") or ""),
                    team=str(meta.get("team") or ""),
                    team_colour=_colour(meta.get("team_colour")),
                    position=position,
                    x=car.get("x"),
                    y=car.get("y"),
                    z=car.get("z"),
                    speed=car.get("speed"),
                    throttle=car.get("throttle"),
                    brake=car.get("brake"),
                    gear=car.get("n_gear"),
                    rpm=car.get("rpm"),
                    drs=car.get("drs"),
                    drs_open=bool(car.get("drs") and float(car["drs"]) > 9),
                    gap_to_leader=timing.get("interval_to_leader"),
                    interval=timing.get("interval_to_ahead"),
                    compound=tyre.get("compound"),
                    tyre_age=tyres_total,
                    lap=timing.get("number_of_laps"),
                    last_lap_s=timing.get("last_lap"),
                    on_track=bool(car.get("on_track", car.get("x") is not None)),
                )
            )

        drivers.sort(key=lambda d: (d.position if d.position else 999, d.code))
        lap = state.lap_count.get("CurrentLap")
        if lap is None:
            lap = max((d.lap or 0 for d in drivers), default=0) or None
        try:
            lap = int(lap) if lap is not None else None
        except (TypeError, ValueError):
            lap = None
        return RaceFrame(
            session_key=self.session_key,
            circuit=self.circuit,
            year=self.year,
            timestamp=datetime.now(UTC),
            elapsed_s=self._elapsed_s(),
            flag_state=self._flag_state(),
            drivers=drivers,
            source="livetiming.live",
            replay_speed=replay_speed,
            lap=lap,
            total_laps=self.total_laps,
            metadata=self.meta,
        )

    def _elapsed_s(self) -> float:
        """How long the session has been running, from the hub's own clock."""
        session = self.source.state.session
        status = session.get("SessionStatus") or {}
        start = status.get("Started") if isinstance(status, dict) else None
        start = start or session.get("StartDate")
        if not start:
            return 0.0
        try:
            started = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - started).total_seconds())

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

