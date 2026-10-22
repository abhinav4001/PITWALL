"""The tyre state: whole laps, and never invented.

The degradation is measured against the tyre age, so the age has to be a
whole number of laps that steps once per lap.  And when a driver's timing
feed stops part way through a session - which OpenF1 has for Hamilton in the
2024 United States Grand Prix - the age must not be extrapolated, because
that would invent a number no data supports.
"""

from pitwall.realtime.session_replay import _compound_at, _tyre_age_at


def _stint(start: int, end: int, age_at_start: int = 0) -> dict:
    return {
        "lap_start": start,
        "lap_end": end,
        "tyre_age_at_start": age_at_start,
        "compound": "MEDIUM",
    }


def test_the_age_is_a_whole_number_of_laps() -> None:
    stint = _stint(1, 25)
    assert _tyre_age_at(stint, None, 1) == 0
    assert _tyre_age_at(stint, None, 12) == 11
    assert _tyre_age_at(stint, None, 25) == 24


def test_the_age_resets_at_a_pit_stop() -> None:
    assert _tyre_age_at(_stint(1, 25), None, 25) == 24
    assert _tyre_age_at(_stint(26, 56), None, 26) == 0
    assert _tyre_age_at(_stint(26, 56), None, 40) == 14


def test_a_stint_that_began_on_used_tyres_keeps_its_age() -> None:
    stint = _stint(1, 20, age_at_start=3)
    assert _tyre_age_at(stint, None, 1) == 3
    assert _tyre_age_at(stint, None, 10) == 12


def test_an_age_is_not_extrapolated_past_the_stint() -> None:
    """When the stint rows do not cover the lap, the age is not guessed."""
    stint = _stint(1, 1)
    # The driver's feed stopped after lap one; the tyre's last known age is
    # the age the stint began with, not an extrapolation.
    assert _tyre_age_at(stint, None, 23) == 0


def test_no_stint_uses_the_lap_row() -> None:
    lap_row = {"TyreLife": 7}
    assert _tyre_age_at(None, lap_row, 10) == 7
    assert _tyre_age_at(None, None, 10) is None


def test_the_compound_comes_from_the_stint() -> None:
    stint = _stint(1, 25)
    stint["compound"] = "SOFT"
    assert _compound_at(stint, None) == "SOFT"
    assert _compound_at(None, {"Compound": "HARD"}) == "HARD"
    assert _compound_at(None, None) is None
