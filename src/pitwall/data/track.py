"""Circuit geometry: canonical outlines, clean-lap derivation and projection.

Why this module exists
----------------------
The Pit Wall frontend draws two things in the same SVG: the circuit outline
and the live driver dots.  If those two use different coordinate systems the
dots float off the track, which is exactly the bug this module removes.

There is a single source of truth for coordinates: **OpenF1 ``/location``**.
Everything else is derived from it or validated against it.

Two coordinate facts were established by measurement (see
``docs/LEARNING_LOG.md``):

1. ``/location`` ``x``/``y`` and FastF1's canonical circuit outline
   (``fastf1.mvapi``, the same source FastF1 uses for ``circuit_info``) live in
   **the same coordinate system**.  On the 2024 Austin race trace the
   canonical corners sit within 1-102 feed units of the driven line, on a
   track whose own bounding box spans 18,136 units.
2. The raw ``y`` axis points **up** while SVG's ``y`` axis points **down**.
   The canonical outline is traversed counter-clockwise (COTA is one of the
   handful of counter-clockwise circuits), which is only true when ``y`` is
   read as pointing up.  Rendering the raw numbers straight into SVG therefore
   produces a **mirrored** circuit, so the projection flips ``y``.

Everything the frontend draws is produced by :meth:`TrackProjection.project`,
so outline and dots can never drift apart.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pitwall.data.openf1 import OpenF1Client

#: Feed units per metre, measured on the 2024 Austin race (54,278 feed units
#: for a 5,513 m lap).  Retained for reporting; the renderer is scale-free.
UNITS_PER_METRE = 9.845

#: A driven trace is rejected if it strays further than this from the
#: canonical outline (feed units).  Measured noise on real data is <150u.
TRACE_TOLERANCE = 400.0


class TrackGeometryError(RuntimeError):
    """Raised when a usable circuit outline cannot be produced."""


@dataclass(frozen=True)
class Outline:
    """A circuit outline in OpenF1 feed coordinates."""

    circuit: str
    year: int
    points: list[tuple[float, float]]
    corners: list[dict[str, Any]] = field(default_factory=list)
    start_finish: tuple[float, float] | None = None
    source: str = "unknown"
    session_key: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "circuit": self.circuit,
            "year": self.year,
            "points": [{"x": x, "y": y} for x, y in self.points],
            "corners": self.corners,
            "start_finish": (
                {"x": self.start_finish[0], "y": self.start_finish[1]}
                if self.start_finish
                else None
            ),
            "source": self.source,
            "session_key": self.session_key,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Outline:
        return cls(
            circuit=data["circuit"],
            year=int(data["year"]),
            points=[(float(p["x"]), float(p["y"])) for p in data["points"]],
            corners=list(data.get("corners") or []),
            start_finish=(
                (float(data["start_finish"]["x"]), float(data["start_finish"]["y"]))
                if data.get("start_finish")
                else None
            ),
            source=data.get("source", "unknown"),
            session_key=data.get("session_key"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class TrackProjection:
    """Maps feed coordinates onto an SVG viewBox.

    The same instance must be used for the outline *and* for the driver dots.
    """

    min_x: float
    max_x: float
    min_y: float
    max_y: float
    view_width: float = 1000.0
    view_height: float = 750.0
    padding: float = 28.0
    flip_y: bool = True

    @classmethod
    def from_outline(
        cls, points: Sequence[tuple[float, float]], **kwargs: Any
    ) -> TrackProjection:
        if not points:
            raise TrackGeometryError("cannot project an empty outline")
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return cls(min_x=min(xs), max_x=max(xs), min_y=min(ys), max_y=max(ys), **kwargs)

    @property
    def span_x(self) -> float:
        return max(self.max_x - self.min_x, 1e-9)

    @property
    def span_y(self) -> float:
        return max(self.max_y - self.min_y, 1e-9)

    @property
    def scale(self) -> float:
        """Uniform scale so the true track aspect ratio is preserved."""
        usable_w = self.view_width - 2 * self.padding
        usable_h = self.view_height - 2 * self.padding
        return min(usable_w / self.span_x, usable_h / self.span_y)

    def project(self, x: float, y: float) -> tuple[float, float]:
        """Project a feed coordinate into the SVG viewBox."""
        scale = self.scale
        offset_x = self.padding + (self.view_width - 2 * self.padding - self.span_x * scale) / 2
        offset_y = self.padding + (self.view_height - 2 * self.padding - self.span_y * scale) / 2
        view_x = offset_x + (x - self.min_x) * scale
        if self.flip_y:
            view_y = offset_y + (self.max_y - y) * scale
        else:
            view_y = offset_y + (y - self.min_y) * scale
        return round(view_x, 2), round(view_y, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_x": self.min_x,
            "max_x": self.max_x,
            "min_y": self.min_y,
            "max_y": self.max_y,
            "view_width": self.view_width,
            "view_height": self.view_height,
            "padding": self.padding,
            "flip_y": self.flip_y,
            "scale": self.scale,
        }


# ----------------------------------------------------------------------
# canonical outline access
# ----------------------------------------------------------------------
def _fastf1_circuit(circuit_key: int, year: int) -> dict[str, Any] | None:
    """Fetch the canonical circuit description used by FastF1 itself."""
    try:
        from fastf1.mvapi.api import get_circuit
    except ImportError:  # pragma: no cover - fastf1 is a hard dependency
        return None
    try:
        return get_circuit(year=year, circuit_key=circuit_key)
    except Exception:  # noqa: BLE001 - upstream/network failure must not crash
        return None


def load_canonical_outline(
    circuit_key: int, circuit: str, year: int
) -> Outline | None:
    """The canonical centreline plus numbered corners for a circuit."""
    data = _fastf1_circuit(circuit_key, year)
    if not data:
        return None
    xs = data.get("x") or []
    ys = data.get("y") or []
    if len(xs) < 10 or len(xs) != len(ys):
        return None
    points = [(float(x), float(y)) for x, y in zip(xs, ys)]

    corners: list[dict[str, Any]] = []
    for corner in data.get("corners") or []:
        pos = corner.get("trackPosition") or {}
        if "x" not in pos or "y" not in pos:
            continue
        corners.append(
            {
                "number": int(corner.get("number", 0)),
                "letter": corner.get("letter") or "",
                "x": float(pos["x"]),
                "y": float(pos["y"]),
            }
        )
    corners.sort(key=lambda c: (c["number"], c["letter"]))

    return Outline(
        circuit=circuit,
        year=year,
        points=points,
        corners=corners,
        start_finish=points[0] if points else None,
        source="fastf1.circuit_info",
        metadata={
            "rotation": data.get("rotation", 0),
            "circuit_key": circuit_key,
            "point_count": len(points),
        },
    )


def deviation_from_outline(
    points: Iterable[tuple[float, float]], outline: Sequence[tuple[float, float]]
) -> float:
    """Mean distance of ``points`` from the nearest outline point (feed units)."""
    reference = np_array(outline)
    if len(reference) == 0:
        return float("inf")
    total = 0.0
    count = 0
    for x, y in points:
        dx = reference[:, 0] - x
        dy = reference[:, 1] - y
        total += float(math.sqrt(float((dx * dx + dy * dy).min())))
        count += 1
    return total / max(count, 1)


def np_array(points: Sequence[tuple[float, float]]) -> Any:
    """Small helper so numpy stays an optional import for pure-geometry users."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return [(x, y) for x, y in points]
    return np.asarray(points, dtype=float)


# ----------------------------------------------------------------------
# outline derivation from a real session
# ----------------------------------------------------------------------
async def _lap_windows(
    client: OpenF1Client, session_key: int, driver_number: int
) -> list[tuple[datetime, datetime]]:
    """Lap start/end timestamps taken from the real ``/laps`` feed."""
    laps = await client.get_laps(session_key, driver_number=driver_number)
    starts = sorted(
        datetime.fromisoformat(lap["date_start"]) for lap in laps if lap.get("date_start")
    )
    return [(starts[i], starts[i + 1]) for i in range(len(starts) - 1)]


async def outline_from_session(
    client: OpenF1Client,
    session_key: int,
    circuit: str,
    year: int,
    *,
    driver_number: int = 1,
    canonical: Outline | None = None,
    max_points: int = 900,
) -> Outline:
    """Derive a clean circuit outline from a real driven session.

    The previous implementation selected the *longest* contiguous run of
    ``/location`` samples.  A session trace also contains the garage, the pit
    lane and out-lap excursions, so that run is not a circuit: the resulting
    path never closed (11,509 feed units of gap on an 18,136 unit track) and
    drew long chords straight across the infield.

    Here each *racing lap* is isolated using the real ``/laps`` feed, and the
    lap whose trace best matches the canonical outline wins.  A racing lap is
    a closed circuit, so the result closes by construction.
    """
    trace = await client.get_location(session_key, driver_number=driver_number)
    if not trace:
        raise TrackGeometryError(f"no /location data for session {session_key}")

    rows: list[tuple[datetime, float, float]] = []
    for row in trace:
        if row.get("x") is None or row.get("y") is None:
            continue
        x, y = float(row["x"]), float(row["y"])
        if x == 0.0 and y == 0.0:
            continue  # feed sentinel for "no data"
        rows.append((datetime.fromisoformat(row["date"]), x, y))
    rows.sort(key=lambda r: r[0])
    if len(rows) < 100:
        raise TrackGeometryError(f"session {session_key} has too few usable samples")

    windows = await _lap_windows(client, session_key, driver_number)
    candidates: list[list[tuple[float, float]]] = []
    for start, end in windows:
        segment = [(x, y) for ts, x, y in rows if start <= ts < end]
        if len(segment) >= 100:
            candidates.append(segment)
    if not candidates:
        candidates = [[(x, y) for _, x, y in rows]]

    if canonical is not None and canonical.points:
        scored = [
            (deviation_from_outline(segment, canonical.points), index, segment)
            for index, segment in enumerate(candidates)
        ]
        scored.sort(key=lambda item: (item[0], item[1]))
        deviation, best_index, best = scored[0]
        if deviation > TRACE_TOLERANCE:
            raise TrackGeometryError(
                f"no lap of session {session_key} matches the canonical outline "
                f"(best deviation {deviation:.0f} > {TRACE_TOLERANCE:.0f} feed units)"
            )
    else:
        deviation = float("nan")
        best_index, best = 0, candidates[0]

    points = _resample(best, max_points)
    if points and points[0] != points[-1]:
        points.append(points[0])  # close the loop

    corners = list(canonical.corners) if canonical is not None else []
    return Outline(
        circuit=circuit,
        year=year,
        points=points,
        corners=corners,
        start_finish=points[0] if points else None,
        source="openf1.location",
        session_key=session_key,
        metadata={
            "driver_number": driver_number,
            "lap_index": best_index,
            "lap_count_candidates": len(candidates),
            "deviation_from_canonical": (
                None if math.isnan(deviation) else round(deviation, 1)
            ),
            "tolerance": TRACE_TOLERANCE,
        },
    )


def _resample(points: Sequence[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    """Resample a closed path to ``count`` evenly spaced points."""
    if len(points) < 2:
        return list(points)
    cumulative = [0.0]
    for index in range(1, len(points)):
        step = math.hypot(
            points[index][0] - points[index - 1][0],
            points[index][1] - points[index - 1][1],
        )
        cumulative.append(cumulative[-1] + step)
    total = cumulative[-1]
    if total <= 0:
        return list(points[:count])
    step = total / count
    out: list[tuple[float, float]] = []
    index = 0
    for i in range(count):
        target = i * step
        while index < len(cumulative) - 2 and cumulative[index + 1] < target:
            index += 1
        span = cumulative[index + 1] - cumulative[index]
        frac = 0.0 if span <= 0 else (target - cumulative[index]) / span
        x0, y0 = points[index]
        x1, y1 = points[index + 1]
        out.append((x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac))
    return out


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------
def outline_path(directory: str | Path, circuit: str, year: int) -> Path:
    return Path(directory) / f"{circuit.lower()}_{year}.json"


def save_outline(outline: Outline, directory: str | Path = "data/track_outlines") -> Path:
    path = outline_path(directory, outline.circuit, outline.year)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(outline.to_dict(), handle, indent=1)
    tmp.replace(path)
    return path


def load_outline(
    directory: str | Path, circuit: str, year: int
) -> Outline | None:
    path = outline_path(directory, circuit, year)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return Outline.from_dict(json.load(handle))


def resolve_outline(
    directory: str | Path,
    circuit: str,
    year: int,
    *,
    circuit_key: int | None = None,
) -> Outline | None:
    """The outline for a circuit, building it on demand if it is missing.

    Every race weekend is a different circuit, so the layout has to follow
    the calendar rather than be edited by hand.  The outline is looked up by
    the circuit's OpenF1 ``circuit_key`` and the season's year, which is
    exactly the pair FastF1 itself uses, and the result is cached on disk so
    it is only fetched once.
    """
    cached = load_outline(directory, circuit, year)
    if cached is not None:
        return cached
    if circuit_key is None:
        return None
    built = load_canonical_outline(circuit_key, circuit, year)
    if built is None:
        return None
    save_outline(built, directory)
    return built
