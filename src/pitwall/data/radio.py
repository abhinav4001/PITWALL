"""Team radio: the calls and their audio.

How this works, and what had to be measured to get it right:

* OpenF1's endpoint is ``/team_radio`` - **not** ``/radio``, which returns
  404 and looks like "the free tier has no radio".  The free tier does carry
  radio for the sessions F1 published it for.
* Each row gives a ``recording_url`` pointing at the official F1 static
  archive.  Those **audio files are still served without authentication**,
  even though the feed's index (``/static/<year>/Index.json``) and its live
  SignalR socket both answer ``HTTP 401``.  So the recordings play in the
  browser without a proxy.
* The app plays the clips themselves.  No speech recognition is involved:
  the words are the drivers' and engineers', not a machine's guess.

F1 does not release radio for every session: coverage fell sharply during
2026 and most events carry none at all.  When there is nothing, the API says
so plainly rather than showing an empty box that looks broken.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pitwall.data.openf1 import OpenF1Client


@dataclass
class RadioMessage:
    """One radio transmission."""

    date: str | None
    driver_number: int | None
    team: str | None
    text: str
    recording_url: str | None = None
    duration: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "driver_number": self.driver_number,
            "team": self.team,
            "text": self.text,
            "recording_url": self.recording_url,
            "duration": self.duration,
        }


async def fetch_team_radio(
    client: OpenF1Client, session_key: int
) -> list[RadioMessage]:
    """The radio calls OpenF1 published for a session."""
    rows = await client.get_team_radio(session_key)
    messages: list[RadioMessage] = []
    for row in rows:
        message = radio_message_from_row(row)
        if message is not None:
            messages.append(message)
    messages.sort(key=lambda message: str(message.date or ""))
    return messages


def radio_message_from_row(row: dict[str, Any]) -> RadioMessage | None:
    """One cached radio row as a :class:`RadioMessage`."""
    url = (row.get("recording_url") or "").strip()
    if not url:
        return None
    return RadioMessage(
        date=row.get("date"),
        driver_number=(
            int(row["driver_number"]) if row.get("driver_number") is not None else None
        ),
        team=row.get("team_name") or row.get("team") or None,
        text=(row.get("transcript") or "").strip(),
        recording_url=url,
    )
