"""
Fault Simulator for Power Networks
DC power flow, cascading-failure simulation, and N-1 contingency analysis.

DC power flow
-------------
For a lossless network with flat voltage magnitudes and small angle
differences, the AC equations linearize to

    P = B · θ

where P is the vector of net bus injections (generation - load), θ the bus
voltage angles, and B the susceptance matrix built from line reactances:

    B[i, j] = -1 / x_ij              (i != j, line between i and j)
    B[i, i] = Σ_k 1 / x_ik           (sum over lines touching bus i)

B is singular (angles are only defined relative to each other), so one
reference bus per island is fixed at θ = 0 and the reduced system is solved.
Line flows then follow from angle differences:

    P_ij = (θ_i - θ_j) / x_ij
"""

from dataclasses import dataclass, field

import numpy as np

from grid_model import GridModel


# ---------------------------------------------------------------------- #
# DC power flow
# ---------------------------------------------------------------------- #
@dataclass
class PowerFlowResult:
    """Outcome of one DC power flow solve."""
    flows: dict          # (from, to) -> MW, signed in the line's reference direction
    angles: dict         # bus -> voltage angle (degrees); NaN for de-energized buses
    served_load: dict    # bus -> MW actually supplied
    generation: dict     # bus -> MW dispatched
    islands: list        # list[set] of in-service bus groups

    @property
    def total_served(self):
        return sum(self.served_load.values())

    def loading(self, grid):
        """Line loading as a fraction of thermal rating: {(from, to): |flow| / capacity}."""
        result = {}
        for line, flow in self.flows.items():
            capacity = grid.graph.edges[line]['capacity']
            result[line] = abs(flow) / capacity if capacity > 0 else 0.0
        return result


def build_susceptance_matrix(grid, buses):
    """
    Build the DC power flow B matrix for a set of buses.

    Args:
        grid (GridModel): The network
        buses (list): Buses to include; lines leaving this set are ignored

    Returns:
        (np.ndarray, dict): B matrix and {bus: matrix index}
    """
    index = {bus: i for i, bus in enumerate(buses)}
    B = np.zeros((len(buses), len(buses)))

    for u, v, attr in grid.get_active_edges():
        if u not in index or v not in index:
            continue
        i, j = index[u], index[v]
        b = 1.0 / attr['reactance']
        B[i, j] -= b
        B[j, i] -= b
        B[i, i] += b
        B[j, j] += b

    return B, index


def balance_island(grid, island):
    """
    Match generation to demand inside one island.

    Rules (applied independently per island, since islands can't share power):
      - No generation capacity -> blackout: all load in the island is lost.
      - Demand exceeds capacity -> under-frequency load shedding: every load is
        scaled down by the same fraction and all generators run at maximum.
      - Otherwise generators deviate from their schedule to cover the
        imbalance: surplus is cut proportionally to output, a deficit is
        picked up proportionally to each unit's remaining headroom
        (a simple model of automatic generation control).

    Returns:
        (dict, dict): served load per bus, generation per bus (MW)
    """
    nodes = grid.graph.nodes
    demand = sum(nodes[n]['load'] for n in island)
    gens = [n for n in island if nodes[n]['is_generator']]
    capacity = sum(nodes[g]['generation_capacity'] for g in gens)
    scheduled = sum(nodes[g]['generation'] for g in gens)

    if capacity <= 0:
        return {n: 0.0 for n in island}, {g: 0.0 for g in gens}

    served_fraction = min(1.0, capacity / demand) if demand > 0 else 1.0
    served = {n: nodes[n]['load'] * served_fraction for n in island}
    target = demand * served_fraction

    if target >= capacity:
        dispatch = {g: nodes[g]['generation_capacity'] for g in gens}
    elif target <= scheduled:
        scale = target / scheduled if scheduled > 0 else 0.0
        dispatch = {g: nodes[g]['generation'] * scale for g in gens}
    else:
        deficit = target - scheduled
        headroom = capacity - scheduled
        dispatch = {g: nodes[g]['generation']
                    + deficit * (nodes[g]['generation_capacity'] - nodes[g]['generation']) / headroom
                    for g in gens}

    return served, dispatch


def solve_dc_power_flow(grid):
    """
    Solve DC power flow on every island of the grid.

    Returns:
        PowerFlowResult
    """
    nodes = grid.graph.nodes
    islands = grid.islands()

    angles = {n: float('nan') for n in grid.graph.nodes()}
    served_load = {n: 0.0 for n in grid.graph.nodes()}
    generation = {n: 0.0 for n in grid.graph.nodes() if nodes[n]['is_generator']}

    for island in islands:
        served, dispatch = balance_island(grid, island)
        served_load.update(served)
        generation.update(dispatch)

        if not dispatch or sum(served.values()) == 0:
            continue  # de-energized island: no angles, no flow

        buses = sorted(island, key=str)
        B, index = build_susceptance_matrix(grid, buses)
        P = np.array([(dispatch.get(b, 0.0) - served[b]) / grid.base_mva for b in buses])

        # Reference bus: the largest generator in the island (θ = 0)
        ref = index[max(dispatch, key=lambda g: nodes[g]['generation_capacity'])]
        keep = [i for i in range(len(buses)) if i != ref]

        theta = np.zeros(len(buses))
        if keep:
            theta[keep] = np.linalg.solve(B[np.ix_(keep, keep)], P[keep])

        for bus, i in index.items():
            angles[bus] = np.degrees(theta[i])

    flows = {}
    for u, v, attr in grid.get_active_edges():
        if np.isnan(angles[u]) or np.isnan(angles[v]):
            flows[(u, v)] = 0.0
        else:
            delta = np.radians(angles[u] - angles[v])
            flows[(u, v)] = grid.base_mva * delta / attr['reactance']

    return PowerFlowResult(flows, angles, served_load, generation, islands)


# ---------------------------------------------------------------------- #
# Cascading failures
# ---------------------------------------------------------------------- #
@dataclass
class CascadeStep:
    """One round of the cascade: the grid state, then the lines it trips."""
    step: int
    power_flow: PowerFlowResult
    lines_out: set               # every line out of service going into this round
    tripped: list                # lines that overloaded and tripped in this round
    loading: dict                # (from, to) -> loading fraction


@dataclass
class CascadeResult:
    """Full record of a cascade simulation."""
    initial_faults: list
    steps: list = field(default_factory=list)
    total_load: float = 0.0
    base_served: float = 0.0
    final_grid: GridModel = None

    @property
    def final(self):
        return self.steps[-1].power_flow

    @property
    def cascade_depth(self):
        """Number of rounds in which overloads tripped further lines."""
        return sum(1 for s in self.steps if s.tripped)

    @property
    def cascade_trips(self):
        """Lines lost to overloads (not counting the initial fault)."""
        return [line for s in self.steps for line in s.tripped]

    @property
    def served_load(self):
        return self.final.total_served

    @property
    def lost_load(self):
        """Load lost relative to what the intact grid could serve."""
        return max(0.0, self.base_served - self.served_load)

    @property
    def load_served_percent(self):
        return 100.0 * self.served_load / self.total_load if self.total_load > 0 else 100.0

    @property
    def blacked_out_buses(self):
        """Buses with demand that receive no power at all."""
        nodes = self.final_grid.graph.nodes
        return sorted((n for n, mw in self.final.served_load.items()
                       if mw == 0 and nodes[n]['load'] > 0), key=str)

    def summary(self):
        return {
            'initial_faults': [_label(f) for f in self.initial_faults],
            'cascade_depth': self.cascade_depth,
            'cascade_trips': [_label(f) for f in self.cascade_trips],
            'islands': len(self.final.islands),
            'blacked_out_buses': self.blacked_out_buses,
            'served_mw': round(self.served_load, 2),
            'lost_mw': round(self.lost_load, 2),
            'load_served_percent': round(self.load_served_percent, 2),
        }


def _label(element):
    """Human-readable name for a line (tuple) or bus."""
    if isinstance(element, tuple):
        return f"{element[0]}-{element[1]}"
    return f"bus {element}"


class FaultSimulator:
    """
    Simulates faults and the cascading failures that follow.

    Cascade model (quasi-steady-state, as in OPA-style studies):
      1. Apply the initial fault(s).
      2. Re-solve DC power flow - power reroutes through the remaining lines.
      3. Every line loaded above `overload_threshold` x rating trips at once
         (protection relays act in parallel).
      4. Repeat until no line is overloaded.
    Islands created along the way are balanced independently (see balance_island).
    """

    def __init__(self, grid, overload_threshold=1.0, max_steps=50):
        """
        Args:
            grid (GridModel): Network to study (never modified)
            overload_threshold (float): Loading fraction at which a line trips
            max_steps (int): Safety cap on cascade rounds
        """
        self.grid = grid
        self.overload_threshold = overload_threshold
        self.max_steps = max_steps
        self.base_case = solve_dc_power_flow(grid)

    def base_case_overloads(self):
        """Lines already above the threshold with nothing faulted."""
        loading = self.base_case.loading(self.grid)
        return [line for line, lf in loading.items() if lf > self.overload_threshold]

    def simulate_cascade(self, faults, verbose=False):
        """
        Simulate fault(s) and the resulting cascade.

        Args:
            faults: A line (from, to), a bus ID, or a list of these
            verbose (bool): Print the cascade as it unfolds

        Returns:
            CascadeResult
        """
        if not isinstance(faults, list):
            faults = [faults]

        grid = self.grid.copy()
        initial = []
        for fault in faults:
            if isinstance(fault, tuple):
                line = grid.line_key(*fault)
                grid.fault_edge(*line)
                initial.append(line)
            else:
                if fault not in grid.graph:
                    raise KeyError(f"Bus {fault} does not exist")
                grid.fault_node(fault)
                initial.append(fault)

        result = CascadeResult(initial_faults=initial, total_load=grid.total_load(),
                               base_served=self.base_case.total_served, final_grid=grid)

        if verbose:
            print(f"\n{'=' * 60}")
            print(f"FAULT: {', '.join(_label(f) for f in initial)}  ({grid.name})")
            print(f"{'=' * 60}")

        for step in range(self.max_steps):
            pf = solve_dc_power_flow(grid)
            loading = pf.loading(grid)
            overloaded = sorted((line for line, lf in loading.items() if lf > self.overload_threshold),
                                key=lambda line: -loading[line])
            lines_out = {(u, v) for u, v, a in grid.get_edges() if not a['active']}
            result.steps.append(CascadeStep(step, pf, lines_out, overloaded, loading))

            if verbose:
                lost = result.base_served - pf.total_served
                print(f"[Round {step}] {len(pf.islands)} island(s), "
                      f"served {pf.total_served:.1f} MW (lost {lost:.1f} MW)")
                for line in overloaded:
                    print(f"           TRIP {_label(line)}: {abs(pf.flows[line]):.1f} MW "
                          f"= {100 * loading[line]:.0f}% of rating")

            if not overloaded:
                break
            for line in overloaded:
                grid.fault_edge(*line)
        else:
            raise RuntimeError(f"Cascade did not settle within {self.max_steps} rounds")

        if verbose:
            s = result.summary()
            print(f"RESULT: {result.cascade_depth} cascade round(s), "
                  f"{len(result.cascade_trips)} extra line(s) tripped, "
                  f"{s['load_served_percent']:.1f}% of load served, "
                  f"blacked out: {s['blacked_out_buses'] or 'none'}")

        return result

    def n_minus_1(self, include_buses=False):
        """
        N-1 contingency analysis: fail every line (optionally every bus) one at
        a time and simulate the cascade.

        Returns:
            list[CascadeResult]: Ranked worst first (lost load, then cascade size)
        """
        contingencies = [(u, v) for u, v in self.grid.graph.edges()]
        if include_buses:
            contingencies += list(self.grid.graph.nodes())

        results = [self.simulate_cascade(c) for c in contingencies]
        results.sort(key=lambda r: (-r.lost_load, -len(r.cascade_trips), -r.cascade_depth))
        return results


def run_fault_analysis(grid, fault_lines, threshold=1.0, verbose=True):
    """
    Run cascade analysis on several independent fault scenarios.

    Args:
        grid (GridModel): Network
        fault_lines (list): Lines to fault one at a time [(from, to), ...]
        threshold (float): Overload trip threshold (fraction of rating)

    Returns:
        list[CascadeResult]
    """
    simulator = FaultSimulator(grid, overload_threshold=threshold)
    return [simulator.simulate_cascade(line, verbose=verbose) for line in fault_lines]


if __name__ == "__main__":
    from grid_model import create_sample_5_bus_network

    grid = create_sample_5_bus_network()
    sim = FaultSimulator(grid)

    print("Base case flows:")
    for line, flow in sim.base_case.flows.items():
        cap = grid.get_edge(*line)['capacity']
        print(f"  {_label(line):>6}: {flow:7.1f} MW  ({100 * abs(flow) / cap:.0f}% of {cap:.0f} MW)")

    run_fault_analysis(grid, [(1, 2), (2, 3), (5, 4)])
