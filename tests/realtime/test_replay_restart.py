"""The replay must start again when it reaches the end.

The clock stops itself at the last racing lap.  Without a rewind the app
then sits on the final frame for ever, which looks like a frozen page
rather than a race that has finished.
"""

from pathlib import Path

from pitwall.realtime.tick_loop import FINISH_HOLD_S, TickLoop


class _Clock:
    def __init__(self) -> None:
        self.position_ns = 0
        self.end_ns = 10
        self.start_ns = 0
        self.seeks: list[float] = []

    @property
    def finished(self) -> bool:
        return self.position_ns >= self.end_ns


class _Replayer:
    session_key = "test"

    def __init__(self) -> None:
        self.clock = _Clock()

    def seek(self, at: float) -> None:
        self.clock.seeks.append(at)
        self.clock.position_ns = int(at)

    def advance(self, seconds: float) -> None:
        self.clock.position_ns = min(
            self.clock.end_ns, self.clock.position_ns + int(seconds)
        )


def _loop(tmp_path: Path, replayer: _Replayer) -> TickLoop:
    return TickLoop(
        replayer,  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
        snapshot_dir=tmp_path,
        interval=1.0,
    )


def test_a_finished_replay_is_held_then_restarted(tmp_path: Path) -> None:
    replayer = _Replayer()
    replayer.clock.position_ns = replayer.clock.end_ns
    loop = _loop(tmp_path, replayer)

    # It holds the final frame for a moment rather than snapping away.
    for _ in range(int(FINISH_HOLD_S) - 1):
        loop._rewind_if_finished()
        assert replayer.clock.seeks == []

    loop._rewind_if_finished()
    assert replayer.clock.seeks == [0.0]


def test_a_running_replay_is_left_alone(tmp_path: Path) -> None:
    replayer = _Replayer()
    replayer.clock.position_ns = 5
    loop = _loop(tmp_path, replayer)

    for _ in range(30):
        loop._rewind_if_finished()
    assert replayer.clock.seeks == []


def test_a_new_run_gets_its_alerts_back(tmp_path: Path) -> None:
    replayer = _Replayer()
    replayer.clock.position_ns = replayer.clock.end_ns
    loop = _loop(tmp_path, replayer)
    loop._alert_keys.add("pit_window|1")

    for _ in range(int(FINISH_HOLD_S)):
        loop._rewind_if_finished()

    assert loop._alert_keys == set(), "a second run should alert again"


def test_the_hold_resets_once_the_replay_moves(tmp_path: Path) -> None:
    """A manual seek clears the hold, so a restart is immediate."""
    replayer = _Replayer()
    replayer.clock.position_ns = replayer.clock.end_ns
    loop = _loop(tmp_path, replayer)
    loop._rewind_if_finished()
    assert loop._finished_for == 1.0

    loop.seek(0.0)
    assert loop._finished_for == 0.0
    assert replayer.clock.seeks == [0.0]


def test_tick_advances_after_the_end(tmp_path: Path) -> None:
    """End to end: a clock at the end still yields a moving replay."""
    replayer = _Replayer()
    replayer.clock.end_ns = 5
    replayer.clock.position_ns = 5
    loop = _loop(tmp_path, replayer)
    loop.speed = 1.0

    for _ in range(int(FINISH_HOLD_S)):
        loop._rewind_if_finished()
    assert replayer.clock.seeks == [0.0]

    replayer.advance(1.0)
    assert replayer.clock.position_ns == 1
    assert not replayer.clock.finished
