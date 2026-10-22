from pydantic import BaseModel, ConfigDict, Field


class RollingEvalScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    round_number: int = Field(gt=0)
    circuit: str
    predicted_pit_lap: int = Field(ge=0)
    actual_pit_lap: int = Field(ge=0)
    absolute_error: int = Field(ge=0)


def evaluate_rolling_round(
    round_number: int,
    circuit: str,
    predicted_lap: int,
    actual_lap: int,
) -> RollingEvalScore:
    error = abs(predicted_lap - actual_lap)
    return RollingEvalScore(
        round_number=round_number,
        circuit=circuit,
        predicted_pit_lap=predicted_lap,
        actual_pit_lap=actual_lap,
        absolute_error=error,
    )
