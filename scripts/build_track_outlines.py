"""Build real circuit outlines from OpenF1 telemetry.

Usage
-----
    python scripts/build_track_outlines.py                 # all configured circuits
    python scripts/build_track_outlines.py --circuit Austin --year 2024

The outline is derived from the *driven line* of a real session (OpenF1
``/location``) and validated against FastF1's canonical circuit description.
Both live in the same coordinate system, which is what lets the frontend draw
driver dots exactly on the outline.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pitwall.data.openf1 import OpenF1Client  # noqa: E402
from pitwall.data.track import (  # noqa: E402
    Outline,
    TrackGeometryError,
    load_canonical_outline,
    outline_from_session,
    save_outline,
)

#: Circuits to build.  ``--auto`` replaces this with every circuit on the
#: calendar, so a new race weekend needs no manual change: the outline is
#: looked up by the OpenF1 ``circuit_key`` for the season, which is the same
#: key FastF1 uses for its own circuit maps.
CIRCUITS: dict[str, tuple[int, int, int]] = {
    "Austin": (9, 9617, 2024),
    "Silverstone": (2, 9558, 2024),
    "Monza": (39, 9590, 2024),
    "Spa": (7, 9574, 2024),
}

OUT_DIR = "data/track_outlines"


async def discover_circuits(
    client: OpenF1Client, year: int
) -> dict[str, tuple[int, int, int]]:
    """Every circuit on the calendar, with a session to derive its outline from.

    The session chosen is the most recent *race* of that circuit in the
    season, because a race contains a full flying lap of the layout.
    """
    sessions = await client.get_sessions(year=year)
    best: dict[str, dict[str, object]] = {}
    for session in sessions:
        name = session.get("circuit_short_name")
        if not name or session.get("session_type") != "Race":
            continue
        current = best.get(name)
        if current is None or session["date_start"] > str(current["date_start"]):
            best[name] = session
    return {
        name: (int(session["circuit_key"]), int(session["session_key"]), year)
        for name, session in best.items()
    }


async def build_one(
    client: OpenF1Client, circuit: str, circuit_key: int, session_key: int, year: int
) -> Outline | None:
    print(f"{circuit} (session_key={session_key}, year={year})...")

    canonical = load_canonical_outline(circuit_key, circuit, year)
    if canonical is None:
        print(f"  WARN no canonical outline for {circuit} {year}; deriving outline alone")
    else:
        print(f"  canonical outline: {len(canonical.points)} pts, {len(canonical.corners)} corners")

    try:
        outline = await outline_from_session(
            client,
            session_key,
            circuit,
            year,
            driver_number=1,
            canonical=canonical,
        )
    except TrackGeometryError as exc:
        print(f"  FAIL {exc}")
        return None

    deviation = outline.metadata.get("deviation_from_canonical")
    if deviation is not None:
        print(
            f"  derived {len(outline.points)} pts from lap "
            f"{outline.metadata['lap_index']} "
            f"(max deviation {deviation} feed units)"
        )
        if deviation > 400:
            print("  FAIL derived outline deviates too far from canonical")
            return None
    path = save_outline(outline, OUT_DIR)
    print(f"  OK -> {path}")
    return outline


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--circuit", default=None, help="circuit name (default: all)")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument(
        "--auto",
        action="store_true",
        help="discover every circuit on the calendar for the year",
    )
    args = parser.parse_args()

    async with OpenF1Client(min_interval=1.0) as client:
        if args.auto:
            year = args.year or 2024
            targets = await discover_circuits(client, year)
            print(f"discovered {len(targets)} circuits for {year}\n")
        else:
            targets = {
                name: spec
                for name, spec in CIRCUITS.items()
                if args.circuit is None or name.lower() == args.circuit.lower()
            }
            if args.year is not None:
                targets = {
                    name: (key, session, args.year)
                    for name, (key, session, _) in targets.items()
                }
        if not targets:
            print(f"unknown circuit {args.circuit!r}; known: {sorted(CIRCUITS)}")
            return 2

        failures: list[str] = []
        for circuit, (circuit_key, session_key, year) in targets.items():
            outline = await build_one(client, circuit, circuit_key, session_key, year)
            if outline is None:
                failures.append(circuit)
            await asyncio.sleep(2)

    if failures:
        print("\nFAILED: " + ", ".join(failures))
        return 1
    print("\nAll circuit outlines built and validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
