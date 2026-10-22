from pitwall.realtime.triggers import TriggerEngine


def test_trigger_engine_idempotency() -> None:
    engine = TriggerEngine()

    # First evaluation should fire PIT_WINDOW_OPEN (with gap_ahead_s=25.0 to avoid undercut threat)
    events1 = engine.evaluate_triggers(
        lap=24,
        driver_number=1,
        optimal_pit_lap_start=24,
        optimal_pit_lap_end=26,
        tyre_age=18,
        cliff_lap=40,
        gap_ahead_s=25.0,
        pit_loss_s=22.0,
    )
    assert len(events1) == 1
    assert events1[0].alert_type == "PIT_WINDOW_OPEN"

    # Second evaluation on the SAME lap should NOT re-fire (idempotency check)
    events2 = engine.evaluate_triggers(
        lap=24,
        driver_number=1,
        optimal_pit_lap_start=24,
        optimal_pit_lap_end=26,
        tyre_age=18,
        cliff_lap=40,
        gap_ahead_s=25.0,
        pit_loss_s=22.0,
    )
    assert len(events2) == 0


def test_trigger_engine_cliff_and_undercut() -> None:
    engine = TriggerEngine()

    events = engine.evaluate_triggers(
        lap=38,
        driver_number=13,
        optimal_pit_lap_start=20,
        optimal_pit_lap_end=22,
        tyre_age=37,
        cliff_lap=40,
        gap_ahead_s=21.0,  # less than pit_loss (22.0) + 1.5 -> undercut threat!
        pit_loss_s=22.0,
    )

    alert_types = {e.alert_type for e in events}
    assert "CLIFF_APPROACHING" in alert_types
    assert "UNDERCUT_THREAT" in alert_types
