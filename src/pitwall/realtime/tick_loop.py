"""The tick loop — the spine of the pit wall.

Every 1-5 seconds the loop:

1. advances the data source (replay or live),
2. rebuilds the world state,
3. evaluates the trigger thresholds,
4. broadcasts the state (and any new alerts) to the connected clients,
5. persists a snapshot so a restart resumes instead of re-firing alerts.

The loop owns the clock, so the WebSocket handlers stay thin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pitwall.data.track import TrackProjection
from pitwall.engine.calibration import CircuitCalibration
from pitwall.engine.pitwall import PitWallEngine, PitWallReport
from pitwall.realtime.session_replay import SessionReplayer, project_frame

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = Path("data/cache/snapshots")

#: How long the final frame is held before the replay starts again.
FINISH_HOLD_S = 8.0


def _call(source: Any, name: str, *args: Any) -> Any:
    """Call an optional method on a data source.

    The live adapter does not have the replay-only helpers (elapsed race time,
    stint, driver pace), so they are looked up defensively.
    """
    method = getattr(source, name, None)
    if method is None:
        return None
    try:
        return method(*args)
    except Exception:  # noqa: BLE001 - an optional extra must not break the tick
        logger.debug("optional source method %s failed", name, exc_info=True)
        return None


@dataclass
class TickResult:
    """Everything one tick produced."""

    state: dict[str, Any]
    alerts: list[dict[str, Any]] = field(default_factory=list)
    report: PitWallReport | None = None


class TickLoop:
    """Advances the replay and publishes the resulting state."""

    def __init__(
        self,
        replayer: SessionReplayer,
        projection: TrackProjection,
        bundle: CircuitCalibration | None = None,
        *,
        snapshot_dir: str | Path = SNAPSHOT_DIR,
        interval: float = 1.0,
    ) -> None:
        self.replayer = replayer
        self.projection = projection
        self.bundle = bundle
        self.interval = interval
        self.snapshot_dir = Path(snapshot_dir)
        self.engine = PitWallEngine(bundle) if bundle is not None else None
        self.focus_driver: int | None = None
        self.speed: float = 1.0
        self._alert_keys: set[str] = set()
        self._listeners: list[asyncio.Queue[dict[str, Any]]] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._finished_for = 0.0
        self._load_snapshot()

    # ------------------------------------------------------------------
    # subscribers
    # ------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        self._listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(queue)

    def _broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self._listeners):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)
            # Drop the oldest message if the client is not keeping up.
            with contextlib.suppress(asyncio.QueueEmpty):
                if queue.qsize() >= queue.maxsize:
                    queue.get_nowait()

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------
    @property
    def _snapshot_path(self) -> Path:
        return self.snapshot_dir / f"session_{self.replayer.session_key}.json"

    def _load_snapshot(self) -> None:
        path = self._snapshot_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._alert_keys = set(data.get("alert_keys") or [])
        elapsed = data.get("elapsed_s")
        if elapsed:
            self.replayer.seek(float(elapsed))
        logger.info("resumed session %s at %.1fs", self.replayer.session_key, elapsed or 0)

    def _save_snapshot(self) -> None:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_key": self.replayer.session_key,
            "elapsed_s": self.replayer.clock.elapsed_s,
            "alert_keys": sorted(self._alert_keys),
            "saved_at": datetime.now(UTC).isoformat(),
        }
        tmp = self._snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._snapshot_path)

    # ------------------------------------------------------------------
    # ticking
    # ------------------------------------------------------------------
    def seek(self, at: float) -> None:
        """Move the replay clock, clearing any end-of-race hold."""
        seek = getattr(self.replayer, "seek", None)
        if seek is not None:
            seek(at)
        self._finished_for = 0.0

    def _rewind_if_finished(self) -> None:
        """Start the replay again once it has reached the end.

        The clock stops itself at the last racing lap, so without this the
        replay sat on its final frame for ever and looked like a frozen app.
        The last frame is held for :data:`FINISH_HOLD_S` and then the replay
        runs again from the beginning.

        A live source never reports a finished clock, so it is untouched.
        """
        clock = getattr(self.replayer, "clock", None)
        if clock is None or not getattr(clock, "finished", False):
            self._finished_for = 0.0
            return
        seek = getattr(self.replayer, "seek", None)
        if seek is None:
            return
        self._finished_for += self.interval
        if self._finished_for < FINISH_HOLD_S:
            return
        seek(0.0)
        self._finished_for = 0.0
        # A new run gets its alerts back.
        self._alert_keys.clear()
        logger.info("replay reached the end; starting it again")

    def tick(self) -> TickResult:
        """Advance one tick and return the new state."""
        if self.speed > 0:
            self._rewind_if_finished()
            self.replayer.advance(self.interval * self.speed)
        frame = self.replayer.frame(replay_speed=self.speed)
        state = project_frame(frame, self.projection)
        state["available"] = True
        state["type"] = "state"
        state["circuit"] = self.replayer.circuit
        state["year"] = self.replayer.year
        state["session_meta"] = self.replayer.meta
        state["projection"] = self.projection.as_dict()
        state["messages"] = self._recent_messages()

        # Where each car will be one tick from now, so the browser can
        # interpolate and the cars move smoothly rather than jumping.
        self.add_lookahead(state)

        report = self._build_report(frame)
        if report is not None:
            state["pitwall"] = report.to_dict()

        alerts = self._collect_alerts(report)
        return TickResult(state=state, alerts=alerts, report=report)

    def _recent_messages(self) -> list[dict[str, Any]]:
        """Race-control messages, whether the source is live or a replay."""
        replayer = self.replayer
        messages = getattr(replayer, "recent_messages", None)
        if messages is None:
            return []
        try:
            return messages(limit=8)
        except TypeError:
            clock = getattr(replayer, "clock", None)
            when = getattr(clock, "position_ns", 0) if clock else 0
            return messages(when, 8)

    def add_lookahead(self, state: dict[str, Any]) -> None:
        """Publish each driver's position one tick ahead.

        The browser draws the car between the current and the next position,
        so the motion is smooth at any replay speed instead of stepping once
        per second.
        """
        positions_at = getattr(self.replayer, "positions_at", None)
        if positions_at is None:
            return
        clock = getattr(self.replayer, "clock", None)
        if clock is None:
            return
        ahead_ns = clock.position_ns + int(self.interval * self.speed * 1e9)
        try:
            positions = positions_at(ahead_ns)
        except Exception:  # noqa: BLE001 - the lookahead is a nicety
            return
        for driver in state.get("drivers", []):
            position = positions.get(int(driver.get("driver_number") or 0))
            if position is None:
                driver["next_view_x"] = None
                driver["next_view_y"] = None
                continue
            view_x, view_y = self.projection.project(position[0], position[1])
            driver["next_view_x"] = view_x
            driver["next_view_y"] = view_y

    def _build_report(self, frame: Any) -> PitWallReport | None:
        if self.engine is None or not frame.drivers:
            return None
        driver_number = self.focus_driver
        if driver_number is None:
            leader = frame.drivers[0]
            driver_number = leader.driver_number
        driver = next(
            (d for d in frame.drivers if d.driver_number == driver_number), None
        )
        if driver is None:
            return None

        rival = self._rival_for(frame, driver)
        replayer = self.replayer
        elapsed = _call(
            replayer, "elapsed_race_time", driver.driver_number, driver.lap or 1
        )
        stint = _call(replayer, "stint_for", driver.driver_number, driver.lap)
        pace = _call(replayer, "driver_pace", driver.driver_number, driver.lap)
        rival_pace = (
            _call(replayer, "driver_pace", rival.driver_number, rival.lap)
            if rival is not None
            else None
        )
        return self.engine.build_report(
            frame=frame,
            driver_number=driver.driver_number,
            elapsed_s=elapsed or 0.0,
            stint=stint,
            rival=rival,
            messages=self._recent_messages()[-6:],
            driver_pace=pace,
            rival_pace=rival_pace,
        )

    @staticmethod
    def _rival_for(frame: Any, driver: Any) -> Any:
        """The car directly ahead of the driver."""
        ahead = [
            other
            for other in frame.drivers
            if other.position is not None
            and driver.position is not None
            and other.position == driver.position - 1
        ]
        if ahead:
            return ahead[0]
        behind = [
            other
            for other in frame.drivers
            if other.position is not None
            and driver.position is not None
            and other.position == driver.position + 1
        ]
        return behind[0] if behind else None

    def _collect_alerts(self, report: PitWallReport | None) -> list[dict[str, Any]]:
        """Turn the report's alerts into broadcastable messages."""
        if report is None:
            return []
        out: list[dict[str, Any]] = []
        for alert in report.alerts:
            key = f"{alert['type']}|{alert['driver_number']}"
            if key in self._alert_keys:
                continue
            self._alert_keys.add(key)
            out.append(
                {
                    "type": "alert",
                    "kind": alert["type"],
                    "driver_number": alert["driver_number"],
                    "lap": alert["lap"],
                    "payload": alert["payload"],
                }
            )
        return out

    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Run the loop until stopped."""
        self._running = True
        ticks = 0
        while self._running:
            try:
                result = self.tick()
                self._broadcast(result.state)
                for alert in result.alerts:
                    self._broadcast(alert)
                ticks += 1
                if ticks % 10 == 0:
                    self._save_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.exception("tick failed: %s", exc)
            await asyncio.sleep(self.interval)
        self._save_snapshot()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._save_snapshot()
