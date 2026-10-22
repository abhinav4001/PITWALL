"""Throttled, cached client for the OpenF1 API.

Every OpenF1 request in PitWall goes through this module so the upstream API is
never hammered (ground rule GR8).  A single client instance owns:

* one shared :class:`httpx.AsyncClient` (connection pooling),
* a minimum-interval throttle (the free tier 429s after ~4 rapid requests),
* exponential backoff with jitter on ``429``/``5xx``,
* an on-disk response cache so repeated historical queries cost nothing.

Measured upstream behaviour that this module encodes (2026-10-22):

* ``date>=`` / ``date<=`` are **rejected** (HTTP 404).  ``date>`` and ``date<``
  are supported and work correctly when passed through ``httpx``'s ``params``
  dict (the client percent-encodes them, and OpenF1 accepts the encoding).
* Asking for a whole session of ``/location`` or ``/car_data`` for *all*
  drivers returns HTTP 422 ("too much data at once").  Query **per driver**
  or use a **date window** instead.
* A date window returns **all** drivers in a single request, which is the
  correct way to obtain per-second positions for the whole field.
* Team radio lives at ``/team_radio``, **not** ``/radio``.  Each row carries a
  ``recording_url`` pointing at the official F1 static archive; the archive
  files are still served without authentication even though the feed's index
  and live socket are not (see :mod:`pitwall.data.radio`).
* Weather lives at ``/weather`` and is keyed on the **meeting**, not the
  session, so one request covers a whole weekend; each reading carries the
  ``session_key`` it belongs to.

The module never invents data: if the upstream API cannot answer, the caller
receives ``None`` and is expected to surface an explicit ``available: false``
payload (ground rule GR1).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openf1.org/v1"

#: Endpoints that return a very large payload for a whole session and
#: therefore must always be queried per driver or windowed.
LARGE_ENDPOINTS = frozenset({"location", "car_data"})


class OpenF1Error(RuntimeError):
    """Raised when OpenF1 cannot satisfy a request."""


class _Throttle:
    """Serialises requests and enforces a minimum interval between them."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval

    def penalise(self, seconds: float) -> None:
        """Push the next request further out after a 429."""
        self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


class OpenF1Client:
    """Async, rate-limited, disk-cached OpenF1 client."""

    def __init__(
        self,
        *,
        cache_dir: str | Path = "data/cache/openf1",
        min_interval: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 4,
        user_agent: str = "pitwall/1.0 (+https://github.com/pitwall)",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self._throttle = _Throttle(min_interval)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def __aenter__(self) -> OpenF1Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _cache_path(self, endpoint: str, params: dict[str, Any]) -> Path:
        payload = json.dumps(params, sort_keys=True, default=str)
        digest = hashlib.sha1(f"{endpoint}|{payload}".encode()).hexdigest()
        return self.cache_dir / f"{endpoint}_{digest[:20]}.json"

    async def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        ttl: float | None = None,
        use_cache: bool = True,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        """Perform a GET against OpenF1, returning parsed JSON or ``None``.

        ``None`` is returned when the API is unreachable or has no data; it is
        never replaced by synthetic rows.
        """
        if self._closed:
            raise OpenF1Error("client is closed")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cache_file = self._cache_path(endpoint, params)

        if use_cache and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if ttl is None or age <= ttl:
                try:
                    with cache_file.open(encoding="utf-8") as handle:
                        return json.load(handle)
                except (OSError, json.JSONDecodeError):
                    logger.warning("openf1: unreadable cache %s", cache_file)

        delay = 2.0
        for attempt in range(self.max_retries):
            await self._throttle.acquire()
            try:
                response = await self._client.get(f"{BASE_URL}/{endpoint}", params=params)
            except httpx.HTTPError as exc:
                logger.warning("openf1: %s failed (%s)", endpoint, exc)
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 429:
                self._throttle.penalise(delay)
                logger.warning("openf1: 429 on %s, backing off %.1fs", endpoint, delay)
                await asyncio.sleep(delay + random.uniform(0, 0.5))
                delay *= 2
                continue

            if response.status_code == 404:
                # No data for this query (e.g. a session that never ran).
                return None

            if response.status_code >= 500:
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if response.status_code != 200:
                logger.warning("openf1: HTTP %s on %s", response.status_code, endpoint)
                return None

            try:
                data = response.json()
            except ValueError:
                logger.warning("openf1: non-JSON response on %s", endpoint)
                return None

            if use_cache:
                try:
                    with cache_file.open("w", encoding="utf-8") as handle:
                        json.dump(data, handle)
                except OSError:
                    logger.warning("openf1: could not write cache %s", cache_file)
            return data

        logger.warning("openf1: giving up on %s after %s attempts", endpoint, self.max_retries)
        return None

    async def _rows(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        data = await self._request(endpoint, params, ttl=ttl)
        if isinstance(data, list):
            return data
        return []

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def get_sessions(
        self,
        *,
        year: int | None = None,
        circuit_short_name: str | None = None,
        session_key: int | None = None,
        meeting_key: int | None = None,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        return await self._rows(
            "sessions",
            {
                "year": year,
                "circuit_short_name": circuit_short_name,
                "session_key": session_key,
                "meeting_key": meeting_key,
            },
            ttl=ttl,
        )

    async def get_drivers(
        self, session_key: int, *, driver_number: int | None = None
    ) -> list[dict[str, Any]]:
        return await self._rows(
            "drivers", {"session_key": session_key, "driver_number": driver_number}
        )

    async def get_location(
        self,
        session_key: int,
        *,
        driver_number: int | None = None,
        date_gt: str | None = None,
        date_lt: str | None = None,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        """Car positions.

        Without ``driver_number`` this returns every driver that reported in
        the window, which is the cheapest way to obtain the whole field at
        once.
        """
        return await self._rows(
            "location",
            {
                "session_key": session_key,
                "driver_number": driver_number,
                "date>": date_gt,
                "date<": date_lt,
            },
            ttl=ttl,
        )

    async def get_car_data(
        self,
        session_key: int,
        *,
        driver_number: int | None = None,
        date_gt: str | None = None,
        date_lt: str | None = None,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        """Telemetry: speed, throttle, brake, gear, rpm and DRS state."""
        return await self._rows(
            "car_data",
            {
                "session_key": session_key,
                "driver_number": driver_number,
                "date>": date_gt,
                "date<": date_lt,
            },
            ttl=ttl,
        )

    async def get_laps(
        self, session_key: int, *, driver_number: int | None = None
    ) -> list[dict[str, Any]]:
        return await self._rows(
            "laps", {"session_key": session_key, "driver_number": driver_number}
        )

    async def get_intervals(
        self,
        session_key: int,
        *,
        driver_number: int | None = None,
        date_gt: str | None = None,
        date_lt: str | None = None,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        """Gaps: ``interval`` is the gap to the car ahead, ``gap_to_leader`` to P1."""
        return await self._rows(
            "intervals",
            {
                "session_key": session_key,
                "driver_number": driver_number,
                "date>": date_gt,
                "date<": date_lt,
            },
            ttl=ttl,
        )

    async def get_stints(
        self, session_key: int, *, driver_number: int | None = None
    ) -> list[dict[str, Any]]:
        return await self._rows(
            "stints", {"session_key": session_key, "driver_number": driver_number}
        )

    async def get_position(
        self,
        session_key: int,
        *,
        driver_number: int | None = None,
        date_gt: str | None = None,
        date_lt: str | None = None,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        """Race order over time.  Contains no coordinates."""
        return await self._rows(
            "position",
            {
                "session_key": session_key,
                "driver_number": driver_number,
                "date>": date_gt,
                "date<": date_lt,
            },
            ttl=ttl,
        )

    async def get_weather(
        self,
        *,
        meeting_key: int | None = None,
        session_key: int | None = None,
        date_gt: str | None = None,
        date_lt: str | None = None,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        """The weather over the track, updated every minute.

        This is the **real** weather feed: air and track temperature,
        humidity, pressure, wind speed and direction, and rainfall.  It is
        keyed on the *meeting* (a whole weekend), so it covers every session
        of the weekend and the ``session_key`` field says which one each
        reading belongs to.
        """
        return await self._rows(
            "weather",
            {
                "meeting_key": meeting_key,
                "session_key": session_key,
                "date>": date_gt,
                "date<": date_lt,
            },
            ttl=ttl,
        )

    async def get_team_radio(
        self,
        session_key: int,
        *,
        driver_number: int | None = None,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        """Team radio calls.

        The endpoint is ``/team_radio`` (not ``/radio``) and each row carries
        a ``recording_url`` pointing at the official F1 static archive.  F1
        does not release radio for every session: coverage fell sharply
        during 2026 and most events carry none at all.
        """
        return await self._rows(
            "team_radio",
            {"session_key": session_key, "driver_number": driver_number},
            ttl=ttl,
        )

    async def get_race_control(self, session_key: int) -> list[dict[str, Any]]:
        return await self._rows("race_control", {"session_key": session_key})
