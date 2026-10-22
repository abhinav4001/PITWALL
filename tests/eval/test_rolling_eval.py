from pitwall.eval.rolling_eval import evaluate_rolling_round


def test_evaluate_rolling_round() -> None:
    score = evaluate_rolling_round(round_number=5, circuit="miami", predicted_lap=22, actual_lap=24)
    assert score.round_number == 5
    assert score.circuit == "miami"
    assert score.absolute_error == 2
