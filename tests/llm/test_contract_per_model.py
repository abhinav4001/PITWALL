from pitwall.engine.simulator import StrategyResult
from pitwall.llm.schemas import Insight


def test_insight_key_numbers_contract() -> None:
    strat_result = StrategyResult(
        driver_number=1,
        strategy_id="one-stop",
        expected_total_s=5400.5,
        p10_s=5390.0,
        p50_s=5400.5,
        p90_s=5415.0,
        p_beat_rival=0.68,
        n_sims=10000,
    )

    insight = Insight(
        headline="PIT WINDOW OPEN",
        driver_number=1,
        confidence=0.68,
        key_numbers={"expected_total_s": 5400.5, "p_beat_rival": 0.68},
        recommendation="PIT_NOW",
        reasoning="Optimal stop window open, yielding net gain.",
        citations=[],
    )

    allowed_keys = set(strat_result.model_dump().keys())
    for k in insight.key_numbers.keys():
        assert k in allowed_keys, (
            f"Contract violation: key '{k}' in Insight.key_numbers "
            "is not part of StrategyResult!"
        )
