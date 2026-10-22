from pitwall.engine.simulator import (
    CalibrationData,
    StintSpec,
    StrategySpec,
    simulate_strategy,
)


def test_simulate_strategy_returns_valid_distribution() -> None:
    calib = CalibrationData(base_pace=90.0, pit_loss=22.0, noise_sigma=0.2)
    stints = (
        StintSpec(compound="MEDIUM", start_lap=1, end_lap=25, pit_lap=25),
        StintSpec(compound="HARD", start_lap=26, end_lap=50, pit_lap=50),
    )
    strategy = StrategySpec(strategy_id="test-1-stop", driver_number=1, stints=stints)

    result = simulate_strategy(strategy, calib, total_laps=50, n_sims=100)

    assert result.driver_number == 1
    assert result.strategy_id == "test-1-stop"
    assert result.p10_s <= result.p50_s <= result.p90_s
    assert result.n_sims == 100
    assert result.expected_total_s > 0


def test_monotonicity_property() -> None:
    calib = CalibrationData(
        base_pace=90.0, pit_loss=22.0, noise_sigma=0.0
    )  # zero noise for deterministic test

    stints_m = (StintSpec(compound="MEDIUM", start_lap=1, end_lap=50, pit_lap=50),)
    stints_s = (StintSpec(compound="SOFT", start_lap=1, end_lap=50, pit_lap=50),)

    strat_m = StrategySpec(strategy_id="mediums", driver_number=1, stints=stints_m)
    strat_s = StrategySpec(strategy_id="softs", driver_number=1, stints=stints_s)

    res_m = simulate_strategy(strat_m, calib, total_laps=50, n_sims=10)
    res_s = simulate_strategy(strat_s, calib, total_laps=50, n_sims=10)

    assert res_s.expected_total_s >= res_m.expected_total_s
