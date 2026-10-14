"""Generate the synthetic outbound order history used by the warehouse examples.

Synthetic by construction (CLAUDE.md: never commit customer data). Deterministic:
re-running produces the identical CSV.

    python examples/data/generate_warehouse_orders.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

OUT = Path(__file__).with_name("warehouse_orders.csv")
SHIFT_S = 4 * 3600
WAVES = [(0, 3600), (3600, 7200), (7200, 10800), (10800, 14400)]  # release, departure
AISLES, BAYS, LANES = 6, 12, 4


def main() -> None:
    rng = np.random.default_rng(20261014)
    rows = []
    n = 0
    for w, (start, departure) in enumerate(WAVES):
        # wave release: most work drops at the start of the wave, the rest trickles in
        count = int(rng.poisson(150))
        times = np.sort(np.concatenate([
            np.full(int(count * 0.6), float(start)),
            rng.uniform(start, departure - 900, count - int(count * 0.6)),
        ]))
        for t in times:
            n += 1
            aisle = int(rng.integers(1, AISLES + 1))
            # skew demand toward the first aisles (fast movers) to create congestion
            if rng.random() < 0.35:
                aisle = int(rng.integers(1, 3))
            bay = int(rng.integers(1, BAYS + 1))
            lane = (w % 2) * 2 + int(rng.integers(1, 3))
            pallets = int(rng.choice([1, 2, 3], p=[0.45, 0.35, 0.20]))
            rows.append({
                "order_id": f"ORD{n:05d}",
                "release_s": f"{t:.1f}",
                "due_s": f"{departure:.1f}",
                "pick_face": f"pick_face_c{aisle:02d}_{bay:02d}",
                "staging_lane": f"staging_lane_{lane:02d}",
                "pallets": pallets,
                "wave": w + 1,
            })
    with OUT.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
