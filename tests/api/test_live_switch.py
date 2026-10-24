"""The hand-over from the replay to a live session.

The browser subscribes to one tick loop, so switching sources has to move the
subscribers across.  Getting that wrong is invisible from the server side -
the app reports ``mode: LIVE`` while the page keeps showing the replay - so it
is pinned here without touching the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pitwall.api.app import PitWallState
from pitwall.data.track import TrackProjection


class _State(SimpleNamespace):
    """The slice of the hub's state the switch reads."""

    def __init__(self) -> None:
        super().__init__(
            session={
                "Key": 11390,
                "Name": "Practice 3",
                "Type": "Practice",
                "SessionStatus": {"Status": "Started"},
                "Meeting": {"Circuit": {"ShortName": "Austin"}, "Year": 2026},
            },
            cars={},
            timing={},
            drivers={},
            tyres={},
            order={},
            lap_count={},
            radio=[],
            weather=[],
            messages=[],
            topics={},
            track_status={},
            clock={},
            frames_received=3,
            bad_frames=0,
            last_frame_at=None,
        )


class _Source:
    connected = True
    subscribed = True
    last_error: str | None = None

    def __init__(self) -> None:
        self.state = _State()

    def bounds(self) -> tuple[float, float, float, float]:
        return 0.0, 100.0, 0.0, 100.0

    def trace(self) -> list[tuple[float, float]]:
        return [(float(i), float(i)) for i in range(40)]


class _Adapter:
    """Just enough of the live adapter for the switch."""

    circuit = "Austin"
    year = 2026

    def __init__(self) -> None:
        self.source = _Source()
        self.last_error = None

    @property
    def session_key(self) -> int:
        return 11390

    @property
    def meta(self) -> dict[str, Any]:
        return {"session_name": "Practice 3", "session_type": "Practice"}

    def frame(self, replay_speed: float = 1.0) -> Any:  # pragma: no cover - unused
        raise AssertionError("the switch must not build a frame")


async def test_the_live_switch_carries_the_open_browser_across() -> None:
    app_state = PitWallState()
    app_state.projection = TrackProjection.from_outline(
        [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    )
    app_state.livetiming = _Adapter()  # type: ignore[assignment]

    assert await app_state._connect_livetiming() is True

    assert app_state.mode == "LIVE"
    assert app_state.replayer is app_state.livetiming
    assert app_state.loop is not None
    assert app_state.loop.replayer is app_state.livetiming
    assert app_state.circuit == "Austin"
    assert app_state.year == 2026
    # The map is built from the cars' path, not from a GPS outline.
    assert app_state._live_trace == [(float(i), float(i)) for i in range(40)]
