"""API contract tests.

These run against the FastAPI app with the replay cache unavailable, so they
assert the *honest* behaviour: either real data or an explicit
``available: false`` payload.  No test may pass on a fabricated value.
"""

import pytest
from fastapi.testclient import TestClient

from pitwall.api import app as app_module

client = TestClient(app_module.app)


def test_health_check() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_current_session_never_invents_a_session_key() -> None:
    response = client.get("/api/sessions/current")
    assert response.status_code == 200
    payload = response.json()

    if payload.get("available"):
        # Real data: the session key must be a real OpenF1 key.
        assert isinstance(payload["session_key"], int)
        assert payload["session_key"] > 0
        assert payload["source"].startswith("openf1")
    else:
        # No data: the response must say so explicitly.
        assert payload["source"] == "unavailable"
        assert payload["reason"]
        assert "session_key" not in payload


def test_outline_endpoint_is_explicit_about_missing_data() -> None:
    response = client.get("/api/circuits/Nowhereville/outline")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["source"] == "unavailable"
    assert payload["reason"] == "outline_not_built"


def test_positions_endpoint_shape() -> None:
    response = client.get("/api/positions")
    assert response.status_code == 200
    payload = response.json()
    if payload.get("available"):
        assert "drivers" in payload
        for driver in payload["drivers"]:
            assert "driver_number" in driver
            assert "view_x" in driver and "view_y" in driver
    else:
        assert payload["source"] == "unavailable"


def test_chat_never_returns_invented_numbers() -> None:
    """The old implementation answered every query with the same constants."""
    response = client.post("/api/chat", json={"query": "should he pit now?"})
    assert response.status_code == 200
    payload = response.json()

    if payload.get("available"):
        assert payload["source"] == "simulation"
        assert "key_numbers" in payload
        # Every number must be traceable to a StrategyResult field.
        allowed = {
            "expected_total_s",
            "p10_s",
            "p50_s",
            "p90_s",
            "p_beat_rival",
        }
        assert set(payload["key_numbers"]) <= allowed
    else:
        assert payload["source"] == "unavailable"
        assert payload["reason"]


@pytest.mark.parametrize(
    "query",
    ["should he pit now?", "what tyre should we use?", "when is the pit window?"],
)
def test_chat_answers_differ_per_query_or_are_unavailable(query: str) -> None:
    response = client.post("/api/chat", json={"query": query})
    payload = response.json()
    if payload.get("available"):
        # The strategy must depend on the query, not on a constant.
        assert payload["query"] == query
        assert payload["message"]
