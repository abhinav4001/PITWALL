# PitWall Evaluation Results & Baseline Log

Recorded per M4 milestone requirements.

## 1. Backtest Methodology
- **Metric 1:** Pit-lap Mean Absolute Error (MAE) in laps vs actual historical stops.
- **Metric 2:** Top-1 and Top-3 strategy ranking accuracy.
- **Zero-Leakage Constraint:** Enforced at the data layer via `truncate_at_data_layer` and verified in CI (`tests/eval/test_no_leakage.py`).

## 2. Initial Baseline (M4)
| Dataset / Circuit | Cutoff Lap | Predicted Optimal Stop Lap | Actual Stop Lap | MAE (laps) | Status |
|---|---|---|---|---|---|
| Synthetic test fixture | Lap 15 | Lap 22 | Lap 24 | 2.0 | Baseline recorded |

*Note: As M5 refines per-circuit calibration and traffic models, backtest MAE will be updated against real historical races.*
