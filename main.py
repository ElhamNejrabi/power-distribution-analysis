"""
Power grid contingency analysis - run the full study from one command.

Examples:
    python main.py                              # IEEE 14-bus, full study
    python main.py --network 3bus --fault 1-3   # cascade for a chosen line
    python main.py --trip-mode all              # all overloaded lines trip at once
"""

import argparse
import json
import os

import matplotlib
matplotlib.use("Agg")  # draw straight to image files, no window needed
import matplotlib.pyplot as plt

from fault_simulator import FaultSimulator
from grid_model import NETWORKS
from visualization import plot_cascade, plot_grid, plot_n_minus_1


def parse_line(text):
    """Turn "1-3" into the line (1, 3)."""
    a, b = text.split("-")
    return (int(a), int(b))


def line_name(line):
    return f"{line[0]}-{line[1]}"


def main():
    parser = argparse.ArgumentParser(description="DC power flow contingency and cascade study")
    parser.add_argument("--network", choices=NETWORKS, default="ieee14",
                        help="test network to study (default: ieee14)")
    parser.add_argument("--fault", type=parse_line,
                        help='line to fail for the cascade plot, e.g. "1-3" (default: worst N-1 case)')
    parser.add_argument("--trip-mode", choices=["all", "worst"], default="worst",
                        help="how overloaded lines trip each round (default: worst)")
    parser.add_argument("--threshold", type=float, default=1.0,
                        help="loading at which a line trips, as a fraction of rating (default: 1.0)")
    parser.add_argument("--out", default="outputs", help="folder for plots and results")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    grid = NETWORKS[args.network]()
    sim = FaultSimulator(grid, overload_threshold=args.threshold, trip_mode=args.trip_mode)
    print(grid)

    # 1. Base case: is the healthy grid within its limits?
    overloads = sim.base_case_overloads()
    if overloads:
        print(f"WARNING: base case already overloaded on {[line_name(l) for l in overloads]}")
    else:
        print("Base case: all lines within rating")

    fig, ax = plt.subplots(figsize=(8, 6))
    plot_grid(grid, sim.base_case, ax)
    ax.set_title(f"{grid.name} - base case")
    fig.savefig(os.path.join(args.out, f"{args.network}_base_case.png"), bbox_inches="tight")
    plt.close(fig)

    # 2. N-1: fail every line one at a time
    results = sim.n_minus_1()
    vulnerable = [r for r in results if r.lost_load > 0.5]
    print(f"\nN-1 analysis: {len(vulnerable)} of {len(results)} single-line outages cause load loss")
    print(f"{'Line':>6}  {'Lost MW':>8}  {'Lost %':>6}  {'Cascade trips':<20}  Blacked-out buses")
    for r in vulnerable:
        s = r.summary()
        print(f"{s['initial_faults'][0]:>6}  {s['lost_mw']:>8.1f}  {100 - s['load_served_percent']:>5.0f}%  "
              f"{', '.join(s['cascade_trips']) or '-':<20}  {s['blacked_out_buses'] or '-'}")

    fig, ax = plt.subplots(figsize=(7, 5))
    plot_n_minus_1(results, ax)
    fig.savefig(os.path.join(args.out, f"{args.network}_n_minus_1.png"), bbox_inches="tight")
    plt.close(fig)

    # 3. Cascade: the chosen line, or the worst one from N-1
    fault = args.fault or results[0].initial_faults[0]
    cascade = sim.simulate_cascade(fault, verbose=True)
    fig = plot_cascade(cascade, grid)
    fig.savefig(os.path.join(args.out, f"{args.network}_cascade_{line_name(fault)}.png"),
                bbox_inches="tight")
    plt.close(fig)

    # 4. Save the numbers so they can be checked or reused later
    summary = {
        "network": grid.name,
        "trip_mode": args.trip_mode,
        "threshold": args.threshold,
        "base_case_overloads": [line_name(l) for l in overloads],
        "n_minus_1": [r.summary() for r in results],
        "cascade": cascade.summary(),
    }
    summary_path = os.path.join(args.out, f"{args.network}_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nPlots and {summary_path} saved to {args.out}/")


if __name__ == "__main__":
    main()
