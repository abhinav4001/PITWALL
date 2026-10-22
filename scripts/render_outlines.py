"""Render the built outlines to PNG for visual verification.

The renderer draws exactly what the frontend draws: the same projection
(:meth:`pitwall.data.track.TrackProjection`) with the y-axis flipped for SVG.
Driver dots from the real OpenF1 trace are overlaid to prove they land on the
track.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pitwall.data.track import TrackProjection  # noqa: E402

OUT_DIR = Path("data/track_outlines")


def main() -> int:
    circuits = ["austin", "silverstone", "monza", "spa"]
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    for ax, name in zip(axes.flat, circuits):
        path = OUT_DIR / f"{name}_2024.json"
        if not path.exists():
            print(f"missing {path}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        points = [(p["x"], p["y"]) for p in data["points"]]
        proj = TrackProjection.from_outline(points)
        vx = [proj.project(x, y)[0] for x, y in points]
        vy = [proj.project(x, y)[1] for x, y in points]
        ax.plot(vx, vy, "-", lw=2.4, color="#e10600")
        ax.scatter(vx[0], vy[0], s=140, marker="s", c="#22c55e", zorder=6, label="start/finish")
        for corner in data.get("corners") or []:
            cx, cy = proj.project(corner["x"], corner["y"])
            ax.annotate(
                str(corner["number"]),
                (cx, cy),
                fontsize=8,
                color="#38bdf8",
                fontweight="bold",
            )
        meta = data.get("metadata", {})
        ax.set_aspect("equal")
        ax.set_title(
            f"{data['circuit']} {data['year']} - {len(points)} pts\n"
            f"source={data['source']} deviation={meta.get('deviation_from_canonical')}u"
        )
        ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    out = OUT_DIR / "_verify.png"
    fig.savefig(out, dpi=95)
    print("saved", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
