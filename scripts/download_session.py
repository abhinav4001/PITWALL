"""Download a full OpenF1 session into the local replay cache.

Usage
-----
    python scripts/download_session.py --session 9617
    python scripts/download_session.py --circuit Austin --year 2024 --type Race

The download is throttled (OpenF1 rate-limits aggressively) and resumable:
files that already exist in the cache are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pitwall.data.openf1 import OpenF1Client  # noqa: E402
from pitwall.data.session_pack import DEFAULT_DIR, SessionPack, download_session  # noqa: E402


async def resolve_session(
    client: OpenF1Client, circuit: str | None, year: int | None, session_type: str | None
) -> int:
    params: dict[str, object] = {}
    if circuit:
        params["circuit_short_name"] = circuit
    if year:
        params["year"] = year
    sessions = await client.get_sessions(**params)
    if not sessions:
        raise SystemExit(f"no sessions found for {params}")

    if session_type:
        sessions = [s for s in sessions if s.get("session_type") == session_type]
    if not sessions:
        raise SystemExit(f"no {session_type} session found for {params}")

    sessions.sort(key=lambda s: s["date_start"])
    chosen = sessions[-1]
    print(
        f"selected session_key={chosen['session_key']} "
        f"{chosen['circuit_short_name']} {chosen['session_name']} ({chosen['date_start']})"
    )
    return int(chosen["session_key"])


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=int, default=None, help="explicit session_key")
    parser.add_argument("--circuit", default=None)
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--type", default="Race", help="session type filter")
    parser.add_argument("--base", default=str(DEFAULT_DIR))
    args = parser.parse_args()

    async with OpenF1Client(min_interval=1.0) as client:
        session_key = args.session
        if session_key is None:
            session_key = await resolve_session(client, args.circuit, args.year, args.type)

        if SessionPack.exists(session_key, args.base):
            print(f"session {session_key} already cached at {args.base}")
            return 0

        print(f"downloading session {session_key} ...")
        pack = await download_session(client, session_key, args.base)
        print(f"done -> {pack.directory}")
        print(f"  drivers: {len(pack.drivers)}")
        print(f"  circuit: {pack.meta.get('circuit_short_name')} {pack.meta.get('year')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
