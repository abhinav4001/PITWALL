"""Tests for the circuit geometry and the shared projection.

Synthetic fixtures only: no test may call the live OpenF1 API (GR8).
"""

import math
from datetime import UTC

import pytest

from pitwall.data.track import (
    Outline,
    TrackGeometryError,
    TrackProjection,
    _resample,
    deviation_from_outline,
    outline_from_session,
)


def _square_outline(size: float = 1000.0) -> list[tuple[float, float]]:
    """A unit square traversed counter-clockwise in a y-up frame."""
    return [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size), (0.0, 0.0)]


def test_projection_flips_y_for_svg() -> None:
    """SVG's y axis points down, so the projection must invert it.

    A circuit is counter-clockwise when read with y pointing up.  Drawing it
    into SVG without inverting y would mirror the circuit, which is exactly
    the bug this projection prevents.
    """
    points = _square_outline()
    projection = TrackProjection.from_outline(points)

    low = projection.project(0.0, 0.0)
    high = projection.project(0.0, 1000.0)

    assert low[1] > high[1], "larger raw y must end up higher on screen (smaller SVG y)"
    assert low[0] == high[0], "x must not be affected by the flip"


def test_projection_preserves_aspect_ratio() -> None:
    """The true circuit proportions must survive the projection."""
    points = [(0.0, 0.0), (2000.0, 0.0), (2000.0, 1000.0), (0.0, 1000.0)]
    projection = TrackProjection.from_outline(points)

    a = projection.project(0.0, 0.0)
    b = projection.project(2000.0, 0.0)
    c = projection.project(0.0, 1000.0)

    width = abs(b[0] - a[0])
    height = abs(c[1] - a[1])
    assert width / height == pytest.approx(2.0, rel=0.02)


def test_projection_is_invertible_for_points_on_the_outline() -> None:
    points = _square_outline()
    projection = TrackProjection.from_outline(points)
    for x, y in points:
        view_x, view_y = projection.project(x, y)
        assert 0.0 <= view_x <= projection.view_width
        assert 0.0 <= view_y <= projection.view_height


def test_projection_requires_points() -> None:
    with pytest.raises(TrackGeometryError):
        TrackProjection.from_outline([])


def test_resample_returns_evenly_spaced_closed_path() -> None:
    circle = [
        (1000.0 * math.cos(2 * math.pi * i / 50), 1000.0 * math.sin(2 * math.pi * i / 50))
        for i in range(50)
    ]
    resampled = _resample(circle, 200)
    assert len(resampled) == 200

    lengths = [
        math.hypot(resampled[i + 1][0] - resampled[i][0], resampled[i + 1][1] - resampled[i][1])
        for i in range(len(resampled) - 1)
    ]
    mean = sum(lengths) / len(lengths)
    assert max(lengths) - min(lengths) < mean * 0.01, "points must be evenly spaced"


def test_deviation_is_zero_for_identical_outline() -> None:
    points = _square_outline()
    assert deviation_from_outline(points, points) == pytest.approx(0.0)


def test_deviation_measures_distance_to_nearest_sample() -> None:
    """The deviation is measured to the nearest outline *sample*."""
    outline = _square_outline()
    # The centre of the square is 707.1 units from the nearest corner sample.
    assert deviation_from_outline([(500.0, 500.0)], outline) == pytest.approx(
        707.106, abs=0.01
    )


class _StubClient:
    """Minimal stand-in for :class:`OpenF1Client` (no network)."""

    def __init__(self, trace: list[dict], laps: list[dict]) -> None:
        self._trace = trace
        self._laps = laps

    async def get_location(self, session_key: int, driver_number: int | None = None, **kw):
        return self._trace

    async def get_laps(self, session_key: int, driver_number: int | None = None):
        return self._laps


def _synthetic_lap(count: int = 400) -> tuple[list[dict], list[dict]]:
    """A closed square lap plus the pit-lane excursion that used to break it."""
    from datetime import datetime, timedelta

    start = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    trace: list[dict] = []
    laps: list[dict] = []

    # Pre-race: the car sits in the garage at the origin for a while.
    for i in range(40):
        trace.append(
            {
                "date": (start + timedelta(seconds=i * 0.25)).isoformat(),
                "x": 0.0,
                "y": 0.0,
                "z": 0.0,
                "driver_number": 1,
            }
        )

    # Lap 1: a full closed square.
    for i in range(count):
        angle = 2 * math.pi * i / count
        trace.append(
            {
                "date": (start + timedelta(seconds=10 + i * 0.25)).isoformat(),
                "x": 1000.0 * math.cos(angle),
                "y": 1000.0 * math.sin(angle),
                "z": 0.0,
                "driver_number": 1,
            }
        )
    laps.append(
        {
            "lap_number": 1,
            "date_start": (start + timedelta(seconds=10)).isoformat(),
            "lap_duration": 96.0,
            "driver_number": 1,
        }
    )

    # A pit-lane excursion that jumps far away from the circuit.
    for i in range(40):
        trace.append(
            {
                "date": (start + timedelta(seconds=110 + i * 0.25)).isoformat(),
                "x": -9000.0 + i * 10,
                "y": -9000.0,
                "z": 0.0,
                "driver_number": 1,
            }
        )
    laps.append(
        {
            "lap_number": 2,
            "date_start": (start + timedelta(seconds=110)).isoformat(),
            "lap_duration": 110.0,
            "driver_number": 1,
        }
    )
    return trace, laps


@pytest.mark.asyncio
async def test_outline_derivation_picks_the_clean_lap_and_closes() -> None:
    """The outline must be a closed circuit, not a path with pit-lane spurs."""
    trace, laps = _synthetic_lap()
    client = _StubClient(trace, laps)

    canonical = Outline(
        circuit="Test",
        year=2024,
        points=[
            (1000.0 * math.cos(2 * math.pi * i / 100), 1000.0 * math.sin(2 * math.pi * i / 100))
            for i in range(100)
        ],
    )

    outline = await outline_from_session(
        client, 1, "Test", 2024, driver_number=1, canonical=canonical
    )

    assert outline.source == "openf1.location"
    assert len(outline.points) > 100
    # Closed: the last point equals the first.
    assert outline.points[0] == outline.points[-1]
    # And it follows the canonical circuit rather than the pit-lane excursion.
    assert outline.metadata["deviation_from_canonical"] < 400.0
    assert max(abs(p[0]) for p in outline.points) < 2000.0


@pytest.mark.asyncio
async def test_outline_derivation_fails_loudly_when_no_lap_matches() -> None:
    """A mismatching trace must raise instead of writing a broken outline."""
    trace, laps = _synthetic_lap()
    client = _StubClient(trace, laps)

    canonical = Outline(
        circuit="Test",
        year=2024,
        points=[(50_000.0 + i, 50_000.0) for i in range(50)],
    )

    with pytest.raises(TrackGeometryError):
        await outline_from_session(
            client, 1, "Test", 2024, driver_number=1, canonical=canonical
        )
