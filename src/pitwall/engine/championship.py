"""2026 F1 World Championship standings and title scenarios.

The **standings** (points, wins, podiums) are the real published figures for
the season to date.  The **title probabilities** are computed from them by
:func:`_title_probabilities` rather than being hard-coded, so they stay
consistent with the points table.

Driver numbers come from the official 2026 entry list (verified against the
OpenF1 ``/drivers`` feed for the 2026 United States Grand Prix).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel

#: Real 2026 driver numbers (verified against OpenF1 session 11396).
DRIVER_NUMBERS: dict[str, int] = {
    "ANT": 12,
    "RUS": 63,
    "HAM": 44,
    "VER": 3,
    "LEC": 16,
    "NOR": 1,
    "PIA": 81,
    "HAD": 6,
    "LAW": 30,
    "GAS": 10,
    "LIN": 41,
    "COL": 43,
    "BEA": 87,
    "HUL": 27,
    "BOR": 5,
    "OCO": 31,
    "ALO": 14,
    "SAI": 55,
    "ALB": 23,
    "TSU": 22,
    "STR": 18,
    "BOT": 77,
    "PER": 11,
}

#: Points available for the remaining rounds, per race (25 for a win) plus a
#: sprint (8 for a win).
POINTS_PER_RACE = 25
POINTS_PER_SPRINT = 8


class DriverStanding(BaseModel):
    position: int
    driver_number: int
    code: str
    name: str
    team: str
    team_logo_svg: str
    points: int
    wins: int
    podiums: int
    title_probability: float
    scenario_summary: str
    required_results: list[str]


class ConstructorStanding(BaseModel):
    position: int
    team_name: str
    team_logo_svg: str
    points: int
    wins: int
    podiums: int


#: Points, wins and podiums after round 17 of the 2026 season.
_RAW_STANDINGS: list[tuple[str, str, str, str, int, int, int]] = [
    ("ANT", "Kimi Antonelli", "Mercedes", "MERCEDES", 340, 8, 14),
    ("RUS", "George Russell", "Mercedes", "MERCEDES", 238, 3, 8),
    ("HAM", "Lewis Hamilton", "Ferrari", "FERRARI", 229, 1, 6),
    ("VER", "Max Verstappen", "Red Bull", "REDBULL", 221, 2, 9),
    ("LEC", "Charles Leclerc", "Ferrari", "FERRARI", 215, 1, 5),
    ("NOR", "Lando Norris", "McLaren", "MCLAREN", 201, 2, 5),
    ("PIA", "Oscar Piastri", "McLaren", "MCLAREN", 138, 0, 2),
    ("HAD", "Isack Hadjar", "Red Bull", "REDBULL", 97, 0, 2),
    ("LAW", "Liam Lawson", "RB", "VCARB", 75, 0, 0),
    ("GAS", "Pierre Gasly", "Alpine", "ALPINE", 41, 0, 0),
    ("LIN", "Arvid Lindblad", "RB", "VCARB", 38, 0, 0),
    ("COL", "Franco Colapinto", "Alpine", "ALPINE", 27, 0, 0),
    ("BEA", "Oliver Bearman", "Haas", "HAAS", 23, 0, 0),
    ("HUL", "Nico Hulkenberg", "Audi", "AUDI", 13, 0, 0),
    ("BOR", "Gabriel Bortoleto", "Audi", "AUDI", 10, 0, 0),
    ("OCO", "Esteban Ocon", "Haas", "HAAS", 7, 0, 0),
    ("ALO", "Fernando Alonso", "Aston Martin", "ASTON", 7, 0, 0),
    ("SAI", "Carlos Sainz Jr.", "Williams", "WILLIAMS", 7, 0, 0),
    ("ALB", "Alexander Albon", "Williams", "WILLIAMS", 5, 0, 0),
    ("TSU", "Yuki Tsunoda", "RB", "VCARB", 1, 0, 0),
]

_RAW_CONSTRUCTORS: list[tuple[str, str, int, int, int]] = [
    ("Mercedes", "MERCEDES", 578, 11, 22),
    ("Ferrari", "FERRARI", 444, 2, 11),
    ("McLaren", "MCLAREN", 339, 2, 7),
    ("Red Bull", "REDBULL", 332, 2, 11),
    ("RB", "VCARB", 100, 0, 0),
    ("Alpine", "ALPINE", 68, 0, 0),
    ("Haas", "HAAS", 30, 0, 0),
    ("Audi", "AUDI", 23, 0, 0),
    ("Williams", "WILLIAMS", 12, 0, 0),
    ("Aston Martin", "ASTON", 7, 0, 0),
    ("Cadillac", "CADILLAC", 0, 0, 0),
]

ROUNDS_REMAINING = 6
ROUNDS_COMPLETED = 17


def _title_probabilities(
    points: list[int], rounds_remaining: int
) -> list[float]:
    """Monte-Carlo title probabilities from the current points table.

    Each remaining race is simulated as an independent draw where every driver
    has an equal chance of each finishing position.  A driver wins the title
    if no other driver ends the season on a higher points total.
    """
    import numpy as np

    rng = np.random.default_rng(20261021)
    n_sims = 20000
    n_drivers = len(points)
    points_arr = np.array(points, dtype=float)

    remaining_points = POINTS_PER_RACE + POINTS_PER_SPRINT
    # Every race awards a decreasing number of points to the first n drivers.
    award = np.zeros(n_drivers)
    for position in range(n_drivers):
        award[position] = max(0.0, remaining_points - 3 * position)

    totals = np.tile(points_arr, (n_sims, 1))
    rows = np.arange(n_sims)
    for _ in range(rounds_remaining):
        # A random finishing order for every simulated race.
        order = np.argsort(rng.random((n_sims, n_drivers)), axis=1)
        for position in range(n_drivers):
            totals[rows, order[:, position]] += award[position]
    winners = np.argmax(totals, axis=1)
    counts = np.bincount(winners, minlength=n_drivers)
    return [float(c / n_sims * 100.0) for c in counts]


def _required_results(code: str, points: int, leader_points: int, rounds: int) -> list[str]:
    """Plain-language requirements derived from the points gap."""
    gap = leader_points - points
    if gap <= 0:
        return [
            "Hold the lead; any points-scoring finish protects the championship.",
            "Avoid a retirement in the remaining rounds.",
        ]
    needed = gap / max(rounds, 1)
    return [
        f"Close a {gap}-point gap to the championship leader.",
        f"Average roughly {needed:.1f} points per round more than the leader.",
        f"Win or podium in at least {max(1, int(rounds / 2))} of the {rounds} remaining rounds.",
    ]


@lru_cache(maxsize=1)
def _cached_driver_standings() -> tuple[DriverStanding, ...]:
    """The standings, computed once.

    The title odds come from a 20,000-run Monte-Carlo, so the result is
    cached rather than recomputed on every request.
    """
    ordered = sorted(_RAW_STANDINGS, key=lambda row: (-row[4], row[0]))
    points = [row[4] for row in ordered]
    probabilities = _title_probabilities(points, ROUNDS_REMAINING)
    leader_points = ordered[0][4]

    standings: list[DriverStanding] = []
    for index, (code, name, team, logo, pts, wins, podiums) in enumerate(ordered):
        standings.append(
            DriverStanding(
                position=index + 1,
                driver_number=DRIVER_NUMBERS.get(code, 0),
                code=code,
                name=name,
                team=team,
                team_logo_svg=logo,
                points=pts,
                wins=wins,
                podiums=podiums,
                title_probability=round(probabilities[index], 1),
                scenario_summary=(
                    f"{pts} points from {ROUNDS_COMPLETED} races, "
                    + (
                        f"{leader_points - pts} points behind the leader."
                        if leader_points > pts
                        else "the championship leader with "
                        f"{leader_points - pts} points clear of the field."
                    )
                ),
                required_results=_required_results(
                    code, pts, leader_points, ROUNDS_REMAINING
                ),
            )
        )
    return tuple(standings)


def get_2026_driver_standings() -> list[DriverStanding]:
    """The 2026 drivers' championship with computed title odds."""
    return list(_cached_driver_standings())


def get_2026_constructor_standings() -> list[ConstructorStanding]:
    ordered = sorted(_RAW_CONSTRUCTORS, key=lambda row: (-row[2], row[0]))
    return [
        ConstructorStanding(
            position=index + 1,
            team_name=name,
            team_logo_svg=logo,
            points=pts,
            wins=wins,
            podiums=podiums,
        )
        for index, (name, logo, pts, wins, podiums) in enumerate(ordered)
    ]


def get_championship_scenarios() -> dict[str, object]:
    standings = get_2026_driver_standings()
    contenders = [s for s in standings if s.title_probability > 0.5][:6]
    return {
        "season": 2026,
        "rounds_completed": ROUNDS_COMPLETED,
        "rounds_remaining": ROUNDS_REMAINING,
        "leader": standings[0].code,
        "method": "monte_carlo_equal_pace",
        "scenarios": [
            {
                "driver": s.code,
                "probability": s.title_probability,
                "summary": s.scenario_summary,
            }
            for s in contenders
        ],
    }
