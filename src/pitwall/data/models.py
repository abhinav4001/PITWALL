from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FlagState(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    VSC = "VSC"
    SAFETY_CAR = "SAFETY_CAR"
    RED = "RED"


class DriverState(BaseModel):
    model_config = ConfigDict(frozen=True)

    driver_number: int = Field(gt=0)
    position: int | None = Field(default=None, gt=0)
    compound: str | None = None
    tyre_age_laps: int | None = Field(default=None, ge=0)
    gap_ahead_s: float | None = None
    last_lap_s: float | None = Field(default=None, gt=0)


class WorldState(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_key: str
    timestamp: datetime
    lap: int = Field(ge=0)
    total_laps: int | None = Field(default=None, gt=0)
    flag_state: FlagState = FlagState.GREEN
    drivers: dict[int, DriverState] = Field(default_factory=dict)
    air_temperature_c: float | None = None
    track_temperature_c: float | None = None
    rain_probability: float | None = Field(default=None, ge=0, le=1)


class WorldStateDelta(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_key: str
    timestamp: datetime
    lap: int | None = Field(default=None, ge=0)
    total_laps: int | None = Field(default=None, gt=0)
    flag_state: FlagState | None = None
    drivers: dict[int, DriverState] = Field(default_factory=dict)
    air_temperature_c: float | None = None
    track_temperature_c: float | None = None
    rain_probability: float | None = Field(default=None, ge=0, le=1)


class StrategyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    driver_number: int = Field(gt=0)
    strategy_id: str
    expected_total_s: float = Field(ge=0)
    p10_s: float = Field(ge=0)
    p50_s: float = Field(ge=0)
    p90_s: float = Field(ge=0)
    p_beat_rival: float = Field(ge=0, le=1)
    n_sims: int = Field(gt=0)
