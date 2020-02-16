import os as os
import sys as sys

import numpy as np
import yaml as yaml
import pickle as pickle
import matplotlib.pyplot as plt

from graph_generator import generate_graph

from naq_graphs import set_dielectric_constant, set_dispersion_relation
from naq_graphs.dispersion_relations import dispersion_relation_dielectric
from naq_graphs import create_naq_graph, scan_frequencies
from naq_graphs.plotting import plot_scan

if len(sys.argv) > 1:
    graph_tpe = sys.argv[-1]
else:
    print("give me a type of graph please!")

params = yaml.full_load(open("graph_params.yaml", "rb"))[graph_tpe]

graph, positions = generate_graph(tpe=graph_tpe, params=params)

os.chdir(graph_tpe)

create_naq_graph(graph, params, positions=positions)

set_dielectric_constant(graph, params)
set_dispersion_relation(graph, dispersion_relation_dielectric, params)

ks, alphas, qualities = scan_frequencies(graph, params, n_workers=4)

pickle.dump([ks, alphas, qualities], open("scan.pkl", "wb"))

plot_scan(ks, alphas, qualities, np.array([[0, 0],]))
plt.savefig("scan_nomodes.svg")
