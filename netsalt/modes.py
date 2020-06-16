"""Functions related to modes."""
import multiprocessing
import warnings
from functools import partial

import numpy as np
import pandas as pd
import scipy as sc
from tqdm import tqdm

from .algorithm import (
    clean_duplicate_modes,
    find_rough_modes_from_scan,
    refine_mode_brownian_ratchet,
)
from .physics import gamma, q_value
from .quantum_graph import (
    construct_incidence_matrix,
    construct_laplacian,
    construct_weight_matrix,
    mode_quality,
)
from .utils import from_complex, get_scan_grid, to_complex

warnings.filterwarnings("ignore")
warnings.filterwarnings("error", category=np.ComplexWarning)


class WorkerModes:
    """Worker to find modes."""

    def __init__(self, estimated_modes, graph, D0s=None, search_radii=None):
        """Init function of the worker."""
        self.graph = graph
        self.params = graph.graph["params"]
        self.estimated_modes = estimated_modes
        self.D0s = D0s
        self.search_radii = search_radii

    def set_search_radii(self, mode):
        """This fixes a local search region set by search radii."""
        if self.search_radii is not None:
            self.params["k_min"] = mode[0] - self.search_radii[0]
            self.params["k_max"] = mode[0] + self.search_radii[0]
            self.params["alpha_min"] = mode[1] - self.search_radii[1]
            self.params["alpha_max"] = mode[1] + self.search_radii[1]
            # the 0.1 is hardcoded, and seems to be a good value
            self.params["search_stepsize"] = 0.1 * np.linalg.norm(self.search_radii)

    def __call__(self, mode_id):
        """Call function of the worker."""
        if self.D0s is not None:
            self.params["D0"] = self.D0s[mode_id]
        mode = self.estimated_modes[mode_id]
        self.set_search_radii(mode)
        return refine_mode_brownian_ratchet(mode, self.graph, self.params)


class WorkerScan:
    """Worker to scan complex frequency."""

    def __init__(self, graph):
        self.graph = graph

    def __call__(self, freq):
        return mode_quality(to_complex(freq), self.graph)


def scan_frequencies(graph):
    """Scan a range of complex frequencies and return mode qualities."""
    ks, alphas = get_scan_grid(graph)
    freqs = [[k, a] for k in ks for a in alphas]

    worker_scan = WorkerScan(graph)
    pool = multiprocessing.Pool(graph.graph["params"]["n_workers"])
    qualities_list = list(
        tqdm(pool.imap(worker_scan, freqs, chunksize=10), total=len(freqs),)
    )
    pool.close()

    id_k = [k_i for k_i in range(len(ks)) for a_i in range(len(alphas))]
    id_a = [a_i for k_i in range(len(ks)) for a_i in range(len(alphas))]
    qualities = sc.sparse.coo_matrix(
        (qualities_list, (id_k, id_a)),
        shape=(graph.graph["params"]["k_n"], graph.graph["params"]["alpha_n"]),
    ).toarray()

    return qualities


def _init_dataframe():
    """Initialize multicolumn dataframe."""
    indexes = pd.MultiIndex(
        levels=[[], []], codes=[[], []], names=["data", "D0"], dtype=np.float
    )
    return pd.DataFrame(columns=indexes)


def find_modes(graph, qualities):
    """Find the modes from a scan."""
    ks, alphas = get_scan_grid(graph)
    estimated_modes = find_rough_modes_from_scan(
        ks, alphas, qualities, min_distance=2, threshold_abs=1.0
    )
    print("Found", len(estimated_modes), "mode candidates.")
    search_radii = [1 * (ks[1] - ks[0]), 1 * (alphas[1] - alphas[0])]
    worker_modes = WorkerModes(estimated_modes, graph, search_radii=search_radii)
    pool = multiprocessing.Pool(graph.graph["params"]["n_workers"])
    refined_modes = list(
        tqdm(
            pool.imap(worker_modes, range(len(estimated_modes))),
            total=len(estimated_modes),
        )
    )
    pool.close()

    if len(refined_modes) == 0:
        raise Exception("No modes found!")

    refined_modes = [
        refined_mode for refined_mode in refined_modes if refined_mode is not None
    ]

    true_modes = clean_duplicate_modes(
        refined_modes, ks[1] - ks[0], alphas[1] - alphas[0]
    )
    print("Found", len(true_modes), "after refinements.")

    modes_sorted = true_modes[np.argsort(true_modes[:, 1])]
    if graph.graph["params"]["n_modes_max"]:
        print(
            "...but we will use the top",
            graph.graph["params"]["n_modes_max"],
            "modes only",
        )
        modes_sorted = modes_sorted[: graph.graph["params"]["n_modes_max"]]

    modes_df = _init_dataframe()
    modes_df["passive"] = [to_complex(mode_sorted) for mode_sorted in modes_sorted]
    return modes_df


def _convert_edges(vector):
    """Convert single edge values to double edges."""
    edge_vector = np.zeros(2 * len(vector), dtype=np.complex)
    edge_vector[::2] = vector
    edge_vector[1::2] = vector
    return edge_vector


def _get_dielectric_constant_matrix(params):
    """Return sparse diagonal matrix of dielectric constants."""
    return sc.sparse.diags(_convert_edges(params["dielectric_constant"]))


def _get_mask_matrices(params):
    """Return sparse diagonal matrices of pump and inner edge masks."""
    in_mask = sc.sparse.diags(_convert_edges(np.array(params["inner"])))
    pump_mask = sc.sparse.diags(_convert_edges(params["pump"])).dot(in_mask)
    return in_mask, pump_mask


def _graph_norm(BT, Bout, Winv, z_matrix, node_solution, mask):
    """Compute the norm of the node solution on the graph."""
    weight_matrix = Winv.dot(z_matrix).dot(Winv)
    inner_matrix = BT.dot(weight_matrix).dot(mask).dot(Bout)
    norm = node_solution.T.dot(inner_matrix.dot(node_solution))
    return norm


def compute_z_matrix(graph):
    """Construct the matrix Z used for computing the pump overlapping factor."""
    data_diag = (np.exp(2.0j * graph.graph["lengths"] * graph.graph["ks"]) - 1.0) / (
        2.0j * graph.graph["ks"]
    )
    data_off_diag = graph.graph["lengths"] * np.exp(
        1.0j * graph.graph["lengths"] * graph.graph["ks"]
    )
    data = np.dstack([data_diag, data_diag, data_off_diag, data_off_diag]).flatten()

    m = len(graph.edges)
    edge_ids = np.arange(m)
    row = np.dstack(
        [2 * edge_ids, 2 * edge_ids + 1, 2 * edge_ids, 2 * edge_ids + 1]
    ).flatten()
    col = np.dstack(
        [2 * edge_ids, 2 * edge_ids + 1, 2 * edge_ids + 1, 2 * edge_ids]
    ).flatten()
    return sc.sparse.csc_matrix((data, (col, row)), shape=(2 * m, 2 * m))


def compute_overlapping_single_edges(passive_mode, graph):
    """Compute the overlappin factor of a mode with the pump."""
    dielectric_constant = _get_dielectric_constant_matrix(graph.graph["params"])
    in_mask, _ = _get_mask_matrices(graph.graph["params"])
    inner_dielectric_constants = dielectric_constant.dot(in_mask)

    node_solution = mode_on_nodes(passive_mode, graph)

    z_matrix = compute_z_matrix(graph)

    BT, Bout = construct_incidence_matrix(graph)
    Winv = construct_weight_matrix(graph, with_k=False)

    inner_norm = np.real(
        _graph_norm(BT, Bout, Winv, z_matrix, node_solution, inner_dielectric_constants)
    )

    pump_norm = np.zeros(len(graph.edges))
    for pump_edge, inner in enumerate(graph.graph["params"]["inner"]):
        if inner:
            mask = np.zeros(len(graph.edges))
            mask[pump_edge] = 1.0
            pump_mask = sc.sparse.diags(_convert_edges(mask))
            pump_norm[pump_edge] = np.real(
                _graph_norm(BT, Bout, Winv, z_matrix, node_solution, pump_mask)
            )

    return pump_norm / inner_norm


def compute_overlapping_factor(passive_mode, graph):
    """Compute the overlappin factor of a mode with the pump."""
    dielectric_constant = _get_dielectric_constant_matrix(graph.graph["params"])
    in_mask, pump_mask = _get_mask_matrices(graph.graph["params"])
    inner_dielectric_constants = dielectric_constant.dot(in_mask)

    node_solution = mode_on_nodes(passive_mode, graph)

    z_matrix = compute_z_matrix(graph)

    BT, Bout = construct_incidence_matrix(graph)
    Winv = construct_weight_matrix(graph, with_k=False)

    pump_norm = _graph_norm(BT, Bout, Winv, z_matrix, node_solution, pump_mask)
    inner_norm = _graph_norm(
        BT, Bout, Winv, z_matrix, node_solution, inner_dielectric_constants
    )

    return pump_norm / inner_norm


def pump_linear(mode_0, graph, D0_0, D0_1):
    """Find the linear approximation of the new wavenumber."""
    graph.graph["params"]["D0"] = D0_0
    overlapping_factor = compute_overlapping_factor(mode_0, graph)
    freq = to_complex(mode_0)
    gamma_overlap = gamma(freq, graph.graph["params"]) * overlapping_factor
    return from_complex(
        freq * np.sqrt((1.0 + gamma_overlap * D0_0) / (1.0 + gamma_overlap * D0_1))
    )


def mode_on_nodes(mode, graph):
    """Compute the mode solution on the nodes of the graph."""
    laplacian = construct_laplacian(to_complex(mode), graph)
    min_eigenvalue, node_solution = sc.sparse.linalg.eigs(
        laplacian, k=1, sigma=0, v0=np.ones(len(graph)), which="LM"
    )

    if abs(min_eigenvalue[0]) > graph.graph["params"]["quality_threshold"]:
        raise Exception(
            "Not a mode, as quality is too high: "
            + str(abs(min_eigenvalue[0]))
            + " > "
            + str(graph.graph["params"]["quality_threshold"])
            + ", mode: "
            + str(mode)
        )

    return node_solution[:, 0]


def flux_on_edges(mode, graph):
    """Compute the flux on each edge (in both directions)."""

    node_solution = mode_on_nodes(mode, graph)

    BT, _ = construct_incidence_matrix(graph)
    Winv = construct_weight_matrix(graph, with_k=False)

    return Winv.dot(BT.T).dot(node_solution)


def mean_mode_on_edges(mode, graph):
    r"""Compute the average :math:`|E|^2` on each edge."""
    edge_flux = flux_on_edges(mode, graph)

    mean_edge_solution = np.zeros(len(graph.edges))
    for ei in range(len(graph.edges)):
        k = graph.graph["ks"][ei]
        l = graph.graph["lengths"][ei]
        z = np.zeros([2, 2], dtype=np.complex)

        z[0, 0] = (np.exp(1.0j * l * (k - np.conj(k))) - 1.0) / (
            1.0j * l * (k - np.conj(k))
        )
        z[0, 1] = (np.exp(1.0j * l * k) - np.exp(-1.0j * l * np.conj(k))) / (
            1.0j * l * (k + np.conj(k))
        )

        z[1, 0] = z[0, 1]
        z[1, 1] = z[0, 0]

        mean_edge_solution[ei] = np.real(
            np.conj(edge_flux[2 * ei : 2 * ei + 2]).T.dot(
                z.dot(edge_flux[2 * ei : 2 * ei + 2])
            )
        )

    return mean_edge_solution


def _precomputations_mode_competition(graph, pump_mask, mode_threshold):
    """precompute some quantities for a mode for mode competitiion matrix"""
    mode, threshold = mode_threshold

    graph.graph["params"]["D0"] = threshold
    node_solution = mode_on_nodes(mode, graph)

    z_matrix = compute_z_matrix(graph)
    BT, Bout = construct_incidence_matrix(graph)
    Winv = construct_weight_matrix(graph, with_k=False)
    pump_norm = _graph_norm(BT, Bout, Winv, z_matrix, node_solution, pump_mask)

    edge_flux = flux_on_edges(mode, graph) / np.sqrt(pump_norm)
    k_mu = graph.graph["ks"]
    gam = gamma(to_complex(mode), graph.graph["params"])

    return k_mu, edge_flux, gam


def _compute_mode_competition_element(lengths, params, data):
    """Computes a single element of the mode competition matrix."""
    mu_data, nu_data, gamma_nu = data
    k_mus, edge_flux_mu = mu_data
    k_nus, edge_flux_nu = nu_data

    matrix_element = 0
    for ei in range(len(lengths)):
        if params["pump"][ei] > 0.0 and params["inner"][ei]:
            k_mu = k_mus[ei]
            k_nu = k_nus[ei]
            length = lengths[ei]

            inner_matrix = np.zeros([4, 4], dtype=np.complex128)

            # A terms
            ik_tmp = 1.0j * (k_nu - np.conj(k_nu) + 2.0 * k_mu)
            inner_matrix[0, 0] = inner_matrix[3, 3] = (
                np.exp(ik_tmp * length) - 1.0
            ) / ik_tmp

            # B terms
            ik_tmp = 1.0j * (k_nu - np.conj(k_nu) - 2.0 * k_mu)
            inner_matrix[0, 3] = inner_matrix[3, 0] = (
                np.exp(2.0j * k_mu * length) * (np.exp(ik_tmp * length) - 1.0) / ik_tmp
            )

            # C terms
            ik_tmp = 1.0j * (k_nu + np.conj(k_nu) + 2.0 * k_mu)
            inner_matrix[1, 0] = inner_matrix[2, 3] = (
                np.exp(1.0j * (k_nu + 2.0 * k_mu) * length)
                - np.exp(-1.0j * np.conj(k_nu) * length)
            ) / ik_tmp

            # D terms
            ik_tmp = 1.0j * (k_nu + np.conj(k_nu) - 2.0 * k_mu)
            inner_matrix[1, 3] = inner_matrix[2, 0] = (
                np.exp(1.0j * k_nu * length)
                - np.exp(1.0j * (2.0 * k_mu - np.conj(k_nu)) * length)
            ) / ik_tmp

            # E terms
            ik_tmp = 1.0j * (k_nu - np.conj(k_nu))
            inner_matrix[0, 1] = inner_matrix[0, 2] = inner_matrix[3, 1] = inner_matrix[
                3, 2
            ] = (
                np.exp(1.0j * k_mu * length) * (np.exp(ik_tmp * length) - 1.0) / ik_tmp
            )

            # F terms
            ik_tmp = 1.0j * (k_nu + np.conj(k_nu))
            inner_matrix[1, 1] = inner_matrix[1, 2] = inner_matrix[2, 1] = inner_matrix[
                2, 2
            ] = (
                np.exp(1.0j * k_mu * length)
                * (
                    np.exp(1.0j * k_nu * length)
                    - np.exp(-1.0j * np.conj(k_nu) * length)
                )
                / ik_tmp
            )

            # left vector
            flux_nu_plus = edge_flux_nu[2 * ei]
            flux_nu_minus = edge_flux_nu[2 * ei + 1]
            left_vector = np.array(
                [
                    abs(flux_nu_plus) ** 2,
                    flux_nu_plus * np.conj(flux_nu_minus),
                    np.conj(flux_nu_plus) * flux_nu_minus,
                    abs(flux_nu_minus) ** 2,
                ]
            )

            # right vector
            flux_mu_plus = edge_flux_mu[2 * ei]
            flux_mu_minus = edge_flux_mu[2 * ei + 1]
            right_vector = np.array(
                [
                    flux_mu_plus ** 2,
                    flux_mu_plus * flux_mu_minus,
                    flux_mu_plus * flux_mu_minus,
                    flux_mu_minus ** 2,
                ]
            )

            matrix_element += left_vector.dot(inner_matrix.dot(right_vector))

    return -matrix_element * np.imag(gamma_nu)


def compute_mode_competition_matrix(graph, modes_df):
    """Compute the mode competition matrix, or T matrix."""
    threshold_modes = modes_df["threshold_lasing_modes"].to_numpy()
    lasing_thresholds = modes_df["lasing_thresholds"].to_numpy()

    threshold_modes = threshold_modes[lasing_thresholds < np.inf]
    lasing_thresholds = lasing_thresholds[lasing_thresholds < np.inf]

    pool = multiprocessing.Pool(graph.graph["params"]["n_workers"])

    precomp = partial(
        _precomputations_mode_competition,
        graph,
        _get_mask_matrices(graph.graph["params"])[1],
    )

    precomp_results = list(
        tqdm(
            pool.imap(precomp, zip(threshold_modes, lasing_thresholds)),
            total=len(lasing_thresholds),
        )
    )

    lengths = graph.graph["lengths"]

    input_data = []
    for mu in range(len(threshold_modes)):
        for nu in range(len(threshold_modes)):
            input_data.append(
                [
                    precomp_results[mu][:2],
                    precomp_results[nu][:2],
                    precomp_results[nu][2],
                ]
            )

    output_data = list(
        tqdm(
            pool.imap(
                partial(
                    _compute_mode_competition_element, lengths, graph.graph["params"]
                ),
                input_data,
            ),
            total=len(input_data),
        )
    )

    mode_competition_matrix = np.zeros(
        [len(threshold_modes), len(threshold_modes)], dtype=np.complex128
    )
    index = 0
    for mu in range(len(threshold_modes)):
        for nu in range(len(threshold_modes)):
            mode_competition_matrix[mu, nu] = output_data[index]
            index += 1

    pool.close()

    mode_competition_matrix_full = np.zeros(
        [
            len(modes_df["threshold_lasing_modes"]),
            len(modes_df["threshold_lasing_modes"]),
        ]
    )
    mode_competition_matrix_full[
        np.ix_(lasing_thresholds < np.inf, lasing_thresholds < np.inf)
    ] = np.real(mode_competition_matrix)

    return mode_competition_matrix_full


def _find_next_lasing_mode(
    pump_intensity,
    threshold_modes,
    lasing_thresholds,
    lasing_mode_ids,
    mode_competition_matrix,
):
    """Find next interacting lasing mode."""
    interacting_lasing_thresholds = np.ones(len(threshold_modes)) * np.inf
    for mu in range(len(threshold_modes)):
        if mu not in lasing_mode_ids:
            sub_mode_comp_matrix_mu = mode_competition_matrix[
                np.ix_(lasing_mode_ids + [mu,], lasing_mode_ids)
            ]
            sub_mode_comp_matrix_inv = np.linalg.pinv(
                mode_competition_matrix[np.ix_(lasing_mode_ids, lasing_mode_ids)]
            )
            sub_mode_comp_matrix_mu_inv = sub_mode_comp_matrix_mu[-1, :].dot(
                sub_mode_comp_matrix_inv
            )

            factor = (1.0 - sub_mode_comp_matrix_mu_inv.sum()) / (
                1.0
                - lasing_thresholds[mu]
                * sub_mode_comp_matrix_mu_inv.dot(
                    1.0 / lasing_thresholds[lasing_mode_ids]
                )
            )
            if lasing_thresholds[mu] * factor > pump_intensity:
                interacting_lasing_thresholds[mu] = lasing_thresholds[mu] * factor

    next_lasing_mode_id = np.argmin(interacting_lasing_thresholds)
    next_lasing_threshold = interacting_lasing_thresholds[next_lasing_mode_id]
    return next_lasing_mode_id, next_lasing_threshold


def compute_modal_intensities(modes_df, pump_intensities, mode_competition_matrix):
    """Compute the modal intensities of the modes up to D0, with D0_steps."""
    threshold_modes = modes_df["threshold_lasing_modes"]
    lasing_thresholds = modes_df["lasing_thresholds"]

    next_lasing_mode_id = np.argmin(lasing_thresholds)
    next_lasing_threshold = lasing_thresholds[next_lasing_mode_id]
    modal_intensities = pd.DataFrame(index=range(len(threshold_modes)))

    lasing_mode_ids = [next_lasing_mode_id]
    interacting_lasing_thresholds = np.inf * np.ones(len(modes_df))
    interacting_lasing_thresholds[next_lasing_mode_id] = next_lasing_threshold
    modal_intensities.loc[next_lasing_mode_id, next_lasing_threshold] = 0

    pump_intensity = next_lasing_threshold
    while pump_intensity < pump_intensities[-1]:
        # !) compute the current mode intensities
        mode_competition_matrix_inv = np.linalg.pinv(
            mode_competition_matrix[np.ix_(lasing_mode_ids, lasing_mode_ids)]
        )
        slopes = mode_competition_matrix_inv.dot(
            1.0 / lasing_thresholds[lasing_mode_ids]
        )
        shifts = mode_competition_matrix_inv.sum(1)

        # modal_intensities.loc[:, pump_intensity] = 0
        # modal_intensities.loc[:, pump_intensity] = 0
        modal_intensities.loc[lasing_mode_ids, pump_intensity] = (
            slopes * pump_intensity - shifts
        )

        # 2) search for next lasing mode
        next_lasing_mode_id, next_lasing_threshold = _find_next_lasing_mode(
            pump_intensity,
            threshold_modes,
            lasing_thresholds,
            lasing_mode_ids,
            mode_competition_matrix,
        )

        # 3) deal with vanishing modes before next lasing mode
        vanishing_mode_id = None
        if any(slopes < -1e-10):
            vanishing_intensities = shifts / slopes
            vanishing_intensities[slopes > -1e-10] = np.inf

            if np.min(vanishing_intensities) < next_lasing_threshold:
                vanishing_mode_id = lasing_mode_ids[np.argmin(vanishing_intensities)]

        # 4) prepare for the next step
        if vanishing_mode_id is None:
            if next_lasing_threshold < np.inf:
                interacting_lasing_thresholds[
                    next_lasing_mode_id
                ] = next_lasing_threshold
                pump_intensity = next_lasing_threshold
            else:
                pump_intensity = pump_intensities[-1] * 1.1

            lasing_mode_ids.append(next_lasing_mode_id)
        else:
            pump_intensity = np.min(vanishing_intensities) + 1e-10
            modal_intensities.loc[vanishing_mode_id, pump_intensity] = 0
            del lasing_mode_ids[
                np.where(np.array(lasing_mode_ids) == vanishing_mode_id)[0][0]
            ]

    modes_df["interacting_lasing_thresholds"] = interacting_lasing_thresholds

    if "modal_intensities" in modes_df:
        del modes_df["modal_intensities"]

    for pump_intensity in modal_intensities:
        modes_df["modal_intensities", pump_intensity] = modal_intensities[
            pump_intensity
        ]
    print(
        len(np.where(modal_intensities.to_numpy()[:, -1] > 0)[0]),
        "lasing modes out of",
        len(modal_intensities.index),
    )

    return modes_df


def pump_trajectories(modes_df, graph, return_approx=False):
    """For a sequence of D0s, find the mode positions of the modes modes."""

    D0s = np.linspace(
        0, graph.graph["params"]["D0_max"], graph.graph["params"]["D0_steps"],
    )

    pool = multiprocessing.Pool(graph.graph["params"]["n_workers"])
    n_modes = len(modes_df)

    pumped_modes = [[from_complex(mode) for mode in modes_df["passive"]]]
    pumped_modes_approx = pumped_modes.copy()
    for d in range(len(D0s) - 1):
        print(
            "Step "
            + str(d + 1)
            + "/"
            + str(len(D0s) - 1)
            + ", computing for D0="
            + str(D0s[d + 1])
        )
        pumped_modes_approx.append(pumped_modes[-1].copy())
        for m in range(n_modes):
            pumped_modes_approx[-1][m] = pump_linear(
                pumped_modes[-1][m], graph, D0s[d], D0s[d + 1]
            )

        worker_modes = WorkerModes(
            pumped_modes_approx[-1], graph, D0s=n_modes * [D0s[d + 1]]
        )
        pumped_modes.append(
            list(tqdm(pool.imap(worker_modes, range(n_modes)), total=n_modes))
        )
        for i, mode in enumerate(pumped_modes[-1]):
            if mode is None:
                print("Mode not be updated, consider changing the search parameters.")
                pumped_modes[-1][i] = pumped_modes[-2][i]

    pool.close()
    if "mode_trajectories" in modes_df:
        del modes_df["mode_trajectories"]
    for D0, pumped_mode in zip(D0s, pumped_modes):
        modes_df["mode_trajectories", D0] = [to_complex(mode) for mode in pumped_mode]

    if return_approx:
        if "mode_trajectories_approx" in modes_df:
            del modes_df["mode_trajectories_approx"]
        for D0, pumped_mode_approx in zip(D0s, pumped_modes_approx):
            modes_df["mode_trajectories_approx", D0] = [
                to_complex(mode) for mode in pumped_mode_approx
            ]

    return modes_df


def find_threshold_lasing_modes(modes_df, graph):
    """Find the threshold lasing modes and associated lasing thresholds."""
    pool = multiprocessing.Pool(graph.graph["params"]["n_workers"])
    stepsize = graph.graph["params"]["search_stepsize"]

    D0_steps = graph.graph["params"]["D0_max"] / graph.graph["params"]["D0_steps"]

    new_modes = modes_df["passive"].to_numpy()

    threshold_lasing_modes = np.zeros([len(modes_df), 2])
    lasing_thresholds = np.inf * np.ones(len(modes_df))
    D0s = np.zeros(len(modes_df))
    current_modes = np.arange(len(modes_df))
    while len(current_modes) > 0:
        print(len(current_modes), "modes left to find")

        new_D0s = np.zeros(len(modes_df))
        new_modes_approx = np.empty([len(new_modes), 2])
        for i in current_modes:
            new_D0s[i] = abs(
                D0s[i] + lasing_threshold_linear(new_modes[i], graph, D0s[i])
            )

            new_D0s[i] = min(new_D0s[i], D0_steps + D0s[i])
            new_modes_approx[i] = pump_linear(new_modes[i], graph, D0s[i], new_D0s[i])

        # this is a trick to reduce the stepsizes as we are near the solution
        graph.graph["params"]["search_stepsize"] = (
            stepsize * np.mean(abs(new_D0s[new_D0s > 0] - D0s[new_D0s > 0])) / D0_steps
        )

        print("Current search_stepsize:", graph.graph["params"]["search_stepsize"])
        worker_modes = WorkerModes(new_modes_approx, graph, D0s=new_D0s)
        new_modes_tmp = np.zeros([len(modes_df), 2])
        new_modes_tmp[current_modes] = list(
            tqdm(pool.imap(worker_modes, current_modes), total=len(current_modes))
        )

        to_delete = []
        for i, mode_index in enumerate(current_modes):
            if new_modes_tmp[mode_index] is None:
                print(
                    "A mode could not be updated, consider modifying the search parameters."
                )
                new_modes_tmp[mode_index] = new_modes[mode_index]
            elif abs(new_modes_tmp[mode_index][1]) < 1e-6:
                to_delete.append(i)
                threshold_lasing_modes[mode_index] = new_modes_tmp[mode_index]
                lasing_thresholds[mode_index] = new_D0s[mode_index]

            elif new_D0s[mode_index] > graph.graph["params"]["D0_max"]:
                to_delete.append(i)

        current_modes = np.delete(current_modes, to_delete)
        D0s = new_D0s.copy()
        new_modes = new_modes_tmp.copy()

    pool.close()

    modes_df["threshold_lasing_modes"] = [
        to_complex(mode) for mode in threshold_lasing_modes
    ]
    modes_df["lasing_thresholds"] = lasing_thresholds
    return modes_df


def lasing_threshold_linear(mode, graph, D0):
    """Find the linear approximation of the new wavenumber."""
    graph.graph["params"]["D0"] = D0
    return 1.0 / (
        q_value(mode)
        * -np.imag(gamma(to_complex(mode), graph.graph["params"]))
        * np.real(compute_overlapping_factor(mode, graph))
    )