"""Report whether the pit wall is following a real session.

Run this while a session is on:

    python scripts/check_live.py

It prints what the running backend knows about the live feed - whether the
hub is connected and subscribed, which session it is showing, which topics
have arrived and what the map is built from - so a silent feed can be told
apart from a session that is simply not running.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = "http://localhost:8111"


def _get(path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"the backend is not answering at {BASE}: {exc}") from exc


def _value(value: Any) -> str:
    return "-" if value is None or value == "" else str(value)


def main() -> int:
    status = _get("/api/live/status")
    session = _get("/api/sessions/current")
    outline = _get("/api/outline/current")
    frame = _get("/api/timing")

    print(f"mode            {status.get('mode')}")
    print(f"hub connected   {status.get('hub_connected')}")
    print(f"hub subscribed  {status.get('hub_subscribed')}")
    print(f"hub error       {_value(status.get('hub_error'))}")
    print(
        f"frames          {status.get('frames_received')} "
        f"(bad: {status.get('bad_frames')})"
    )
    print(f"last frame      {_value(status.get('last_frame_at'))}")

    live = status.get("session") or {}
    print(
        f"session         {_value(live.get('name'))} "
        f"({_value(live.get('type'))}) key {_value(live.get('session_key'))}"
    )
    print(
        f"showing         {session.get('circuit')} {session.get('year')} "
        f"{session.get('session_type')}"
    )
    print(f"source          {session.get('source')}")

    topics = status.get("topics") or {}
    summary = ", ".join(f"{name} x{count}" for name, count in sorted(topics.items()))
    print(f"topics          {summary or '-'}")
    print(f"drivers         {status.get('drivers')}")
    print(f"radio calls     {status.get('radio_calls')}")
    print(f"race control    {status.get('messages')}")
    print(f"weather rows    {status.get('weather_reports')}")

    points = outline.get("points")
    print(
        f"outline         {_value(outline.get('source') or outline.get('reason'))} "
        f"({len(points) if points else 0} points)"
    )

    drivers = frame.get("timing") or []
    if drivers:
        print("\nfirst cars:")
        for driver in drivers[:8]:
            gap = _value(driver.get("gap_to_leader"))
            print(
                f"  P{_value(driver.get('position')):>2} {driver.get('code'):>3} "
                f"lap {_value(driver.get('lap')):>2} "
                f"tyre {_value(driver.get('compound')):>10} "
                f"age {_value(driver.get('tyre_age')):>3} gap {gap:>8}"
            )

    problems = []
    if not status.get("hub_connected"):
        problems.append("the hub is not connected (it throttles; it retries by itself)")
    if status.get("hub_connected") and not status.get("hub_subscribed"):
        problems.append("connected but not subscribed - the feed would be silent")
    if status.get("mode") == "LIVE" and not status.get("drivers"):
        problems.append("live, but no drivers have been parsed yet")
    if status.get("bad_frames"):
        problems.append(f"{status['bad_frames']} frames could not be decoded")

    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if status.get("mode") != "LIVE":
        print("\nno session is running: the replay is being shown, as intended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
