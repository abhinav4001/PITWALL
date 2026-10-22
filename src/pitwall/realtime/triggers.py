from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TriggerEvent:
    lap: int
    driver_number: int
    alert_type: str
    payload: dict[str, Any]


class TriggerEngine:
    def __init__(self) -> None:
        self.fired_alerts: set[str] = set()

    def check_and_fire(
        self,
        lap: int,
        driver_number: int,
        alert_type: str,
        payload: dict[str, Any],
    ) -> TriggerEvent | None:
        # The de-duplication key deliberately excludes the lap: an alert such
        # as PIT_WINDOW_OPEN must fire once when the window opens, not on
        # every lap the window stays open.
        key = f"{driver_number}|{alert_type}"
        if key in self.fired_alerts:
            return None  # Already fired for this driver/type

        self.fired_alerts.add(key)
        return TriggerEvent(
            lap=lap,
            driver_number=driver_number,
            alert_type=alert_type,
            payload=payload,
        )

    def evaluate_triggers(
        self,
        lap: int,
        driver_number: int,
        optimal_pit_lap_start: int,
        optimal_pit_lap_end: int,
        tyre_age: int,
        cliff_lap: int,
        gap_ahead_s: float | None,
        pit_loss_s: float,
    ) -> list[TriggerEvent]:
        events: list[TriggerEvent] = []

        # 1. Pit Window Open
        if optimal_pit_lap_start <= lap <= optimal_pit_lap_end:
            event = self.check_and_fire(
                lap,
                driver_number,
                "PIT_WINDOW_OPEN",
                {"optimal_lap": optimal_pit_lap_start},
            )
            if event:
                events.append(event)

        # 2. Tyre Cliff Approaching (within 3 laps)
        if 0 <= (cliff_lap - tyre_age) <= 3:
            event = self.check_and_fire(
                lap,
                driver_number,
                "CLIFF_APPROACHING",
                {"tyre_age": tyre_age, "cliff_lap": cliff_lap},
            )
            if event:
                events.append(event)

        # 3. Undercut Threat: the driver ahead must actually be ahead and
        # within reach, so a negative gap (the car is behind) must not fire.
        if gap_ahead_s is not None and 0.0 <= gap_ahead_s < (pit_loss_s + 1.5):
            event = self.check_and_fire(
                lap,
                driver_number,
                "UNDERCUT_THREAT",
                {"gap_ahead_s": gap_ahead_s},
            )
            if event:
                events.append(event)

        return events
