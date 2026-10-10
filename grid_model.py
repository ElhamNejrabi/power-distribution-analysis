"""
Power Grid Model
Represents a power network as a graph using NetworkX.

Conventions
-----------
- Power is in MW, line reactance is in per-unit on the system MVA base.
- Each line has a *reference direction* (from_node -> to_node). A positive flow
  means power moves from -> to, a negative flow means it moves to -> from.
  Physically, lines are bidirectional, so connectivity is always evaluated on
  the undirected view of the graph.
"""

import json
import math

import networkx as nx


class GridModel:
    """
    A power network as a directed graph (direction = flow sign convention only).

    Nodes (buses / substations):
        load                 Demand (MW)
        generation           Scheduled generator output (MW)
        generation_capacity  Max generator output (MW)
        is_generator         Whether this bus has generation
        voltage              Nominal voltage (kV) - informational
        pos                  Optional (x, y) for plotting
        active               False when the bus is faulted / out of service

    Edges (transmission lines):
        reactance            Series reactance X (p.u.) - drives DC power flow
        capacity             Thermal rating (MW) - line trips above this
        active               False when the line is faulted / tripped
    """

    def __init__(self, name="Grid", base_mva=100.0):
        """Initialize an empty grid."""
        self.name = name
        self.base_mva = base_mva
        self.graph = nx.DiGraph()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def add_substation(self, node_id, voltage=138, load=0.0, generation=0.0,
                       generation_capacity=0.0, is_generator=None, pos=None,
                       active=True):
        """
        Add a bus (substation/junction) to the grid.

        Args:
            node_id: Unique identifier (int or str)
            voltage: Nominal voltage (kV)
            load: Power demand (MW)
            generation: Scheduled generator output (MW)
            generation_capacity: Max generator output (MW)
            is_generator: Defaults to True when generation_capacity > 0
            pos: Optional (x, y) layout position for plotting
            active: Bus status
        """
        if is_generator is None:
            is_generator = generation_capacity > 0
        if generation > generation_capacity:
            raise ValueError(f"Bus {node_id}: generation {generation} exceeds capacity {generation_capacity}")

        self.graph.add_node(
            node_id,
            voltage=voltage,
            load=float(load),
            generation=float(generation),
            generation_capacity=float(generation_capacity),
            is_generator=bool(is_generator),
            pos=tuple(pos) if pos is not None else None,
            active=active,
        )

    def add_transmission_line(self, from_node, to_node, reactance=0.1, capacity=100.0,
                              active=True):
        """
        Add a transmission line between two existing buses.

        Args:
            from_node: Reference "from" bus
            to_node: Reference "to" bus
            reactance: Series reactance (p.u.) - must be > 0
            capacity: Thermal rating (MW)
            active: Line status
        """
        for node in (from_node, to_node):
            if node not in self.graph:
                raise KeyError(f"Bus {node} does not exist - add it before connecting lines")
        if reactance <= 0:
            raise ValueError(f"Line {from_node}-{to_node}: reactance must be positive")
        if self.has_line(from_node, to_node):
            raise ValueError(f"Line {from_node}-{to_node} already exists")

        self.graph.add_edge(
            from_node,
            to_node,
            reactance=float(reactance),
            capacity=float(capacity),
            active=active,
        )

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    def get_node(self, node_id):
        """Get all attributes of a bus."""
        return self.graph.nodes[node_id]

    def get_edge(self, from_node, to_node):
        """Get all attributes of a line (either orientation)."""
        return self.graph.edges[self.line_key(from_node, to_node)]

    def has_line(self, a, b):
        """True if a line connects a and b in either orientation."""
        return self.graph.has_edge(a, b) or self.graph.has_edge(b, a)

    def line_key(self, a, b):
        """Return the stored (from, to) orientation of the line between a and b."""
        if self.graph.has_edge(a, b):
            return (a, b)
        if self.graph.has_edge(b, a):
            return (b, a)
        raise KeyError(f"No line between {a} and {b}")

    def set_load(self, node_id, load):
        """Update load at a bus."""
        self.graph.nodes[node_id]['load'] = float(load)

    def set_generation(self, node_id, generation):
        """Update scheduled generation at a bus."""
        self.graph.nodes[node_id]['generation'] = float(generation)

    def get_nodes(self):
        """Get all buses with attributes."""
        return list(self.graph.nodes(data=True))

    def get_edges(self):
        """Get all lines with attributes."""
        return list(self.graph.edges(data=True))

    def get_active_nodes(self):
        """Get only in-service buses."""
        return [n for n, attr in self.graph.nodes(data=True) if attr['active']]

    def get_active_edges(self):
        """
        Get in-service lines. A line is only in service if it is active
        and both of its end buses are active.
        """
        nodes = self.graph.nodes
        return [(u, v, attr) for u, v, attr in self.graph.edges(data=True)
                if attr['active'] and nodes[u]['active'] and nodes[v]['active']]

    def total_load(self):
        """Total demand of all buses (MW), including faulted ones."""
        return sum(attr['load'] for _, attr in self.graph.nodes(data=True))

    def islands(self):
        """
        Electrical islands: groups of in-service buses connected by in-service lines.

        Returns:
            list[set]: One set of bus IDs per island, largest first
        """
        energized = nx.Graph()
        energized.add_nodes_from(self.get_active_nodes())
        energized.add_edges_from((u, v) for u, v, _ in self.get_active_edges())
        return sorted(nx.connected_components(energized), key=len, reverse=True)

    # ------------------------------------------------------------------ #
    # Faults
    # ------------------------------------------------------------------ #
    def fault_node(self, node_id):
        """Take a bus out of service (its lines are implicitly out too)."""
        self.graph.nodes[node_id]['active'] = False

    def fault_edge(self, from_node, to_node):
        """Take a line out of service."""
        self.graph.edges[self.line_key(from_node, to_node)]['active'] = False

    def restore_node(self, node_id):
        """Return a bus to service."""
        self.graph.nodes[node_id]['active'] = True

    def restore_edge(self, from_node, to_node):
        """Return a line to service."""
        self.graph.edges[self.line_key(from_node, to_node)]['active'] = True

    def restore_all(self):
        """Return every bus and line to service."""
        for node in self.graph.nodes():
            self.graph.nodes[node]['active'] = True
        for u, v in self.graph.edges():
            self.graph.edges[u, v]['active'] = True

    def copy(self):
        """Independent copy, so simulations never mutate the original grid."""
        new = GridModel(name=self.name, base_mva=self.base_mva)
        new.graph = self.graph.copy()
        return new

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self):
        """Convert grid to a JSON-serializable dictionary."""
        nodes = [{'id': n, **attr} for n, attr in self.graph.nodes(data=True)]
        edges = [{'from': u, 'to': v, **attr} for u, v, attr in self.graph.edges(data=True)]
        return {'name': self.name, 'base_mva': self.base_mva, 'nodes': nodes, 'edges': edges}

    def save(self, filepath):
        """Save grid to JSON file."""
        with open(filepath, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)

    @staticmethod
    def load(filepath):
        """Load grid from JSON file."""
        with open(filepath, 'r') as f:
            data = json.load(f)

        grid = GridModel(name=data['name'], base_mva=data.get('base_mva', 100.0))
        for node in data['nodes']:
            node = dict(node)
            grid.add_substation(node.pop('id'), **node)
        for edge in data['edges']:
            edge = dict(edge)
            grid.add_transmission_line(edge.pop('from'), edge.pop('to'), **edge)
        return grid

    def __repr__(self):
        return (f"GridModel({self.name}) - {self.graph.number_of_nodes()} buses, "
                f"{self.graph.number_of_edges()} lines, {self.total_load():.1f} MW load")


# ---------------------------------------------------------------------- #
# Test networks
# ---------------------------------------------------------------------- #
def create_simple_3_bus_network():
    """
    Minimal meshed 3-bus network - small enough to check by hand.

        Bus 1 (gen) ── Bus 2 (load)
             \\          /
              Bus 3 (load)
    """
    grid = GridModel(name="Simple 3-Bus Network")

    grid.add_substation(1, load=0, generation=180, generation_capacity=300, pos=(0, 1))
    grid.add_substation(2, load=100, pos=(2, 1))
    grid.add_substation(3, load=80, pos=(1, 0))

    grid.add_transmission_line(1, 2, reactance=0.10, capacity=150)
    grid.add_transmission_line(2, 3, reactance=0.15, capacity=60)
    grid.add_transmission_line(1, 3, reactance=0.12, capacity=120)

    return grid


def create_sample_5_bus_network():
    """
    Small 5-bus network with two generators, a meshed core and a radial feeder.

        Gen 1 ── 2 ── 3
          │      │ \\  │
          │      │  \\ │
         Gen 5 ─ 4 ──┘   (plus radial bus via 4)

    Topology chosen so that losing one corridor overloads its parallel path,
    which is exactly the mechanism that drives cascading failures.
    """
    grid = GridModel(name="Sample 5-Bus Network")

    grid.add_substation(1, voltage=138, load=0, generation=200, generation_capacity=300, pos=(0, 2))
    grid.add_substation(2, voltage=138, load=90, pos=(2, 2))
    grid.add_substation(3, voltage=138, load=120, pos=(4, 2))
    grid.add_substation(4, voltage=138, load=70, pos=(2, 0))
    grid.add_substation(5, voltage=138, load=20, generation=100, generation_capacity=150, pos=(0, 0))

    grid.add_transmission_line(1, 2, reactance=0.06, capacity=200)
    grid.add_transmission_line(2, 3, reactance=0.08, capacity=110)
    grid.add_transmission_line(2, 4, reactance=0.10, capacity=90)
    grid.add_transmission_line(3, 4, reactance=0.09, capacity=90)
    grid.add_transmission_line(1, 5, reactance=0.12, capacity=80)
    grid.add_transmission_line(5, 4, reactance=0.08, capacity=110)

    return grid


# IEEE 14-bus test case (MATPOWER case14).
# Bus loads (MW) and branch reactances (p.u., 100 MVA base) are the published values.
# Buses 3, 6 and 8 are synchronous condensers in the original case (0 MW), so only
# buses 1 and 2 are modeled as real-power sources.
_IEEE14_LOADS = {1: 0.0, 2: 21.7, 3: 94.2, 4: 47.8, 5: 7.6, 6: 11.2, 7: 0.0,
                 8: 0.0, 9: 29.5, 10: 9.0, 11: 3.5, 12: 6.1, 13: 13.5, 14: 14.9}
_IEEE14_GENERATORS = {1: (232.4, 332.4), 2: (40.0, 140.0)}  # bus: (scheduled MW, max MW)
_IEEE14_BRANCHES = [  # (from, to, reactance p.u.)
    (1, 2, 0.05917), (1, 5, 0.22304), (2, 3, 0.19797), (2, 4, 0.17632),
    (2, 5, 0.17388), (3, 4, 0.17103), (4, 5, 0.04211), (4, 7, 0.20912),
    (4, 9, 0.55618), (5, 6, 0.25202), (6, 11, 0.19890), (6, 12, 0.25581),
    (6, 13, 0.13027), (7, 8, 0.17615), (7, 9, 0.11001), (9, 10, 0.08450),
    (9, 14, 0.27038), (10, 11, 0.19207), (12, 13, 0.19988), (13, 14, 0.34802),
]
# Layout roughly following the standard one-line diagram.
_IEEE14_POS = {1: (0, 4), 2: (1, 0.5), 3: (4, 0), 4: (3.5, 2), 5: (1.5, 2.5),
               6: (1.5, 4.5), 7: (5, 3), 8: (6.5, 3), 9: (4.5, 4.5), 10: (3.5, 5.5),
               11: (2.5, 5.5), 12: (0.5, 6.5), 13: (2, 6.5), 14: (4.5, 6.5)}


def create_ieee_14_bus_network(rating_margin=1.25, min_rating=15.0, base_flows=None):
    """
    IEEE 14-bus test system with assumed thermal ratings.

    The published case has no line ratings, so ratings are set to
    `rating_margin` x base-case flow (at least `min_rating` MW). This mimics a
    grid operated with limited headroom - the condition under which real
    cascades happen.

    Args:
        rating_margin: Rating as a multiple of base-case |flow|
        min_rating: Floor for lightly loaded lines (MW)
        base_flows: Optional precomputed {(from, to): MW}; computed if omitted
    """
    grid = GridModel(name="IEEE 14-Bus Test System", base_mva=100.0)

    for bus, load in _IEEE14_LOADS.items():
        gen, cap = _IEEE14_GENERATORS.get(bus, (0.0, 0.0))
        grid.add_substation(bus, voltage=138 if bus <= 5 else 69, load=load,
                            generation=gen, generation_capacity=cap, pos=_IEEE14_POS[bus])

    for u, v, x in _IEEE14_BRANCHES:
        grid.add_transmission_line(u, v, reactance=x, capacity=float('inf'))

    if base_flows is None:
        from fault_simulator import solve_dc_power_flow  # local import avoids a cycle
        base_flows = solve_dc_power_flow(grid).flows

    for (u, v), flow in base_flows.items():
        rating = max(min_rating, rating_margin * abs(flow))
        grid.get_edge(u, v)["capacity"] = math.ceil(rating / 5) * 5

    return grid


NETWORKS = {
    '3bus': create_simple_3_bus_network,
    '5bus': create_sample_5_bus_network,
    'ieee14': create_ieee_14_bus_network,
}


if __name__ == "__main__":
    import os

    grid = create_sample_5_bus_network()
    print(grid)
    for node, attr in grid.get_nodes():
        print(f"  Bus {node}: {attr}")

    os.makedirs("test_grids", exist_ok=True)
    grid.save("test_grids/sample_5_bus.json")
    loaded = GridModel.load("test_grids/sample_5_bus.json")
    print(f"Round-trip through JSON: {loaded}")

    grid.fault_edge(3, 4)
    print(f"Line 3-4 active after fault: {grid.get_edge(3, 4)['active']}")
    grid.restore_edge(3, 4)
    print(f"Line 3-4 active after restore: {grid.get_edge(3, 4)['active']}")
