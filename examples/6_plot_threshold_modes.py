import os
import sys

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import yaml

import naq_graphs as naq
from naq_graphs import plotting
from graph_generator import generate_graph

if len(sys.argv) > 1:
    graph_tpe = sys.argv[-1]
else:
    print("give me a type of graph please!")

params = yaml.full_load(open("graph_params.yaml", "rb"))[graph_tpe]

os.chdir(graph_tpe)

graph = naq.load_graph()
naq.update_parameters(graph, params)
# graph = naq.oversample_graph(graph, params)

modes_df = naq.load_modes()
#print(modes_df)
# threshold_modes, lasing_thresholds = naq.load_modes(filename="threshold_modes")

if not os.path.isdir("threshold_modes"):
    os.mkdir("threshold_modes")

plotting.plot_modes(
    graph, modes_df, df_entry="threshold_lasing_modes", folder="threshold_modes"
)

sys.exit()
positions = [graph.nodes[u]["position"] for u in graph]
for i, threshold_mode in enumerate(threshold_modes):
    graph.graph["params"]["D0"] = lasing_thresholds[i]

    node_solution = naq.mode_on_nodes(threshold_mode, graph)
    edge_solution = naq.mean_mode_on_edges(threshold_mode, graph)

    plt.figure(figsize=(6, 4))
    nodes = nx.draw_networkx_nodes(
        graph,
        pos=positions,
        node_color=abs(node_solution) ** 2,
        node_size=2,
        cmap=plt.get_cmap("Blues"),
    )
    plt.colorbar(nodes, label=r"$|E|^2$ (a.u)")
    edges_k = nx.draw_networkx_edges(
        graph,
        pos=positions,
        edge_color=edge_solution,
        width=2,
        edge_cmap=plt.get_cmap("Blues"),
    )

    plt.title(
        "k="
        + str(np.around(threshold_mode[0], 3) - 1j * np.around(threshold_mode[1], 3))
    )

    plt.savefig("threshold_modes/mode_" + str(i) + ".png")
    plt.close()

    if graph_tpe == "line_PRA" or graph_tpe == "line_semi":
        position_x = [graph.nodes[u]["position"][0] for u in graph]
        E_sorted = node_solution[np.argsort(position_x)]
        node_positions = np.sort(position_x - position_x[1])

        plt.figure(figsize=(6, 4))
        plt.plot(
            node_positions[1:-1], abs(E_sorted[1:-1]) ** 2
        )  # only plot over inner edges
        plt.title(
            "k="
            + str(
                np.around(threshold_mode[0], 3) - 1j * np.around(threshold_mode[1], 3)
            )
        )
        plt.savefig("threshold_modes/profile_mode_" + str(i) + ".svg")
        plt.close()

        naq.save_modes(
            node_positions, E_sorted, filename="threshold_modes/thresholdmode_" + str(i)
        )
