"""Fit a calibration bundle for a circuit from cached session data.

Usage
-----
    python scripts/calibrate_circuit.py --circuit Austin --year 2024 --session 9617
    python scripts/calibrate_circuit.py --latest --circuit Austin

The numbers are fitted from **real** laps by
:mod:`pitwall.engine.calibrate`, which is the same code the app runs when it
picks up a new session, so a fitted bundle is always reproducible.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pitwall.data.session_pack import DEFAULT_DIR  # noqa: E402
from pitwall.engine.calibrate import fit_calibration  # noqa: E402
from pitwall.engine.calibration import save_calibration  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--circuit", default="Austin")
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--session", type=int, default=9617)
    parser.add_argument("--base", default=str(DEFAULT_DIR))
    args = parser.parse_args()

    bundle = fit_calibration(args.session, Path(args.base))
    path = save_calibration(bundle)
    print(json.dumps(bundle.model_dump(), indent=2))
    print(f"\nsaved -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
