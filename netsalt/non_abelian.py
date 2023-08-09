"""Module for non-abelian quantum graphs."""
import numpy as np
from scipy import sparse, linalg


def hat_inv(xi_vec):
    """Convert vector Lie algebra to matrix Lie algebra element."""
    xi = np.zeros((3, 3), dtype=np.complex128)
    xi[1, 2] = -xi_vec[0]
    xi[2, 1] = xi_vec[0]
    xi[0, 2] = xi_vec[1]
    xi[2, 0] = -xi_vec[1]
    xi[0, 1] = -xi_vec[2]
    xi[1, 0] = xi_vec[2]
    return xi


def hat(xi):
    """Convert matrix Lie algebra to vector Lie algebra element."""
    xi_vec = np.zeros(3, dtype=np.complex128)
    xi_vec[0] = xi[2, 1]
    xi_vec[1] = xi[0, 2]
    xi_vec[2] = xi[1, 0]
    return xi_vec


def proj_perp(chi):
    """Perpendicular projection."""
    return np.eye(3) - proj_paral(chi)


def proj_paral(chi_mat):
    """Paralell projection."""
    chi_vec = hat(chi_mat)
    return np.outer(chi_vec, chi_vec) / np.linalg.norm(chi_vec) ** 2


def norm(chi_mat):
    """Norm of chi"""
    chi_vec = hat(chi_mat)
    return np.linalg.norm(chi_vec)


def Ad(chi_mat):
    """Adjoint action."""
    return linalg.expm(chi_mat)


def set_so3_wavenumber(graph, wavenumber):
    """Set so3 matrix wavenumber."""
    chis = graph.graph["params"].get("chis", None)
    if chis is None:
        chi = hat_inv(np.array([0.0, 0.0, 1.0]))
        chis = np.array(len(graph.edges) * [chi])
    else:
        if len(np.shape(chis[0])) == 1:
            chis = np.array([hat_inv(chi) for chi in chis])
    graph.graph["ks"] = chis * wavenumber


def construct_so3_incidence_matrix(graph, abelian_scale=1.0):
    """Construct SO3 incidence matrix."""
    DIM = 3

    def _ext(i):
        return slice(DIM * i, DIM * (i + 1))

    Bout = sparse.lil_matrix((len(graph.edges) * 2 * DIM, len(graph) * DIM), dtype=np.complex128)
    BT = sparse.lil_matrix((len(graph) * DIM, len(graph.edges) * 2 * DIM), dtype=np.complex128)
    for ei, (u, v) in enumerate(graph.edges):
        one = np.eye(DIM)
        expl = Ad(graph.graph["lengths"][ei] * graph.graph["ks"][ei])
        expl = np.array(expl.dot(proj_perp(graph.graph["ks"][ei])), dtype=np.complex128)
        expl += (
            abelian_scale
            * proj_paral(graph.graph["ks"][ei])
            * np.exp(1.0j * graph.graph["lengths"][ei] * norm(graph.graph["ks"][ei]))
        )

        out = len(graph[u]) == 1 or len(graph[v]) == 1

        Bout[_ext(2 * ei), _ext(u)] = -one
        Bout[_ext(2 * ei), _ext(v)] = 0 if out else expl
        Bout[_ext(2 * ei + 1), _ext(u)] = 0 if out else expl
        Bout[_ext(2 * ei + 1), _ext(v)] = -one

        BT[_ext(u), _ext(2 * ei)] = -one
        BT[_ext(v), _ext(2 * ei)] = expl
        BT[_ext(u), _ext(2 * ei + 1)] = expl
        BT[_ext(v), _ext(2 * ei + 1)] = -one

    return BT, Bout


def construct_so3_weight_matrix(graph, with_k=True, abelian_scale=1.0):
    """Construct SO3 weight matrix."""
    DIM = 3

    def _ext(i):
        return slice(DIM * i, DIM * (i + 1))

    Winv = sparse.lil_matrix(
        (len(graph.edges) * 2 * DIM, len(graph.edges) * 2 * DIM), dtype=np.complex128
    )
    for ei, _ in enumerate(graph.edges):
        chi = graph.graph["ks"][ei]
        length = graph.graph["lengths"][ei]

        w_perp = Ad(2.0 * length * chi).dot(proj_perp(chi))
        w_paral = abelian_scale * np.exp(2.0j * length * norm(chi)) * proj_paral(chi)
        w = w_perp + w_paral - np.eye(3)

        winv = linalg.inv(w)

        if with_k:
            winv = (chi.dot(proj_perp(chi)) + 1.0j * norm(chi) * proj_paral(chi)).dot(winv)

        Winv[_ext(2 * ei), _ext(2 * ei)] = winv
        Winv[_ext(2 * ei + 1), _ext(2 * ei + 1)] = winv
    return Winv


def construct_so3_laplacian(wavenumber, graph, abelian_scale=1.0):
    """Construct quantum laplacian from a graph."""
    set_so3_wavenumber(graph, wavenumber)
    BT, B = construct_so3_incidence_matrix(graph, abelian_scale=abelian_scale)
    Winv = construct_so3_weight_matrix(graph, abelian_scale=abelian_scale)
    return BT.dot(Winv).dot(B)