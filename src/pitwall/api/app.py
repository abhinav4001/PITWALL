"""PitWall HTTP API.

Every endpoint either serves real data (from OpenF1 or from the local replay
cache) or returns an explicit ``{"available": false, "reason": ...}`` payload.
No endpoint invents values.

The "live" view is backed by :mod:`pitwall.realtime.session_replay`, which
replays a *real* recorded session (the 2024 United States Grand Prix at Austin
by default) so the app behaves like a live race on a day with no race running.
When a session really is live the same endpoints are served from
:mod:`pitwall.realtime.live`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pitwall.data.openf1 import OpenF1Client
from pitwall.data.radio import (
    RadioMessage,
    load_cached_radio,
    radio_message_from_row,
    save_radio,
    transcribe_all,
)
from pitwall.data.session_pack import DEFAULT_DIR, SessionPack, download_session
from pitwall.data.track import (
    TrackProjection,
    load_outline,
    resolve_outline,
)
from pitwall.engine.calibrate import fit_calibration
from pitwall.engine.calibration import load_calibration, save_calibration
from pitwall.engine.championship import (
    get_2026_constructor_standings,
    get_2026_driver_standings,
    get_championship_scenarios,
)
from pitwall.realtime.live import (
    OpenF1LiveAdapter,
    OpenF1LiveSource,
    find_live_session,
    latest_per_driver,
)
from pitwall.realtime.livetiming import (
    LivetimingLiveAdapter,
    _parse_utc,
    session_is_running,
)
from pitwall.realtime.session_replay import (
    SessionReplayer,
    project_frame,
)
from pitwall.realtime.tick_loop import TickLoop

logger = logging.getLogger(__name__)

#: The replay session shown when no race is live.  Session 9617 is the 2024
#: United States Grand Prix race at Austin (56 laps, 308.405 km).
DEFAULT_REPLAY_SESSION = 9617

#: Directories are resolved against the repository root, not the process
#: working directory, so the API works no matter where uvicorn is started.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTLINE_DIR = PROJECT_ROOT / "data" / "track_outlines"
SESSION_DIR = Path(DEFAULT_DIR)
if not SESSION_DIR.is_absolute():
    SESSION_DIR = PROJECT_ROOT / SESSION_DIR

@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start the OpenF1 client and the replay engine, and clean up after."""
    await state.startup()
    watcher = asyncio.create_task(state.watch_for_live())
    try:
        yield
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await state.shutdown()


app = FastAPI(title="PitWall API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    """The honest response shape used whenever real data is missing."""
    return {"available": False, "reason": reason, "source": "unavailable", **extra}


def _optional(source: Any, name: str, *args: Any) -> Any:
    """Call a helper the source may not have.

    The live adapter deliberately lacks the replay-only helpers, so every
    lookup of one goes through here instead of assuming they all exist.
    """
    method = getattr(source, name, None)
    if method is None:
        return None
    try:
        return method(*args)
    except Exception:  # noqa: BLE001 - an optional helper must not 500
        return None


def _recent_messages(limit: int = 12) -> list[dict[str, Any]]:
    """Race-control messages from whichever source is running."""
    if state.replayer is None:
        return []
    method = getattr(state.replayer, "recent_messages", None)
    if method is None:
        return []
    clock = getattr(state.replayer, "clock", None)
    try:
        if clock is not None:
            return method(clock.position_ns, limit=limit)
        return method(limit=limit)
    except Exception:  # noqa: BLE001 - messages are a nicety
        return []


def _weather_summary() -> dict[str, Any]:
    """The weather for whichever source is running.

    The replay has a summary of its own; a live session is summarised from
    the hub's own weather reports, in the same shape, so the card does not
    change between the two.
    """
    if state.replayer is None:
        return _unavailable("no_replay_session_cached")
    summary = _optional(state.replayer, "weather_summary")
    if isinstance(summary, dict):
        return summary
    readings = _optional(state.replayer, "weather") or []
    if not readings:
        return _unavailable(
            "no_weather_feed",
            reason_detail="The live timing feed has published no weather yet.",
        )
    latest = readings[-1]
    raining = bool(latest.get("rainfall"))
    return {
        "available": True,
        "source": "livetiming.weather",
        "rain_risk_percent": None,
        "condition": "wet" if raining else "clear",
        "clear_reports": 0,
        "slippery_reports": 0,
        "readings": readings,
        "current": {
            "date": latest.get("date"),
            "air_temperature": latest.get("air_temperature"),
            "track_temperature": latest.get("track_temperature"),
            "humidity": latest.get("humidity"),
            "pressure": latest.get("pressure"),
            "wind_speed": latest.get("wind_speed"),
            "wind_direction": latest.get("wind_direction"),
            "rainfall": latest.get("rainfall"),
        },
    }


# ----------------------------------------------------------------------
# shared state
# ----------------------------------------------------------------------
class PitWallState:
    """Holds the openf1 client, the replay engine and the track projection."""

    def __init__(self) -> None:
        self.client: OpenF1Client | None = None
        self.replayer: SessionReplayer | None = None
        self.projection: TrackProjection | None = None
        self.outline: dict[str, Any] | None = None
        self.circuit: str = "Austin"
        self.year: int = 2024
        self.replay_speed: float = 1.0
        self.lock = asyncio.Lock()
        self.loop: TickLoop | None = None
        self.mode: str = "REPLAY"
        self.live_session: dict[str, Any] | None = None
        #: F1's own live timing hub.  Free and unauthenticated.
        self.livetiming: LivetimingLiveAdapter | None = None
        #: The background task that transcribes the radio calls.
        self._radio_task: asyncio.Task[None] | None = None
        self._bundle: Any = None
        #: Watcher ticks with an open, silent hub subscription.
        self._silent_checks = 0
        #: The next session due at this circuit, and when it was looked up.
        self._next_session_at: datetime | None = None
        self._next_session_checked: float = 0.0
        #: Consecutive failed hub connections, and when to try again.
        self._hub_failures = 0
        self._hub_next_try = 0.0
        #: The live track, traced by the cars themselves (live mode only).
        self._live_trace: list[tuple[float, float]] = []

    async def startup(self) -> None:
        self.client = OpenF1Client(min_interval=1.0)
        await self._load_replay(DEFAULT_REPLAY_SESSION)
        await self._check_for_live_session()
        asyncio.create_task(self._follow_latest_session())
        asyncio.create_task(self._live_geometry_loop())

    async def _live_geometry_loop(self) -> None:
        """Keep the live map in step with the positions the cars are sending.

        The outline is built from the cars' own path, so it has to be
        refreshed as they circulate - otherwise the map would still be
        showing the replay's circuit when the session starts.
        """
        while True:
            await asyncio.sleep(5)
            if self.mode != "LIVE":
                continue
            try:
                self._rebuild_live_geometry()
            except Exception as exc:  # noqa: BLE001 - the map must not crash
                logger.debug("live geometry refresh failed: %s", exc)

    async def _follow_latest_session(self) -> None:
        """Keep the replay on the most recent session of this circuit.

        The pit wall should not need a manual refresh each weekend: when a
        session at this circuit has finished and its data is available, the
        replay follows it.  The session pack is downloaded in the background,
        and the current one keeps playing until the new one is ready.
        """
        while True:
            await asyncio.sleep(300)
            if self.mode == "LIVE" or self.client is None:
                continue
            try:
                target = await self._latest_available_session()
            except Exception as exc:  # noqa: BLE001 - the watcher must survive
                logger.warning("session lookup failed: %s", exc)
                continue
            if target is None or target == self.replayer.session_key:
                continue
            logger.info("a newer session is available: %s", target)
            if await self._switch_replay(target):
                logger.info("now following session %s", target)

    async def _latest_available_session(self) -> int | None:
        """The newest session of this circuit whose data has been published.

        OpenF1 publishes a session's data shortly after it ends, so the
        candidates are tried newest first and the first one that actually
        answers wins.
        """
        if self.client is None:
            return None
        now = datetime.now(UTC)
        sessions = await self.client.get_sessions(circuit_short_name=self.circuit)
        finished = [
            session
            for session in sessions
            if session.get("date_end")
            and datetime.fromisoformat(session["date_end"]) < now
            and not session.get("is_cancelled")
        ]
        finished.sort(key=lambda session: session["date_end"], reverse=True)
        # A race carries the most content, so it is preferred over a
        # practice or qualifying session.
        races = [s for s in finished if s.get("session_type") == "Race"]
        others = [s for s in finished if s.get("session_type") != "Race"]
        for session in races + others:
            laps = await self.client.get_laps(
                int(session["session_key"]), driver_number=1
            )
            if laps:
                return int(session["session_key"])
        return None

    async def _switch_replay(self, session_key: int) -> bool:
        """Download a session pack, fit its calibration and replay it."""
        if self.client is None:
            return False
        try:
            await download_session(self.client, session_key, SESSION_DIR)
        except Exception as exc:  # noqa: BLE001 - keep the current replay
            logger.warning("could not download session %s: %s", session_key, exc)
            return False
        # Fit the physics for the new session so the strategy widgets keep
        # working on it; a failed fit only costs the strategy panel.
        try:
            bundle = fit_calibration(session_key, SESSION_DIR)
            save_calibration(bundle)
            logger.info("calibrated %s %s", bundle.circuit, bundle.year)
        except Exception as exc:  # noqa: BLE001
            logger.warning("calibration for session %s failed: %s", session_key, exc)
        return await self._load_replay(session_key)

    async def _check_for_live_session(self) -> bool:
        """Switch to the real session when a race is actually running.

        The pit wall decides this by itself.  Two feeds can carry the live
        timing:

        * **F1's own timing hub** (`/signalrcore`), which is free and
          unauthenticated.  This is tried first.
        * **OpenF1's live tier**, which costs EUR 9.90 a month.  This is the
          fallback, for the case where the hub cannot be reached.

        If neither has data, the replay stays in place and the reason is
        reported rather than pretending.
        """
        if await self._connect_livetiming():
            return True
        return await self._connect_openf1_live()

    def _hub_failed(self) -> None:
        """Remember a failed connection and wait longer before the next try.

        Measured on 2026-10-22: the hub answers 404 once its connection budget
        is spent, and every further attempt spends it again.  The wait grows
        to five minutes, which is the interval the hub itself uses.
        """
        self._hub_failures = min(self._hub_failures + 1, 5)
        delay = min(300.0, 15.0 * 2 ** (self._hub_failures - 1))
        self._hub_next_try = time.monotonic() + delay
        logger.info(
            "livetiming hub is not reachable: %s (trying again in %.0fs)",
            self.livetiming.last_error if self.livetiming is not None else "unknown",
            delay,
        )

    async def _connect_livetiming(self) -> bool:
        """Try F1's own timing hub."""
        if self.livetiming is None:
            self.livetiming = LivetimingLiveAdapter()
        source = self.livetiming.source
        if not source.connected:
            # The hub answers 404 while its connection budget is spent, and
            # each attempt spends it further.  Back off like the socket does
            # instead of asking once a minute for ever.
            if time.monotonic() < self._hub_next_try:
                return False
            try:
                await source.connect()
            except Exception as exc:  # noqa: BLE001 - never fail startup
                logger.warning("livetiming connect failed: %s", exc)
                self._hub_failed()
                return False

        if not source.connected:
            self._hub_failed()
            return False

        self._hub_failures = 0
        self._hub_next_try = 0.0

        # The hub stays connected whether or not a session is running, and it
        # keeps the last session in its snapshot afterwards, so the session it
        # reports is what decides whether we go live.
        session = source.state.session
        if not session_is_running(session):
            logger.info("livetiming hub is connected but no session is running")
            self.mode = "REPLAY"
            return False

        self.live_session = {
            "session_key": session.get("Key"),
            "session_name": session.get("Name"),
            "source": "livetiming",
        }
        self.mode = "LIVE"
        self.replayer = self.livetiming  # type: ignore[assignment]
        self.circuit = self.livetiming.circuit
        self.year = self.livetiming.year
        self._bundle = self._bundle_for(self.circuit, self.year)
        self._rebuild_live_geometry()
        # Rebuild the tick loop so it drives the live source, carrying the
        # open browsers across - otherwise the page keeps receiving the
        # replay's frames and never shows the session that is running.
        await self._replace_loop(self.livetiming)
        logger.info(
            "following the live session %s (%s) at %s on F1's timing hub",
            session.get("Key"),
            session.get("Name"),
            self.circuit,
        )
        return True

    def _bundle_for(self, circuit: str, year: int) -> Any:
        """The calibration for a circuit and season, if one has been fitted.

        Strategy numbers are only shown when a measured bundle exists for the
        session being followed; a different season gets no invented forecast.
        """
        try:
            return load_calibration(circuit, year)
        except FileNotFoundError:
            logger.info(
                "no calibration for %s %s: the strategy card stays unavailable",
                circuit,
                year,
            )
            return None

    async def _replace_loop(self, source: Any) -> None:
        """Point the tick loop at a new source, keeping browsers attached.

        The browser subscribes to one loop, so swapping the source without
        moving the subscribers across would leave the page showing the old
        feed for ever - which is exactly how the 2024 replay stayed on screen
        through a live practice session.
        """
        old = self.loop
        was_running = old is not None and old.is_running
        new = TickLoop(source, self.projection, self._bundle)
        new.speed = self.replay_speed
        if old is not None:
            new.take_clients_from(old)
        self.loop = new
        if old is not None:
            await old.stop()
        if was_running:
            new.start()

    def _rebuild_live_geometry(self) -> None:
        """Point the map at the live positions.

        F1's live feed reports X/Y in **its own** two-dimensional frame, not
        in GPS coordinates, so the replay's GPS outline cannot be reused -
        the cars would be drawn in the wrong place.  The outline is therefore
        the path the cars have actually driven this session, which is a real
        lap traced by a real car with nothing invented.  It fills in as the
        session runs.
        """
        if self.livetiming is None:
            return
        source = self.livetiming.source
        bounds = source.bounds()
        trace = source.trace()
        if bounds is None or len(trace) < 10:
            self._live_trace = []
            return
        self._live_trace = trace
        min_x, max_x, min_y, max_y = bounds
        pad_x = max((max_x - min_x) * 0.03, 1.0)
        pad_y = max((max_y - min_y) * 0.03, 1.0)
        lo_x, hi_x = min_x - pad_x, max_x + pad_x
        lo_y, hi_y = min_y - pad_y, max_y + pad_y

        # Keep the map still unless the cars have driven outside it: re-scaling
        # on every tick would make the whole circuit drift under the dots.
        current = self.projection
        if (
            current is not None
            and current.min_x <= lo_x
            and current.max_x >= hi_x
            and current.min_y <= lo_y
            and current.max_y >= hi_y
        ):
            return
        self.projection = TrackProjection(
            min_x=lo_x, max_x=hi_x, min_y=lo_y, max_y=hi_y
        )
        # The tick loop holds its own reference, and it is the one that draws
        # the cars, so it is pointed at the new projection too.
        if self.loop is not None:
            self.loop.projection = self.projection

    async def _connect_openf1_live(self) -> bool:
        """Fall back to OpenF1's live tier."""
        if self.client is None:
            return False
        try:
            session = await find_live_session(self.client, circuit=self.circuit)
        except Exception as exc:  # noqa: BLE001 - never fail startup
            logger.warning("live session lookup failed: %s", exc)
            return False
        if not session:
            self.live_session = None
            self.mode = "REPLAY"
            return False

        adapter = OpenF1LiveAdapter(
            self.client, int(session["session_key"]), session_meta=session
        )
        if not await adapter.refresh():
            logger.info("live session %s has no data yet", session["session_key"])
            self.live_session = None
            self.mode = "REPLAY"
            return False

        self.live_session = session
        self.mode = "LIVE"
        self.replayer = adapter  # type: ignore[assignment]
        self.circuit = adapter.circuit
        self.year = adapter.year
        self._bundle = self._bundle_for(self.circuit, self.year)
        outline = load_outline(OUTLINE_DIR, self.circuit, self.year)
        if outline is not None:
            self.outline = outline.to_dict()
            self.projection = TrackProjection.from_outline(outline.points)
        # Rebuild the tick loop so it drives the live source.
        await self._replace_loop(adapter)
        logger.info("following live session %s at %s", session["session_key"], self.circuit)
        return True

    async def _transcribe_radio(self, session_key: int) -> None:
        """Transcribe the session's radio calls in the background.

        The calls are audio; the transcript is produced on this machine with
        an offline speech recogniser.  It takes a while - roughly ten seconds
        a call with the good model - so it runs in the background and the
        panel fills in as the results arrive.
        """
        directory = SESSION_DIR / str(session_key)
        messages = _raw_radio(directory)
        if not messages:
            return
        pending = [message for message in messages if not message.text]
        if not pending:
            return
        logger.info("transcribing %d radio calls for session %s", len(pending), session_key)

        async def publish(done: list[RadioMessage]) -> None:
            # The transcription runs newest first, so the file is put back in
            # the order the calls were made.
            save_radio(directory, sorted(done, key=lambda m: str(m.date or "")))

        try:
            # Newest first: the calls that have just been made are the ones
            # on screen, so they are the ones worth having ready.
            await transcribe_all(
                list(reversed(messages)),
                work_dir=directory,
                on_done=publish,
            )
        except Exception as exc:  # noqa: BLE001 - the radio is optional
            logger.warning("radio transcription failed: %s", exc)

    async def shutdown(self) -> None:
        if self.loop is not None:
            await self.loop.stop()
            self.loop = None
        if self.livetiming is not None:
            await self.livetiming.source.aclose()
            self.livetiming = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def watch_for_live(self, interval: float = 60.0) -> None:
        """Keep checking whether a real session has started.

        The pit wall follows the replay until a session is actually running,
        then switches to the live feed by itself.  The check repeats so a race
        that starts while the app is open is picked up without a restart.
        """
        while True:
            await asyncio.sleep(interval)
            try:
                if self.mode == "LIVE":
                    # The live track fills in as the cars circulate, so the
                    # map is refreshed while the session runs.
                    if self._session_has_ended():
                        logger.info("the live session has ended; back to the replay")
                        await self._back_to_replay()
                    else:
                        self._rebuild_live_geometry()
                    continue
                await self._check_for_live_session()
                await self._refresh_a_silent_hub()
            except Exception as exc:  # noqa: BLE001 - the watcher must survive
                logger.warning("live session check failed: %s", exc)

    async def _refresh_a_silent_hub(self) -> None:
        """Reopen a subscription the hub may never push a new session to.

        Three cases need this:

        * a subscription opened before any session has started, which has
          received nothing at all - reopened every half hour;
        * a subscription made during one session, now looking at a finished
          one - reopened every twenty minutes;
        * a session that is scheduled to be running right now - reopened
          every two minutes, so the start is not missed by twenty.

        All three rates are ones the hub's connection budget is comfortable
        with.
        """
        if self.livetiming is None:
            return
        source = self.livetiming.source
        now = datetime.now(UTC)
        start = await self._next_session_start()
        imminent = (
            start is not None
            and start - timedelta(minutes=5) <= now <= start + timedelta(minutes=60)
        )
        if imminent:
            limit = 2
        elif source.state.frames_received:
            limit = 20
        else:
            limit = 30
        self._silent_checks += 1
        if self._silent_checks < limit:
            return
        self._silent_checks = 0
        logger.info("livetiming: reopening the subscription to look for a session")
        # Dropping the source makes the next check negotiate a fresh socket
        # and subscribe again from scratch.
        await source.aclose()
        self.livetiming = None

    async def _next_session_start(self) -> datetime | None:
        """When the next session at this circuit is due to start.

        Used only to decide how often to reopen the hub subscription while no
        session is running.  The calendar is cached, so the free API tier is
        barely touched.
        """
        now = datetime.now(UTC)
        if self._next_session_at is not None and (
            now - self._next_session_at < timedelta(minutes=60)
        ):
            return self._next_session_at
        if self.client is None:
            return None
        if now.timestamp() - self._next_session_checked < 600:
            return self._next_session_at
        self._next_session_checked = now.timestamp()
        try:
            sessions = await self.client.get_sessions(
                circuit_short_name=self.circuit
            )
        except Exception as exc:  # noqa: BLE001 - the calendar is a nicety
            logger.debug("session calendar lookup failed: %s", exc)
            return self._next_session_at
        upcoming = []
        for session in sessions:
            when = _parse_utc(session.get("date_start"))
            if when is None or session.get("is_cancelled"):
                continue
            if when >= now - timedelta(minutes=60):
                upcoming.append(when)
        self._next_session_at = min(upcoming) if upcoming else None
        return self._next_session_at

    def _session_has_ended(self) -> bool:
        """Whether the hub has stopped reporting a running session."""
        if self.livetiming is None:
            return True
        state = self.livetiming.source.state
        if not session_is_running(state.session):
            return True
        last = state.last_frame_at
        if last is not None:
            try:
                return (datetime.now(UTC) - last).total_seconds() > 600
            except (TypeError, ValueError):
                return False
        return False

    async def _back_to_replay(self) -> None:
        """Drop the live feed and follow the replay again.

        The tick loop is left in place: loading the replay swaps the source
        and moves the connected browsers across, so nothing goes dark.
        """
        if self.livetiming is not None:
            await self.livetiming.source.aclose()
            self.livetiming = None
        self.mode = "REPLAY"
        self.live_session = None
        self._live_trace = []
        if not await self._load_replay(DEFAULT_REPLAY_SESSION) and self.loop is not None:
            await self.loop.stop()
            self.loop = None

    async def _load_replay(self, session_key: int) -> bool:
        if not SessionPack.exists(session_key, SESSION_DIR):
            logger.warning("replay session %s is not cached", session_key)
            return False
        pack = SessionPack.load(session_key, SESSION_DIR)
        self.replayer = SessionReplayer(pack)
        self.circuit = self.replayer.circuit
        self.year = self.replayer.year

        # The layout follows the calendar: it is looked up by the circuit key
        # for the season and built on demand, so a new season or a new circuit
        # needs nothing by hand.
        circuit_key = pack.meta.get("circuit_key")
        outline = resolve_outline(
            OUTLINE_DIR,
            self.circuit,
            self.year,
            circuit_key=int(circuit_key) if circuit_key is not None else None,
        )
        if outline is None:
            logger.warning("no outline for %s %s", self.circuit, self.year)
            self.outline = None
            self.projection = None
            return False

        self.outline = outline.to_dict()
        self.projection = TrackProjection.from_outline(outline.points)

        # The pit-wall engine only runs with a fitted calibration bundle.
        bundle = None
        try:
            bundle = load_calibration(self.circuit, self.year)
        except FileNotFoundError:
            logger.warning(
                "no calibration bundle for %s %s: strategy widgets stay unavailable",
                self.circuit,
                self.year,
            )
        self._bundle = bundle
        await self._replace_loop(self.replayer)

        # The radio transcripts are produced in the background: the panel
        # fills in as they arrive rather than after the whole session.
        if self._radio_task is None or self._radio_task.done():
            self._radio_task = asyncio.create_task(self._transcribe_radio(session_key))
        return True

    def frame_payload(self) -> dict[str, Any]:
        if self.replayer is None or self.projection is None:
            return _unavailable("no_replay_session_cached")
        frame = self.replayer.frame(replay_speed=self.replay_speed)
        payload = project_frame(frame, self.projection)
        payload["available"] = True
        payload["circuit"] = self.circuit
        payload["year"] = self.year
        payload["session_meta"] = self.replayer.meta
        payload["projection"] = self.projection.as_dict()
        return payload


state = PitWallState()


# ----------------------------------------------------------------------
# models
# ----------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_key: int | None = None
    query: str
    driver_number: int | None = None


# ----------------------------------------------------------------------
# endpoints
# ----------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "project": "PitWall F1 Strategy Companion",
        "replay_session": state.replayer.session_key if state.replayer else None,
        "circuit": state.circuit,
    }


@app.get("/api/sessions/current")
async def current_session() -> dict[str, Any]:
    """The session the pit wall is following.

    Reports the real session metadata of the replayed race and, when the
    upstream API is reachable, the next real session on the calendar.
    """
    payload: dict[str, Any] = {
        "available": state.replayer is not None,
        "source": (
            ("livetiming.hub" if state.mode == "LIVE" else "openf1.session_pack")
            if state.replayer
            else "unavailable"
        ),
        "mode": state.mode,
    }
    if state.replayer is not None:
        payload.update(
            {
                "session_key": state.replayer.session_key,
                "circuit": state.replayer.circuit,
                "year": state.replayer.year,
                "meeting_name": state.replayer.meta.get("session_name"),
                "session_type": state.replayer.meta.get("session_type"),
                "date_start": state.replayer.meta.get("date_start"),
                "date_end": state.replayer.meta.get("date_end"),
                "total_laps": getattr(state.replayer, "total_laps", None),
                "status": "LIVE" if state.mode == "LIVE" else "REPLAY_ACTIVE",
            }
        )
    else:
        payload.update(_unavailable("no_replay_session_cached"))

    if state.client is not None:
        try:
            sessions = await state.client.get_sessions(circuit_short_name=state.circuit)
            future = [
                s
                for s in sessions
                if s.get("date_start")
                and s["date_start"] > datetime.now(UTC).isoformat()
                and not s.get("is_cancelled")
            ]
            future.sort(key=lambda s: s["date_start"])
            if future:
                payload["next_session"] = future[0]
        except Exception as exc:  # noqa: BLE001 - never fail the endpoint
            payload["next_session_error"] = str(exc)
    return payload


async def _circuit_key(circuit: str, year: int) -> int | None:
    """The OpenF1 circuit key for a circuit and season."""
    if state.client is None:
        return None
    sessions = await state.client.get_sessions(circuit_short_name=circuit, year=year)
    for session in sessions:
        key = session.get("circuit_key")
        if key is not None:
            return int(key)
    return None


@app.get("/api/circuits")
async def circuits_list() -> dict[str, Any]:
    """Every circuit on the calendar, with the key the outline is built from."""
    if state.client is None:
        return _unavailable("openf1_unavailable")
    sessions = await state.client.get_sessions()
    seen: dict[str, dict[str, Any]] = {}
    for session in sessions:
        name = session.get("circuit_short_name")
        if not name:
            continue
        entry = seen.setdefault(
            name,
            {
                "circuit": name,
                "circuit_key": session.get("circuit_key"),
                "year": session.get("year"),
                "location": session.get("location"),
                "country": session.get("country_name"),
            },
        )
        if session.get("year", 0) > entry.get("year", 0):
            entry["year"] = session.get("year")
    return {
        "available": True,
        "source": "openf1",
        "circuits": sorted(seen.values(), key=lambda c: c["circuit"]),
    }


@app.get("/api/circuits/status")
async def circuit_status(circuit: str) -> dict[str, Any]:
    """Real session information for a circuit."""
    if state.client is None:
        return _unavailable("openf1_unavailable", circuit=circuit)
    sessions = await state.client.get_sessions(circuit_short_name=circuit)
    if not sessions:
        return _unavailable("no_sessions_for_circuit", circuit=circuit)
    sessions.sort(key=lambda s: s["date_start"])
    latest = sessions[-1]
    return {
        "available": True,
        "source": "openf1",
        "circuit": circuit,
        "sessions": sessions,
        "latest_session_key": latest["session_key"],
        "latest_session": latest,
    }


@app.get("/api/circuits/{circuit_name}/outline")
async def circuit_outline(circuit_name: str, year: int | None = None) -> dict[str, Any]:
    """The circuit outline plus the projection used to place driver dots.

    The layout follows the calendar: the circuit is looked up by its OpenF1
    ``circuit_key`` for the season, so a new race weekend needs no manual
    change.  The result is cached on disk after the first build.
    """
    circuit = circuit_name.strip().title()
    target_year = year or (state.year if circuit == state.circuit else 2024)

    outline = load_outline(OUTLINE_DIR, circuit, target_year)
    if outline is None and state.client is not None:
        circuit_key = await _circuit_key(circuit, target_year)
        if circuit_key is not None:
            outline = resolve_outline(OUTLINE_DIR, circuit, target_year, circuit_key=circuit_key)
    if outline is None:
        return _unavailable("outline_not_built", circuit=circuit, year=target_year)
    projection = TrackProjection.from_outline(outline.points)
    data = outline.to_dict()
    projected = [
        {"x": projection.project(x, y)[0], "y": projection.project(x, y)[1]}
        for x, y in outline.points
    ]
    return {
        "available": True,
        "source": outline.source,
        "circuit": circuit,
        "year": target_year,
        "points": projected,
        "raw_points": data["points"],
        "corners": [
            {
                "number": c["number"],
                "letter": c.get("letter", ""),
                "x": projection.project(c["x"], c["y"])[0],
                "y": projection.project(c["x"], c["y"])[1],
            }
            for c in outline.corners
        ],
        "start_finish": (
            {
                "x": projection.project(*outline.start_finish)[0],
                "y": projection.project(*outline.start_finish)[1],
            }
            if outline.start_finish
            else None
        ),
        "view_box": {
            "width": projection.view_width,
            "height": projection.view_height,
        },
        "projection": projection.as_dict(),
        "metadata": outline.metadata,
    }


@app.get("/api/outline/current")
async def current_outline() -> dict[str, Any]:
    """The map for whatever is being shown: the replay, or the live session.

    A live session has no GPS outline - F1's live feed reports positions in
    its own coordinate frame - so the live outline is the path the cars have
    driven, and it fills in as they circulate.
    """
    projection = state.projection
    if projection is None:
        return _unavailable("no_track_geometry")

    if state.mode == "LIVE":
        trace = state._live_trace  # noqa: SLF001 - same package
        if len(trace) < 10:
            return _unavailable(
                "track_still_building",
                mode=state.mode,
                points=len(trace),
                circuit=state.circuit,
            )
        return {
            "available": True,
            "source": "livetiming.positions",
            "circuit": state.circuit,
            "year": state.year,
            "points": [
                {"x": projection.project(x, y)[0], "y": projection.project(x, y)[1]}
                for x, y in trace
            ],
            "raw_points": [{"x": x, "y": y} for x, y in trace],
            "corners": [],
            "start_finish": None,
            "view_box": {"width": projection.view_width, "height": projection.view_height},
            "projection": projection.as_dict(),
            "metadata": {"point_count": len(trace)},
        }

    outline = state.outline
    if not outline:
        return _unavailable("outline_not_built", circuit=state.circuit, year=state.year)
    return {
        "available": True,
        "source": outline.get("source"),
        "circuit": outline.get("circuit", state.circuit),
        "year": outline.get("year", state.year),
        "points": [
            {
                "x": projection.project(float(p["x"]), float(p["y"]))[0],
                "y": projection.project(float(p["x"]), float(p["y"]))[1],
            }
            for p in outline.get("points") or []
        ],
        "raw_points": outline.get("points") or [],
        "corners": [
            {
                "number": c.get("number"),
                "letter": c.get("letter", ""),
                "x": projection.project(float(c["x"]), float(c["y"]))[0],
                "y": projection.project(float(c["x"]), float(c["y"]))[1],
            }
            for c in outline.get("corners") or []
        ],
        "start_finish": (
            {
                "x": projection.project(*outline["start_finish"])[0],
                "y": projection.project(*outline["start_finish"])[1],
            }
            if isinstance(outline.get("start_finish"), (list, tuple))
            and len(outline["start_finish"]) == 2
            else None
        ),
        "view_box": {"width": projection.view_width, "height": projection.view_height},
        "projection": projection.as_dict(),
        "metadata": outline.get("metadata") or {},
    }


@app.get("/api/championship/drivers")
async def championship_drivers() -> list[dict[str, Any]]:
    """The 2026 drivers' championship with computed title odds."""
    return [standing.model_dump() for standing in get_2026_driver_standings()]


@app.get("/api/championship/constructors")
async def championship_constructors() -> list[dict[str, Any]]:
    """The 2026 constructors' championship."""
    return [standing.model_dump() for standing in get_2026_constructor_standings()]


@app.get("/api/championship/scenarios")
async def championship_scenarios() -> dict[str, Any]:
    """The title scenarios derived from the current points table."""
    return get_championship_scenarios()


@app.get("/api/drivers")
async def drivers(session_key: int | None = None) -> dict[str, Any]:
    """The driver list of the session being followed."""
    if state.replayer is None:
        return _unavailable("no_replay_session_cached")
    return {
        "available": True,
        "source": "openf1",
        "session_key": state.replayer.session_key,
        "drivers": state.replayer.drivers,
    }


@app.get("/api/positions")
async def positions(
    session_key: int | None = None, at: float | None = None
) -> dict[str, Any]:
    """Car positions for every driver, projected onto the circuit.

    ``at`` is the replay timestamp in seconds since the start of the session;
    omit it for "now" in the replay.
    """
    if state.replayer is None or state.projection is None:
        return _unavailable("no_replay_session_cached")
    async with state.lock:
        if at is not None:
            state.replayer.seek(at)
        frame = state.replayer.frame(replay_speed=state.replay_speed)
        payload = project_frame(frame, state.projection)
        if state.loop is not None:
            state.loop.add_lookahead(payload)
    return {
        "available": True,
        "source": "openf1",
        "session_key": payload["session_key"],
        "timestamp": payload["timestamp"],
        "elapsed_s": payload["elapsed_s"],
        "flag_state": payload["flag_state"],
        "lap": payload["lap"],
        "total_laps": payload["total_laps"],
        "drivers": payload["drivers"],
    }


@app.get("/api/telemetry")
async def telemetry(
    driver_number: int | None = None, at: float | None = None
) -> dict[str, Any]:
    """Telemetry for one driver (or the whole field)."""
    if state.replayer is None:
        return _unavailable("no_replay_session_cached")
    async with state.lock:
        if at is not None:
            state.replayer.seek(at)
        frame = state.replayer.frame(replay_speed=state.replay_speed)
    if driver_number is None:
        selected = frame.drivers
    else:
        selected = [d for d in frame.drivers if d.driver_number == driver_number]
        if not selected:
            return _unavailable(
                "driver_not_in_session", driver_number=driver_number
            )
    return {
        "available": True,
        "source": "openf1",
        "timestamp": frame.timestamp.isoformat(),
        "elapsed_s": round(frame.elapsed_s, 3),
        "telemetry": [d.to_dict() for d in selected],
    }


@app.get("/api/timing")
async def timing(at: float | None = None) -> dict[str, Any]:
    """The timing tower: order, gaps, tyres and stint ages."""
    if state.replayer is None or state.projection is None:
        return _unavailable("no_replay_session_cached")
    async with state.lock:
        if at is not None:
            state.replayer.seek(at)
        frame = state.replayer.frame(replay_speed=state.replay_speed)
        payload = project_frame(frame, state.projection)
    return {
        "available": True,
        "source": "openf1",
        "timestamp": payload["timestamp"],
        "elapsed_s": payload["elapsed_s"],
        "flag_state": payload["flag_state"],
        "lap": payload["lap"],
        "total_laps": payload["total_laps"],
        "timing": payload["drivers"],
    }


@app.get("/api/replay/control")
async def replay_control(
    action: str = "status", speed: float | None = None, at: float | None = None
) -> dict[str, Any]:
    """Control the replay: play/pause, change speed, seek."""
    if state.replayer is None:
        return _unavailable("no_replay_session_cached")
    async with state.lock:
        if speed is not None:
            state.replay_speed = max(0.0, min(speed, 60.0))
        if state.loop is not None:
            state.loop.speed = state.replay_speed
        if at is not None:
            if state.loop is not None:
                state.loop.seek(at)
            else:
                state.replayer.seek(at)
        if action == "pause":
            state.replay_speed = 0.0
            if state.loop is not None:
                state.loop.speed = 0.0
        elif action == "play":
            if state.replay_speed <= 0:
                state.replay_speed = 1.0
            if state.loop is not None:
                state.loop.speed = state.replay_speed
        elif action == "reset":
            if state.loop is not None:
                state.loop.seek(0.0)
            else:
                state.replayer.seek(0.0)
        frame = state.replayer.frame(replay_speed=state.replay_speed)
        payload = project_frame(frame, state.projection) if state.projection else {}
        clock = getattr(state.replayer, "clock", None)
    return {
        "available": True,
        "source": "openf1",
        "action": action,
        "speed": state.replay_speed,
        "elapsed_s": round(float(getattr(clock, "elapsed_s", 0.0) or 0.0), 3),
        "duration_s": round(float(getattr(clock, "duration_s", 0.0) or 0.0), 3),
        "finished": bool(getattr(clock, "finished", False)),
        "drivers": payload.get("drivers", []),
    }


@app.get("/api/strategies")
async def strategies(
    driver_number: int | None = None, at: float | None = None
) -> dict[str, Any]:
    """The pit-wall report: tyres, pace, ranked strategies and alerts.

    Engine-gated: when no calibration bundle exists for the circuit the
    endpoint says so instead of producing plausible-looking numbers.
    """
    if state.loop is None or state.replayer is None:
        return _unavailable("no_replay_session_cached")
    if state.loop.engine is None:
        return _unavailable(
            "engine_not_calibrated",
            circuit=state.circuit,
            year=state.year,
        )

    async with state.lock:
        if at is not None:
            state.replayer.seek(at)
        if driver_number is not None:
            state.loop.focus_driver = driver_number
        frame = state.replayer.frame(replay_speed=state.replay_speed)
        driver_number = driver_number or state.loop.focus_driver
        if driver_number is None:
            leader = frame.drivers[0] if frame.drivers else None
            driver_number = leader.driver_number if leader else None
        if driver_number is None:
            return _unavailable("no_driver_context")

        driver = next(
            (d for d in frame.drivers if d.driver_number == driver_number), None
        )
        if driver is None:
            return _unavailable("driver_not_in_session", driver_number=driver_number)

        rival = state.loop._rival_for(frame, driver)  # noqa: SLF001 - same package
        # The live adapter does not carry the replay-only helpers (elapsed
        # race time, stints, fitted pace), so they are looked up defensively
        # rather than assumed - otherwise every session goes 500 in live mode.
        elapsed = _optional(state.replayer, "elapsed_race_time", driver_number, driver.lap or 1)
        stint = _optional(state.replayer, "stint_for", driver_number, driver.lap)
        pace = _optional(state.replayer, "driver_pace", driver_number, driver.lap)
        rival_pace = (
            _optional(state.replayer, "driver_pace", rival.driver_number, rival.lap)
            if rival is not None
            else None
        )
        clock = getattr(state.replayer, "clock", None)
        try:
            messages = (
                state.replayer.recent_messages(clock.position_ns, limit=6)
                if clock is not None
                else state.replayer.recent_messages(limit=6)
            )
        except Exception:  # noqa: BLE001 - messages are a nicety
            messages = []
        report = state.loop.engine.build_report(
            frame=frame,
            driver_number=driver_number,
            elapsed_s=elapsed or 0.0,
            stint=stint,
            rival=rival,
            messages=messages,
            driver_pace=pace,
            rival_pace=rival_pace,
        )
    return report.to_dict()


@app.get("/api/live/status")
async def live_status() -> dict[str, Any]:
    """What the live feeds are doing right now.

    F1's own timing hub is free and unauthenticated, so it is connected
    whenever the app is running; whether it is *carrying a session* is a
    separate question, and it is answered here.
    """
    hub = state.livetiming
    if hub is None:
        return {
            "available": False,
            "mode": state.mode,
            "hub_connected": False,
            "hub_error": None,
            "session": None,
        }
    session = hub.source.state.session
    source_state = hub.source.state
    return {
        "available": True,
        "mode": state.mode,
        "hub_connected": hub.source.connected,
        "hub_subscribed": hub.source.subscribed,
        "hub_error": hub.source.last_error,
        "session": {
            "session_key": session.get("Key"),
            "name": session.get("Name"),
            "type": session.get("Type"),
            "path": session.get("Path"),
        }
        if session
        else None,
        "drivers": len(source_state.cars),
        "radio_calls": len(source_state.radio),
        "weather_reports": len(source_state.weather),
        "messages": len(source_state.messages),
        # Diagnosis: which topics are actually arriving, so a silent feed is
        # visible instead of looking like "no session is running".
        "topics": dict(source_state.topics),
        "frames_received": source_state.frames_received,
        "bad_frames": source_state.bad_frames,
        "last_frame_at": (
            source_state.last_frame_at.isoformat()
            if source_state.last_frame_at is not None
            else None
        ),
    }


@app.get("/api/weather")
async def weather() -> dict[str, Any]:
    """The real weather over the track.

    OpenF1 publishes a per-minute weather feed for every session - air and
    track temperature, humidity, pressure, wind and rainfall.  That is what
    is shown.  The race-control broadcasts (the rain-risk briefing and the
    track surface reports) are the fallback for sessions whose weather feed
    was not retained.
    """
    if state.replayer is None:
        return _unavailable("no_replay_session_cached")
    summary = _weather_summary()
    if not summary.get("available"):
        return _unavailable(
            "no_weather_feed",
            reason_detail=(
                "No weather feed was published for this session and race "
                "control broadcast no rain-risk or track surface message."
            ),
        )
    return summary


@app.get("/api/radio")
async def radio(limit: int = 30) -> dict[str, Any]:
    """Team radio: the calls, their audio, and their transcript.

    The calls come from OpenF1's ``/team_radio`` feed.  Each one is an MP3 on
    F1's public static archive, and the transcript is produced on this
    machine with an offline speech recogniser - nothing is sent anywhere.

    The feed is deliberately **not** fetched while a request is waiting: the
    calls are fetched once, when the session is prepared, and served from the
    cache afterwards.
    """
    if state.replayer is None:
        return _unavailable("no_replay_session_cached")

    if state.mode == "LIVE":
        # The hub sends its own captions with each call, so a live session
        # needs no download and no transcription: the words arrive with the
        # audio.
        rows = _optional(state.replayer, "recent_radio", limit) or []
        drivers = (
            state.livetiming.source.state.drivers if state.livetiming is not None else {}
        )
        calls: list[dict[str, Any]] = []
        for row in rows:
            number = row.get("driver_number")
            meta = drivers.get(int(number), {}) if number is not None else {}
            calls.append(
                {
                    "date": row.get("date"),
                    "driver_number": number,
                    "team": meta.get("team"),
                    "text": row.get("text"),
                    "recording_url": None,
                    "duration": None,
                }
            )
        return {
            "available": bool(calls),
            "source": "livetiming.team_radio" if calls else "unavailable",
            "reason": "" if calls else "no_radio_feed",
            "count": len(calls),
            "transcribed": len(calls),
            "pending": 0,
            "messages": calls,
        }

    directory = SESSION_DIR / str(state.replayer.session_key)
    messages = load_cached_radio(directory)
    raw = _raw_radio(directory)

    # The cached file carries the transcripts; the raw file carries every
    # call.  Merge them so the panel shows the calls even while the
    # transcripts are still being produced.
    if raw:
        by_key = {message.recording_url: message for message in messages}
        merged: list[RadioMessage] = []
        for message in raw:
            cached = by_key.get(message.recording_url)
            if cached is not None and cached.text:
                message.text = cached.text
            merged.append(message)
        messages = merged

    if not messages:
        return {
            "available": False,
            "source": "unavailable",
            "reason": "no_radio_feed",
            "count": 0,
            "messages": [],
            "detail": (
                "F1 did not publish team radio for this session. Coverage "
                "varies by event and fell sharply through 2026, so many "
                "sessions carry no radio at all."
            ),
        }

    transcribed = sum(1 for message in messages if message.text)

    # The radio plays out with the race: only the calls that have already
    # happened by now are shown, so a call appears at the moment it is made
    # rather than the whole session appearing at once.
    if state.replayer is not None:
        clock = getattr(state.replayer, "clock", None)
        position = getattr(clock, "position_ns", 0) if clock is not None else 0
        if position:
            upto = datetime.fromtimestamp(position / 1e9, tz=UTC)
            in_play = []
            for message in messages:
                stamp = message.date
                if not stamp:
                    in_play.append(message)
                    continue
                try:
                    when = datetime.fromisoformat(stamp)
                except ValueError:
                    in_play.append(message)
                    continue
                if when <= upto:
                    in_play.append(message)
            messages = in_play

    transcribed = sum(1 for message in messages if message.text)
    return {
        "available": True,
        "source": "openf1.team_radio",
        "count": len(messages),
        "transcribed": transcribed,
        "pending": len(messages) - transcribed,
        "messages": [message.to_dict() for message in messages[-limit:]],
    }


def _raw_radio(directory: Path) -> list[RadioMessage]:
    """The radio calls as OpenF1 published them, without transcripts."""
    path = directory / "radio_raw.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    messages: list[RadioMessage] = []
    for row in rows:
        message = radio_message_from_row(row)
        if message is not None:
            messages.append(message)
    messages.sort(key=lambda message: str(message.date or ""))
    return messages


@app.get("/api/messages")
async def messages(limit: int = 12) -> dict[str, Any]:
    """Recent race-control messages."""
    if state.replayer is None:
        return _unavailable("no_replay_session_cached")
    return {
        "available": True,
        "source": "openf1.race_control" if state.mode == "REPLAY" else "livetiming",
        "messages": _recent_messages(limit),
    }


@app.get("/api/live/snapshot")
async def live_snapshot() -> dict[str, Any]:
    """One poll of the *live* OpenF1 session, for the whole field at once.

    This issues a single date-windowed request per endpoint rather than one
    request per driver, which is what keeps the free tier from rate-limiting
    us (see :mod:`pitwall.realtime.live`).
    """
    if state.client is None:
        return _unavailable("openf1_unavailable")
    source = OpenF1LiveSource(state.client, DEFAULT_REPLAY_SESSION)
    snapshot = await source.poll()
    if not snapshot.positions and not snapshot.telemetry:
        return _unavailable("no_live_data")
    positions_by_driver = latest_per_driver(snapshot.positions)
    telemetry_by_driver = latest_per_driver(snapshot.telemetry)
    drivers_out = []
    for number, row in sorted(positions_by_driver.items()):
        tel = telemetry_by_driver.get(number, {})
        drivers_out.append(
            {
                "driver_number": number,
                "x": row.get("x"),
                "y": row.get("y"),
                "z": row.get("z"),
                "date": row.get("date"),
                "speed": tel.get("speed"),
                "throttle": tel.get("throttle"),
                "brake": tel.get("brake"),
                "n_gear": tel.get("n_gear"),
                "rpm": tel.get("rpm"),
                "drs": tel.get("drs"),
            }
        )
    return {
        "available": True,
        "source": "openf1",
        "timestamp": snapshot.timestamp.isoformat(),
        "requests_per_tick": 2,
        "drivers": drivers_out,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    """Strategy chat.

    The numbers come from the strategy engine when it is calibrated; when it is
    not, the endpoint says so instead of inventing figures.
    """
    from pitwall.api.strategy import answer_question

    return await answer_question(req, state)


@app.websocket("/ws/live")
async def live_websocket(websocket: WebSocket) -> None:
    """Stream race frames to the browser.

    The tick loop owns the clock, so every connected client sees exactly the
    same state and the replay is advanced once, not once per client.
    """
    await websocket.accept()
    queue: asyncio.Queue[dict[str, Any]] | None = None
    try:
        if state.loop is None:
            await websocket.send_json(_unavailable("no_replay_session_cached"))
            return

        queue = state.loop.subscribe()
        state.loop.start()
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:  # noqa: BLE001 - never kill the socket silently
        logger.warning("websocket error: %s", exc)
        with contextlib.suppress(Exception):
            await websocket.send_json(_unavailable("websocket_error"))
    finally:
        if queue is not None and state.loop is not None:
            state.loop.unsubscribe(queue)
