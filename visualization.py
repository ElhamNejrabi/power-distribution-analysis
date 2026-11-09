"""
Visualization for power grid simulations.
"""

import matplotlib.pyplot as plt
import networkx as nx

from grid_model import create_simple_3_bus_network
from fault_simulator import solve_dc_power_flow

def line_color(loading):
    """Pick a color for a line based on its loading (flow / rating)."""
    if loading > 1.0:
        return "#d03b3b"   # red: overloaded
    elif (loading>0.8):
        return "#fab219"   # orange: close to the limit
    else:
        return "#00b41b"        # green: fine


def plot_grid(grid, pf, ax):
    """Draw the grid: buses as circles, lines as edges."""
    # Build {bus: (x, y)} from each bus's 'pos' attribute
    pos = {node: attr['pos'] for node, attr in grid.get_nodes()}

    nx.draw_networkx_nodes(grid.graph, pos, ax=ax, node_size=600)
    loading = pf.loading(grid)
    colors = []
    for u, v in grid.graph.edges():
        if (u, v) in loading:
            colors.append(line_color(loading[(u, v)]))
        else:
            colors.append("#b0b0b0")   # gray for a tripped line

    nx.draw_networkx_edges(grid.graph, pos, ax=ax, arrows=False,
                           edge_color=colors, width=3)

    labels = {}
    for (u, v), lf in loading.items():
        flow = abs(pf.flows[(u, v)])
        labels[(u, v)] = f"{flow:.0f} MW\n{lf:.0%}"
    nx.draw_networkx_edge_labels(grid.graph, pos, edge_labels=labels,
         ax=ax, font_size=8)                     

    nx.draw_networkx_labels(grid.graph, pos, ax=ax)  

    ax.set_title(grid.name)                                
    ax.axis('off')


if __name__ == "__main__":
    grid = create_simple_3_bus_network()
    grid.fault_edge(1,3)
    fig, ax = plt.subplots(figsize=(6, 4))
    pf= solve_dc_power_flow(grid)
    plot_grid(grid, pf, ax)
    fig.savefig( "outputs/my_plot.png")                                   # 
    print("Saved!")
