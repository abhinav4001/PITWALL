"""Tests for the OpenF1 client.

The client is exercised against a mock transport, never against the live API
(GR8).  The behaviours under test are the ones that broke the old
implementation: request throttling, 429 backoff and correct query parameters.
"""

import httpx
import pytest

from pitwall.data.openf1 import OpenF1Client


class _Recorder:
    """A mock transport that records requests and replays canned responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(200, json=[])


def _client(recorder: _Recorder, tmp_path, **kwargs) -> OpenF1Client:
    client = OpenF1Client(cache_dir=tmp_path, min_interval=0.0, **kwargs)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        timeout=5.0,
    )
    return client


@pytest.mark.asyncio
async def test_location_query_uses_window_operators(tmp_path) -> None:
    recorder = _Recorder([httpx.Response(200, json=[{"driver_number": 1, "x": 1, "y": 2}])])
    async with _client(recorder, tmp_path) as client:
        rows = await client.get_location(
            9617, date_gt="2024-10-20T19:00:00Z", date_lt="2024-10-20T19:00:10Z"
        )

    assert len(rows) == 1
    request = recorder.requests[0]
    assert request.url.params["session_key"] == "9617"
    assert "date>" in request.url.params
    assert "date<" in request.url.params


@pytest.mark.asyncio
async def test_client_retries_on_429_then_succeeds(tmp_path) -> None:
    recorder = _Recorder(
        [
            httpx.Response(429, json={"detail": "rate limited"}),
            httpx.Response(200, json=[{"driver_number": 1}]),
        ]
    )
    async with _client(recorder, tmp_path, max_retries=3) as client:
        rows = await client.get_laps(9617)

    assert len(rows) == 1
    assert len(recorder.requests) == 2, "the client must retry after a 429"


@pytest.mark.asyncio
async def test_client_returns_none_for_missing_data(tmp_path) -> None:
    recorder = _Recorder([httpx.Response(404, json={"detail": "No results found."})])
    async with _client(recorder, tmp_path) as client:
        rows = await client.get_stints(9617, driver_number=999)

    assert rows == [], "a 404 must yield no rows, never invented ones"


@pytest.mark.asyncio
async def test_responses_are_cached_on_disk(tmp_path) -> None:
    recorder = _Recorder([httpx.Response(200, json=[{"driver_number": 1}])])
    async with _client(recorder, tmp_path) as client:
        await client.get_drivers(9617)
        await client.get_drivers(9617)

    assert len(recorder.requests) == 1, "the second call must be served from the cache"
    assert list(tmp_path.glob("drivers_*.json"))


@pytest.mark.asyncio
async def test_all_driver_window_is_a_single_request(tmp_path) -> None:
    """The whole field must come from one windowed request, not N requests."""
    payload = [
        {"driver_number": number, "x": float(number), "y": 0.0, "date": "2024-10-20T19:00:00Z"}
        for number in (1, 4, 16, 44, 63)
    ]
    recorder = _Recorder([httpx.Response(200, json=payload)])
    async with _client(recorder, tmp_path) as client:
        rows = await client.get_location(9617, date_gt="2024-10-20T19:00:00Z")

    assert len(rows) == 5
    assert len(recorder.requests) == 1
    assert "driver_number" not in recorder.requests[0].url.params
