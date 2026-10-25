"""The official timing hub: decoding, parsing, and the adapter's surface.

Every payload here is in the format the hub actually sends, which was
established from FastF1's reference client:

* ``CarData.z`` and ``Position.z`` are **base64 of a raw-deflate stream**,
* every other topic is plain JSON,
* the *current session state* arrives in the **completion message** of the
  ``Subscribe`` call, not as an ordinary invocation.
"""

import base64
import json
import zlib
from datetime import UTC

import pytest

from pitwall.realtime.livetiming import (
    HUB_BASE,
    TOPICS,
    WS_BASE,
    LivetimingLiveAdapter,
    LivetimingLiveSource,
    _decode,
    _parse_hub_time,
    session_is_running,
)


def _zipped(payload: dict) -> str:
    """A ``.z`` payload exactly as the hub sends it."""
    raw = json.dumps(payload).encode("utf-8")
    packed = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    deflated = packed.compress(raw) + packed.flush()
    return base64.b64encode(deflated).decode("ascii")


def _frame(topic: str, payload: dict) -> str:
    """One SignalR invocation frame, compressed if the topic is a ``.z``."""
    body = _zipped(payload) if topic.endswith(".z") else json.dumps(payload)
    return json.dumps({"type": 1, "target": topic, "arguments": [body]})


def _feed(topic: str, payload: dict) -> str:
    """One incremental hub update, exactly as the live feed sends it.

    The topic is the *first argument* of a ``feed`` invocation, not the
    target; getting this wrong silently discards every update after the
    initial snapshot.
    """
    return json.dumps(
        {
            "type": 1,
            "target": "feed",
            "arguments": [topic, payload, "2026-10-24T17:45:00.000Z"],
        }
    )


def _completion(snapshot: dict[str, dict]) -> str:
    """The reply to ``Subscribe``: the whole session state, topic by topic."""
    result = {
        topic: _zipped(value) if topic.endswith(".z") else json.dumps(value)
        for topic, value in snapshot.items()
    }
    return json.dumps({"type": 3, "invocationId": "0", "result": result})


# ----------------------------------------------------------------------
# the wire format
# ----------------------------------------------------------------------
def test_the_open_endpoint_is_used_not_the_gated_one() -> None:
    """F1's legacy ``/signalr/`` answers 401; the site's ``/signalrcore`` does not."""
    assert HUB_BASE.endswith("/signalrcore")
    assert WS_BASE.endswith("/signalrcore")
    assert not HUB_BASE.endswith("/signalr")


def test_only_the_topics_that_exist_are_subscribed() -> None:
    """Asking for ``SessionInfo.z`` subscribes to nothing at all.

    Only ``CarData`` and ``Position`` have compressed variants; every other
    topic is plain.  Subscribing to the invented ``.z`` names delivered no
    data whatsoever, which is why the live switch never saw a session.
    """
    zipped = {topic for topic in TOPICS if topic.endswith(".z")}
    assert zipped == {"CarData.z", "Position.z"}
    assert {"SessionInfo", "TimingData", "DriverList"} <= set(TOPICS)


def test_the_subscribe_call_passes_the_topic_list_as_one_argument() -> None:
    """``Subscribe`` takes a *list*, so the arguments must be nested.

    Sent flat, the hub receives the first topic as a bare string and rejects
    the call with an error completion - the socket stays open and silent,
    which reads exactly like "no session is running".
    """
    source = LivetimingLiveSource()
    frame = json.loads(source._subscribe_frame().rstrip("\x1e"))
    assert frame["type"] == 1
    assert frame["target"] == "Subscribe"
    assert frame["arguments"] == [list(TOPICS)]
    # Without an invocation id the hub sends no completion, and the
    # completion's result is the only place the live snapshot appears.
    assert frame["invocationId"] == "0"


def test_a_zipped_payload_is_decoded() -> None:
    payload = {"Entries": {"1": {"Speed": 300}}}
    assert _decode(_zipped(payload)) == payload


def test_a_rejected_subscribe_is_reported_not_swallowed() -> None:
    source = LivetimingLiveSource()
    source._handle(
        json.dumps({"type": 3, "invocationId": "0", "error": "unknown method Subscribe"})
    )
    assert source.subscribed is False
    assert source.state.bad_frames == 1
    assert source.last_error and "subscribe" in source.last_error


def test_plain_json_is_decoded() -> None:
    payload = {"Status": "Started"}
    assert _decode(json.dumps(payload)) == payload
    assert _decode(payload) == payload


def test_a_finished_session_is_not_live() -> None:
    """The hub keeps the last session after it ends; that is not "live".

    Measured on 2026-10-24: the snapshot named Practice 2 with
    ``"Status": "Finalised"``, and without this check the pit wall showed an
    empty live session instead of the replay.
    """
    from datetime import UTC, datetime

    from pitwall.realtime.livetiming import session_is_running

    finished = {
        "Key": 11390,
        "Name": "Practice 2",
        "StartDate": "2026-10-23T16:00:00",
        "EndDate": "2026-10-23T17:00:00",
        "SessionStatus": {"Status": "Finalised"},
    }
    assert session_is_running(finished, now=datetime(2026, 10, 24, 14, 0, tzinfo=UTC)) is False
    assert session_is_running({}, now=datetime(2026, 10, 24, 14, 0, tzinfo=UTC)) is False

    # The hub uses "Finished" for the post-session state.  It must win over
    # the published window, which still allows a 45-minute overrun.
    just_finished = dict(finished, SessionStatus={"Status": "Finished"})
    assert (
        session_is_running(just_finished, now=datetime(2026, 10, 23, 17, 15, tzinfo=UTC))
        is False
    )

    running = dict(finished, SessionStatus={"Status": "Started"})
    assert session_is_running(running, now=datetime(2026, 10, 23, 16, 30, tzinfo=UTC)) is True

    # With no usable status, the published window decides.
    no_status = dict(finished)
    no_status.pop("SessionStatus")
    assert session_is_running(no_status, now=datetime(2026, 10, 23, 16, 30, tzinfo=UTC)) is True
    assert session_is_running(no_status, now=datetime(2026, 10, 24, 14, 0, tzinfo=UTC)) is False


def test_the_subscribe_completion_carries_the_session() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _completion(
            {
                "SessionInfo": {
                    "Key": 9880,
                    "Name": "Practice 1",
                    "Type": "Practice",
                    "Meeting": {
                        "Name": "United States Grand Prix",
                        "Circuit": {"ShortName": "Austin"},
                        "Year": 2026,
                    },
                },
                "DriverList": {
                    "Entries": {"1": {"RacingNumber": "1", "Tla": "VER"}}
                },
            }
        )
    )
    assert source.state.session["Key"] == 9880
    assert source.state.session["Name"] == "Practice 1"
    assert source.state.topics["SessionInfo"] == 1

    adapter = LivetimingLiveAdapter(source)
    assert adapter.session_key == 9880
    assert adapter.circuit == "Austin"
    assert adapter.year == 2026
    assert adapter.session_type == "Practice"


# ----------------------------------------------------------------------
# the data
# ----------------------------------------------------------------------
def test_driver_list_gives_the_names_and_colours() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "DriverList",
            {
                "Entries": {
                    "1": {
                        "RacingNumber": "1",
                        "Tla": "VER",
                        "FullName": "Max VERSTAPPEN",
                        "TeamName": "Red Bull Racing",
                        "TeamColour": "3671C6",
                    }
                }
            },
        )
    )
    driver = source.state.drivers[1]
    assert driver["code"] == "VER"
    assert driver["team"] == "Red Bull Racing"
    # The colour arrives without a "#" and has to be normalised.
    assert driver["team_colour"] == "#3671C6"


def test_car_data_is_read_from_the_numbered_channels() -> None:
    """``CarData`` has no named fields and no position - just channels."""
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "CarData.z",
            {
                "Entries": [
                    {
                        "Utc": "2026-10-24T15:00:00Z",
                        "Cars": {
                            "1": {
                                "Channels": {
                                    "0": 11100,  # rpm
                                    "2": 312,  # speed
                                    "3": 8,  # gear
                                    "4": 100,  # throttle
                                    "5": 0,  # brake
                                    "45": 12,  # drs
                                }
                            }
                        },
                    }
                ]
            },
        )
    )
    car = source.state.cars[1]
    assert car["speed"] == 312
    assert car["n_gear"] == 8
    assert car["throttle"] == 100
    assert car["rpm"] == 11100
    assert car["drs"] == 12


def test_position_comes_from_the_position_topic() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "Position.z",
            {
                "Position": [
                    {
                        "Timestamp": "2026-10-24T15:00:00Z",
                        "Entries": {
                            "1": {"Status": "OnTrack", "X": 1234.5, "Y": -567.8, "Z": 12}
                        },
                    }
                ]
            },
        )
    )
    car = source.state.cars[1]
    assert car["x"] == 1234.5
    assert car["y"] == -567.8
    assert car["on_track"] is True


def test_a_car_in_the_pits_is_marked_off_track() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "Position.z",
            {"Entries": {"1": {"Status": "OffTrack", "X": 0, "Y": 0, "Z": 0}}},
        )
    )
    assert source.state.cars[1]["on_track"] is False


def test_team_radio_comes_through_as_text() -> None:
    """F1's own caption and audio path are on the topic."""
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "TeamRadio",
            {
                "Captions": {
                    "1-2026-10-24T15:03:38": {
                        "Utc": "2026-10-24T15:03:38Z",
                        "Captions": "VERSTAPPEN: the rear is moving around a lot",
                    }
                }
            },
        )
    )
    radio = source.state.radio
    assert len(radio) == 1
    assert radio[0]["driver_number"] == 1
    assert radio[0]["text"] == "the rear is moving around a lot"
    assert radio[0]["who"] == "VERSTAPPEN"
    assert radio[0]["recording_url"] is None


def test_a_later_snapshot_can_add_the_audio_path() -> None:
    """A call first seen without audio must not lose the path that follows."""
    source = LivetimingLiveSource()
    first = {
        "Captions": {
            "1-2026-10-24T15:05:00": {
                "Utc": "2026-10-24T15:05:00Z",
                "Captions": "VERSTAPPEN: box box",
            }
        }
    }
    second = {
        "Captions": {
            "1-2026-10-24T15:05:00": {
                "Utc": "2026-10-24T15:05:00Z",
                "Captions": "VERSTAPPEN: box box",
                "Path": "2026/2026-10-25_United_States_Grand_Prix/"
                "2026-10-23_Practice_3/TeamRadio/VER01_1_x.mp3",
            }
        }
    }
    source._handle(_frame("TeamRadio", first))
    source._handle(_frame("TeamRadio", second))
    radio = source.state.radio
    assert len(radio) == 1
    assert radio[0]["recording_url"] == (
        "https://livetiming.formula1.com/static/2026/"
        "2026-10-25_United_States_Grand_Prix/"
        "2026-10-23_Practice_3/TeamRadio/VER01_1_x.mp3"
    )


def test_a_radio_caption_wrapped_in_metadata_is_still_shown() -> None:
    """The same call must not be shown twice, and its audio path is kept.

    The hub repeats the caption in later snapshots, and some builds wrap the
    recording metadata in a JSON string.  The words and the MP3 path still
    have to appear once.
    """
    source = LivetimingLiveSource()
    payload = {
        "Captions": {
            "16-2026-10-24T15:04:02": {
                "Utc": "2026-10-24T15:04:02Z",
                "Captions": '{"RacingNumber":"16","Path":"TeamRadio/x.mp3",'
                '"Captions":"LECLERC: I have no grip at all"}',
            }
        }
    }
    source._handle(_frame("TeamRadio", payload))
    source._handle(_frame("TeamRadio", payload))
    radio = source.state.radio
    assert len(radio) == 1
    assert radio[0]["driver_number"] == 16
    assert radio[0]["text"] == "I have no grip at all"
    assert (
        radio[0]["recording_url"]
        == "https://livetiming.formula1.com/static/TeamRadio/x.mp3"
    )


def test_timing_data_gives_the_gap_and_the_order() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "TimingData",
            {
                "Lines": {
                    "44": {
                        "Position": 2,
                        "NumberOfLaps": 12,
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
    assert timing["number_of_laps"] == 12
    assert source.state.order[44] == 2


def test_the_hubs_string_times_are_normalised() -> None:
    """The hub writes ``"1:39.699"``; the widgets expect seconds.

    Left as strings, the tower would print a lap time of ``NaN`` and a mixed
    string/number position would make the sort raise - one broken tick, and
    the live page would freeze.
    """
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "TimingData",
            {
                "Lines": {
                    "44": {
                        "Position": "3",
                        "NumberOfLaps": "12",
                        "LastLap": {"Value": "1:39.699"},
                        "IntervalToAhead": {"Value": "+1.234"},
                        "IntervalToLeader": {"Value": "+4.567"},
                    },
                    "16": {
                        "Position": "4",
                        "NumberOfLaps": "11",
                        "LastLap": {"Value": "1:40.001"},
                    },
                }
            },
        )
    )
    assert source.state.timing[44]["last_lap"] == 99.699
    assert source.state.timing[44]["interval_to_ahead"] == 1.234
    assert source.state.timing[44]["interval_to_leader"] == 4.567
    assert source.state.timing[44]["position"] == 3
    assert source.state.timing[44]["number_of_laps"] == 12
    assert source.state.order[44] == 3

    frame = LivetimingLiveAdapter(source).frame()
    assert [driver.position for driver in frame.drivers] == [3, 4]
    assert frame.drivers[0].last_lap_s == 99.699


def test_race_timing_uses_gap_to_leader_and_interval_to_position_ahead() -> None:
    """The race feed names the gaps differently from practice.

    Measured on 2026-10-25 during the United States Grand Prix: the hub sent
    ``GapToLeader`` and ``IntervalToPositionAhead`` rather than
    ``TimeDiffToFastest`` / ``TimeDiffToPositionAhead``.  Without both shapes
    the timing tower showed a dash for every car even though the feed had
    the real gaps.
    """
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "TimingData",
            {
                "Lines": {
                    "16": {
                        "Position": "3",
                        "NumberOfLaps": 2,
                        "GapToLeader": "+3.576",
                        "IntervalToPositionAhead": {"Value": "+1.305"},
                        "LastLapTime": {"Value": "1:48.941"},
                        "BestLapTime": {"Value": "1:48.941", "Lap": 2},
                    }
                }
            },
        )
    )
    timing = source.state.timing[16]
    assert timing["interval_to_leader"] == 3.576
    assert timing["interval_to_ahead"] == 1.305
    assert timing["last_lap"] == 108.941
    assert timing["best_lap"] == 108.941

    driver = LivetimingLiveAdapter(source).frame().drivers[0]
    assert driver.gap_to_leader == 3.576
    assert driver.interval == 1.305
    assert driver.last_lap_s == 108.941
    assert driver.best_lap_s == 108.941


def test_the_tyres_come_from_timing_app_data() -> None:
    """``TimingData`` has no compound; ``TimingAppData`` does."""
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "TimingAppData",
            {
                "Lines": {
                    "1": {
                        "Stints": {
                            "0": {"Compound": "SOFT", "TotalLaps": 18, "StartLaps": 0},
                            "1": {"Compound": "MEDIUM", "TotalLaps": 7, "StartLaps": 0},
                        }
                    }
                }
            },
        )
    )
    tyre = source.state.tyres[1]
    assert tyre["compound"] == "MEDIUM"
    assert tyre["total_laps"] == 7


def test_weather_is_parsed_from_the_hub() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _frame(
            "WeatherData",
            {
                "Reports": {
                    "R11-2026-10-24T15:05:00": {
                        "SessionKey": "R11",
                        "Utc": "2026-10-24T15:05:00",
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


def test_a_yellow_flag_is_read_from_the_race_control_messages() -> None:
    from pitwall.data.models import FlagState

    adapter = LivetimingLiveAdapter(LivetimingLiveSource())
    adapter.source._handle(
        _frame(
            "RaceControlMessages",
            {
                "Messages": {
                    "1": {
                        "Message": "YELLOW IN TRACK SECTOR 1",
                        "Category": "Flag",
                        "Flag": "YELLOW",
                        "Lap": 3,
                        "Utc": "2026-10-24T15:05:00",
                    }
                }
            },
        )
    )
    assert adapter._flag_state() == FlagState.YELLOW
    messages = adapter.recent_messages()
    assert messages and "YELLOW IN TRACK SECTOR 1" in messages[-1]["message"]


# ----------------------------------------------------------------------
# the adapter
# ----------------------------------------------------------------------
def test_the_adapter_builds_a_frame_like_the_replay_does() -> None:
    adapter = LivetimingLiveAdapter(LivetimingLiveSource())
    source = adapter.source
    source._handle(
        _frame(
            "DriverList",
            {
                "Entries": {
                    "1": {
                        "RacingNumber": "1",
                        "Tla": "VER",
                        "FullName": "Max VERSTAPPEN",
                        "TeamName": "Red Bull Racing",
                        "TeamColour": "3671C6",
                    }
                }
            },
        )
    )
    source._handle(
        _frame(
            "CarData.z",
            {
                "Entries": [
                    {
                        "Cars": {
                            "1": {"Channels": {"2": 300, "3": 8, "4": 100, "45": 12}}
                        }
                    }
                ]
            },
        )
    )
    source._handle(
        _frame(
            "Position.z",
            {"Position": [{"Entries": {"1": {"Status": "OnTrack", "X": 10, "Y": 20}}}]},
        )
    )
    source._handle(
        _frame("TimingData", {"Lines": {"1": {"Position": 1, "NumberOfLaps": 12}}})
    )
    source._handle(
        _frame(
            "TimingAppData",
            {"Lines": {"1": {"Stints": {"0": {"Compound": "SOFT", "TotalLaps": 4}}}}},
        )
    )

    frame = adapter.frame()
    assert len(frame.drivers) == 1
    driver = frame.drivers[0]
    assert driver.code == "VER"
    assert driver.name == "Max VERSTAPPEN"
    assert driver.speed == 300
    assert driver.gear == 8
    assert driver.drs_open is True
    assert driver.compound == "SOFT"
    assert driver.tyre_age == 4
    assert driver.position == 1
    assert driver.lap == 12
    assert driver.on_track is True
    assert frame.source == "livetiming.live"


def test_a_used_tyre_age_is_the_total_laps_on_that_set() -> None:
    adapter = LivetimingLiveAdapter(LivetimingLiveSource())
    source = adapter.source
    source._handle(
        _frame(
            "DriverList",
            {
                "1": {
                    "RacingNumber": "1",
                    "Tla": "VER",
                    "FullName": "Max VERSTAPPEN",
                    "TeamName": "Red Bull Racing",
                }
            },
        )
    )
    source._handle(
        _frame(
            "TimingData",
            {
                "Lines": {
                    "1": {
                        "Position": "1",
                        "NumberOfLaps": 12,
                        "LastLapTime": {"Value": "2:44.069"},
                        "BestLapTime": {"Value": "1:35.127"},
                        "Speeds": {"ST": {"Value": "298"}},
                    }
                }
            },
        )
    )
    source._handle(
        _frame(
            "TimingAppData",
            {
                "Lines": {
                    "1": {
                        "Stints": [
                            {
                                "Compound": "SOFT",
                                "New": "false",
                                "TotalLaps": 4,
                                "StartLaps": 3,
                            }
                        ]
                    }
                }
            },
        )
    )

    driver = adapter.frame().drivers[0]
    # TotalLaps is the age of the set, including the laps it had already run
    # when it was fitted.  StartLaps is not added again.
    assert driver.tyre_age == 4
    assert driver.best_lap_s == pytest.approx(95.127)
    assert driver.last_lap_s == pytest.approx(164.069)
    assert driver.speed_trap == pytest.approx(298)


def test_a_broken_frame_does_not_break_the_stream() -> None:
    source = LivetimingLiveSource()
    source._handle("not json at all")
    source._handle(json.dumps({"type": 6}))
    source._handle(json.dumps({"type": 1, "target": "CarData.z", "arguments": []}))
    assert source.state.cars == {}
    assert source.state.bad_frames >= 1


@pytest.mark.parametrize("bad", ["", "[]", "null", "%%%not base64%%%"])
def test_unexpected_payloads_are_ignored(bad: str) -> None:
    source = LivetimingLiveSource()
    source._handle(json.dumps({"type": 1, "target": "CarData.z", "arguments": [bad]}))
    assert source.state.cars == {}


def test_a_live_source_can_be_ticked_end_to_end(tmp_path) -> None:
    """The whole live path, without the network: frames in, a frame out.

    This is exactly what has to work during a practice session, so it is
    pinned here: hub frames -> adapter -> tick loop -> the payload the
    browser draws.
    """
    from pitwall.data.track import TrackProjection
    from pitwall.realtime.tick_loop import TickLoop

    adapter = LivetimingLiveAdapter(LivetimingLiveSource())
    adapter.source._handle(
        _completion(
            {
                "SessionInfo": {
                    "Key": 9880,
                    "Name": "Practice 1",
                    "Type": "Practice",
                    "Meeting": {"Circuit": {"ShortName": "Austin"}, "Year": 2026},
                },
                "DriverList": {
                    "Entries": {
                        "1": {
                            "RacingNumber": "1",
                            "Tla": "VER",
                            "FullName": "Max VERSTAPPEN",
                            "TeamName": "Red Bull Racing",
                            "TeamColour": "3671C6",
                        }
                    }
                },
            }
        )
    )
    adapter.source._handle(
        _frame(
            "Position.z",
            {"Position": [{"Entries": {"1": {"Status": "OnTrack", "X": 10, "Y": 20}}}]},
        )
    )
    adapter.source._handle(
        _frame("CarData.z", {"Entries": [{"Cars": {"1": {"Channels": {"2": 311, "3": 8}}}}]})
    )
    adapter.source._handle(
        _frame("TimingData", {"Lines": {"1": {"Position": 1, "NumberOfLaps": 2}}})
    )

    projection = TrackProjection.from_outline([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)])
    loop = TickLoop(adapter, projection, None, snapshot_dir=tmp_path / "snap")
    state = loop.tick().state

    assert state["available"] is True
    assert state["session_meta"]["session_name"] == "Practice 1"
    assert state["session_meta"]["session_type"] == "Practice"
    assert len(state["drivers"]) == 1
    driver = state["drivers"][0]
    assert driver["code"] == "VER"
    assert driver["speed"] == 311
    assert driver["position"] == 1
    assert driver["view_x"] is not None and driver["view_y"] is not None


def test_the_track_outline_is_the_path_the_cars_drove() -> None:
    source = LivetimingLiveSource()
    for step in range(5):
        source._handle(
            _frame(
                "Position.z",
                {
                    "Position": [
                        {
                            "Entries": {
                                "1": {
                                    "Status": "OnTrack",
                                    "X": float(step * 10),
                                    "Y": float(step * 5),
                                }
                            }
                        }
                    ]
                },
            )
        )
    trace = source.trace()
    assert trace == [(0.0, 0.0), (10.0, 5.0), (20.0, 10.0), (30.0, 15.0), (40.0, 20.0)]
    bounds = source.bounds()
    assert bounds is not None
    assert bounds[0] == 0.0 and bounds[1] == 40.0


def test_feed_envelope_routes_the_topic_and_payload() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _feed(
            "TimingData",
            {
                "Lines": {
                    "1": {
                        "RacingNumber": "1",
                        "Position": "1",
                        "NumberOfLaps": 5,
                        "TimeDiffToFastest": "",
                        "TimeDiffToPositionAhead": "+0.321",
                        "LastLapTime": {"Value": "1:35.962"},
                    }
                }
            },
        )
    )

    assert source.state.topics["TimingData"] == 1
    assert source.state.timing[1]["position"] == 1
    assert source.state.timing[1]["interval_to_ahead"] == 0.321
    assert source.state.timing[1]["last_lap"] == 95.962


def test_driver_list_is_a_direct_mapping_on_the_live_feed() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _feed(
            "DriverList",
            {
                "1": {
                    "RacingNumber": "1",
                    "Tla": "VER",
                    "FullName": "Max VERSTAPPEN",
                    "TeamName": "Red Bull Racing",
                    "TeamColour": "4781D7",
                }
            },
        )
    )

    driver = source.state.drivers[1]
    assert driver["code"] == "VER"
    assert driver["name"] == "Max VERSTAPPEN"
    assert driver["team"] == "Red Bull Racing"


def test_session_info_string_status_becomes_a_running_status() -> None:
    source = LivetimingLiveSource()
    source._on_SessionInfo(
        {
            "Key": 11391,
            "Name": "Practice 3",
            "SessionStatus": "Started",
            "StartDate": "2026-10-24T12:30:00",
            "EndDate": "2026-10-24T13:30:00",
            "GmtOffset": "-05:00:00",
        }
    )
    source._handle(_feed("SessionStatus", {"Status": "Started", "Started": "Started"}))

    assert isinstance(source.state.session["SessionStatus"], dict)
    assert session_is_running(source.state.session) is True


def test_stints_are_an_ordered_list_on_the_live_feed() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _feed(
            "TimingAppData",
            {
                "Lines": {
                    "1": {
                        "RacingNumber": "1",
                        "Stints": [
                            {"Compound": "SOFT", "TotalLaps": 2, "StartLaps": 0},
                            {"Compound": "MEDIUM", "TotalLaps": 4, "StartLaps": 0},
                        ],
                    }
                }
            },
        )
    )

    assert source.state.tyres[1]["compound"] == "MEDIUM"
    assert source.state.tyres[1]["total_laps"] == 4


def test_live_weather_values_are_numbers_not_strings() -> None:
    source = LivetimingLiveSource()
    source._handle(
        _feed(
            "WeatherData",
            {
                "AirTemp": "26.9",
                "TrackTemp": "41.0",
                "Humidity": "50.5",
                "Pressure": "1004.0",
                "WindSpeed": "2.3",
                "WindDirection": "116",
                "Rainfall": "0",
            },
        )
    )

    report = source.state.weather[-1]
    assert report["air_temperature"] == 26.9
    assert report["track_temperature"] == 41.0
    assert report["rainfall"] == 0.0
    assert report["date"]


def test_live_radio_captures_keep_the_official_audio_path() -> None:
    source = LivetimingLiveSource()
    source.state.session["Path"] = (
        "2026/2026-10-25_United_States_Grand_Prix/2026-10-24_Practice_3/"
    )
    source._handle(
        _feed(
            "TeamRadio",
            {
                "Captures": [
                    {
                        "Utc": "2026-10-24T17:35:06.8212045Z",
                        "RacingNumber": "43",
                        "Path": "TeamRadio/COL_43_20261024_123506.mp3",
                    }
                ]
            },
        )
    )

    assert len(source.state.radio) == 1
    assert source.state.radio[0]["driver_number"] == 43
    # The clip path is relative to the session archive; the session prefix is
    # what makes the static URL resolve instead of answering 403.
    assert source.state.radio[0]["recording_url"].endswith(
        "2026/2026-10-25_United_States_Grand_Prix/2026-10-24_Practice_3/"
        "TeamRadio/COL_43_20261024_123506.mp3"
    )


def test_hub_session_time_applies_the_gmt_offset() -> None:
    moment = _parse_hub_time("2026-10-24T12:30:00", "-05:00:00")

    assert moment is not None
    utc = moment.astimezone(UTC)
    assert (utc.hour, utc.minute) == (17, 30)
