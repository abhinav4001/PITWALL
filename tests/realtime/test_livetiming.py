"""The official timing hub: parsing, and the adapter's replay-compatible surface."""

import json

import pytest

from pitwall.realtime.livetiming import (
    HUB_BASE,
    WS_BASE,
    LivetimingLiveAdapter,
    LivetimingLiveSource,
)


def _frame(topic: str, payload: dict) -> str:
    """One SignalR frame, as the hub sends it."""
    return json.dumps({"type": 1, "target": topic, "arguments": [json.dumps(payload)]})


def test_the_open_endpoint_is_used_not_the_gated_one() -> None:
    """F1's legacy ``/signalr/`` answers 401; the site's ``/signalrcore`` does not."""
    assert HUB_BASE.endswith("/signalrcore")
    assert WS_BASE.endswith("/signalrcore")
    # The legacy endpoint is the one with nothing after "signalr".
    assert not HUB_BASE.endswith("/signalr")
    assert not WS_BASE.endswith("/signalr")


def test_car_data_is_parsed_into_the_hub_state() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "CarData.z",
            {
                "Entries": {
                    "1": {
                        "Number": 1,
                        "X": 100,
                        "Y": -200,
                        "Z": 5,
                        "Speed": 312,
                        "Throttle": 100,
                        "Brake": 0,
                        "Gear": 8,
                        "RPM": 11100,
                        "DRS": 12,
                        "Compound": "SOFT",
                        "TyreLife": 9,
                        "Lap": 21,
                        "TotalLaps": 56,
                        "Position": 1,
                        "Meta": {
                            "Driver": {
                                "Code": "VER",
                                "FullName": "Max VERSTAPPEN",
                                "Team": "Red Bull Racing",
                                "TeamColour": "3671C6",
                            }
                        },
                    }
                }
            },
        )
    )
    car = source.state.cars[1]
    assert car["speed"] == 312
    assert car["drs"] == 12
    assert car["compound"] == "SOFT"
    assert car["tyre_age"] == 9
    assert car["code"] == "VER"
    # The team colour arrives without a "#" and has to be normalised.
    assert car["team_colour"] == "#3671C6"


def test_team_radio_comes_through_as_text() -> None:
    """F1's own captions are on the topic, so no transcription is needed."""
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "TeamRadio.z",
            {
                "Captions": {
                    "16-2024-10-20T19:23:38": {
                        "Captions": "LECLERC: are you doing a good job gap to max three point five"
                    }
                }
            },
        )
    )
    radio = source.state.radio
    assert len(radio) == 1
    assert radio[0]["driver_number"] == 16
    assert radio[0]["text"] == "are you doing a good job gap to max three point five"
    assert radio[0]["who"] == "LECLERC"


def test_timing_data_gives_the_gap_to_the_car_ahead() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "TimingData.z",
            {
                "Lines": {
                    "44": {
                        "Number": 44,
                        "Position": 2,
                        "LastLap": {"Value": 94.123},
                        "BestLap": {"Value": 93.5},
                        "IntervalToAhead": {"Value": 1.347},
                        "IntervalToLeader": {"Value": 3.572},
                    }
                }
            },
        )
    )
    timing = source.state.timing[44]
    assert timing["interval_to_ahead"] == 1.347
    assert timing["interval_to_leader"] == 3.572
    assert timing["last_lap"] == 94.123


def test_weather_is_parsed_from_the_hub() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "WeatherData.z",
            {
                "Reports": {
                    "R11-2024-10-20T19:05:00": {
                        "SessionKey": "R11",
                        "TimeStamp": "2024-10-20T19:05:00",
                        "AirTemp": 27.5,
                        "TrackTemp": 38.0,
                        "Humidity": 44,
                        "Pressure": 1011.2,
                        "WindDirection": 90,
                        "WindSpeed": 3.4,
                        "Rainfall": 0,
                    }
                }
            },
        )
    )
    weather = source.state.weather
    assert len(weather) == 1
    assert weather[0]["air_temperature"] == 27.5
    assert weather[0]["track_temperature"] == 38.0
    assert weather[0]["rainfall"] == 0


def test_a_broken_frame_does_not_break_the_stream() -> None:
    source = LivetimingLiveSource()
    source._handle("not json at all")
    source._handle(json.dumps({"type": 6}))
    source._handle(json.dumps({"type": 1, "target": "CarData.z", "arguments": []}))
    assert source.state.cars == {}


def test_the_adapter_builds_a_frame_like_the_replay_does() -> None:
    adapter = LivetimingLiveAdapter(LivetimingLiveSource())
    source = adapter.source
    source._handle(
        _frame(
            "DriverList.z",
            {
                "Entries": [
                    {
                        "Number": 1,
                        "Code": "VER",
                        "FullName": "Max VERSTAPPEN",
                        "Team": "Red Bull Racing",
                        "TeamColour": "#3671C6",
                    }
                ]
            },
        )
    )
    source._handle(
        _frame(
            "CarData.z",
            {
                "Entries": {
                    "1": {
                        "Number": 1,
                        "X": 10,
                        "Y": 20,
                        "Speed": 300,
                        "Throttle": 100,
                        "Brake": 0,
                        "DRS": 12,
                        "Compound": "SOFT",
                        "TyreLife": 4,
                        "Lap": 12,
                    }
                }
            },
        )
    )
    source._handle(
        _frame("Position.z", {"Entries": {"1": {"Number": 1, "Position": 1}}})
    )

    frame = adapter.frame()
    assert len(frame.drivers) == 1
    driver = frame.drivers[0]
    assert driver.code == "VER"
    assert driver.name == "Max VERSTAPPEN"
    assert driver.speed == 300
    assert driver.drs_open is True
    assert driver.compound == "SOFT"
    assert driver.position == 1
    assert frame.source == "livetiming.live"


def test_a_yellow_flag_is_read_from_the_race_control_messages() -> None:
    from pitwall.data.models import FlagState

    adapter = LivetimingLiveAdapter(LivetimingLiveSource())
    adapter.source._handle(
        _frame(
            "RaceControlMessages.z",
            {
                "Messages": {
                    "1": {
                        "Message": "YELLOW IN TRACK SECTOR 1",
                        "Category": "Flag",
                        "Flag": "YELLOW",
                        "Lap": 3,
                        "TimeStamp": "2024-10-20T19:05:00",
                    }
                }
            },
        )
    )
    assert adapter._flag_state() == FlagState.YELLOW
    messages = adapter.recent_messages()
    assert messages and "YELLOW IN TRACK SECTOR 1" in messages[-1]["message"]


@pytest.mark.parametrize("bad", ["", "[]", "null"])
def test_unexpected_payloads_are_ignored(bad: str) -> None:
    source = LivetimingLiveSource()
    source._handle(json.dumps({"type": 1, "target": "CarData.z", "arguments": [bad]}))
    assert source.state.cars == {}
