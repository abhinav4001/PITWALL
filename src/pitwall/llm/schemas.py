from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RegulationRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    article: str
    summary: str


class Insight(BaseModel):
    model_config = ConfigDict(frozen=True)

    headline: str
    driver_number: int = Field(gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    key_numbers: dict[str, float]
    recommendation: Literal["PIT_NOW", "WAIT", "MONITOR", "UNKNOWN"]
    reasoning: str
    citations: list[RegulationRef] = Field(default_factory=list)
