"""Tasks for lasing modes."""
import pickle

import luigi
import numpy as np
import yaml

from netsalt.io import (
    load_mode_competition_matrix,
    load_modes,
    save_mode_competition_matrix,
    save_modes,
)
from netsalt.modes import (
    compute_modal_intensities,
    compute_mode_competition_matrix,
    find_threshold_lasing_modes,
    pump_trajectories,
)

from .netsalt_task import NetSaltTask
from .passive import CreateQuantumGraph, FindPassiveModes
from .pump import OptimizePump


class CreatePumpProfile(NetSaltTask):
    """Create a pump profile."""

    mode = luigi.ChoiceParameter(
        default="uniform", choices=["uniform", "optimized", "custom"]
    )
    custom_pump_path = luigi.Parameter(default="pump_profile.yaml")
    lasing_modes_id = luigi.ListParameter(default=[0])

    def requires(self):
        """"""
        if self.mode == "uniform":
            return {"graph": CreateQuantumGraph()}
        if self.mode == "optimized":
            return {"optimize": OptimizePump(lasing_modes_id=self.lasing_modes_id)}

    def run(self):
        """"""
        if self.mode == "uniform":
            qg = self.get_graph(self.input()["graph"].path)
            pump = np.zeros(len(qg.edges()))
            for i, (u, v) in enumerate(qg.edges()):
                if qg[u][v]["inner"]:
                    pump[i] = 1
        if self.mode == "optimized":
            results = pickle.load(open(self.input()["optimize"].path, "rb"))
            pump = results["optimal_pump"].tolist()
        if self.mode == "custom":
            pump = yaml.load(open(self.custom_pump_path, "r"))
        yaml.dump(pump, open(self.output().path, "w"))


class ComputeModeTrajectories(NetSaltTask):
    """Compute mode trajectories from passive modes."""

    lasing_modes_id = luigi.ListParameter(default=[0])

    def requires(self):
        """"""
        return {
            "graph": CreateQuantumGraph(),
            "modes": FindPassiveModes(),
            "pump": CreatePumpProfile(lasing_modes_id=self.lasing_modes_id),
        }

    def run(self):
        """"""
        modes_df = load_modes(self.input()["modes"].path)
        qg = self.get_graph_with_pump(self.input()["graph"].path)

        modes_df = pump_trajectories(modes_df, qg, return_approx=True)
        save_modes(modes_df, filename=self.output().path)


class FindThresholdModes(NetSaltTask):
    """Find the lasing thresholds and associated modes."""

    lasing_modes_id = luigi.ListParameter(default=[0])

    def requires(self):
        """"""
        return {
            "graph": CreateQuantumGraph(),
            "modes": ComputeModeTrajectories(),
            "pump": CreatePumpProfile(lasing_modes_id=self.lasing_modes_id),
        }

    def run(self):
        """"""
        qg = self.get_graph_with_pump(self.input()["graph"].path)
        modes_df = load_modes(self.input()["modes"].path)
        modes_df = find_threshold_lasing_modes(modes_df, qg)
        save_modes(modes_df, filename=self.output().path)


class ComputeModeCompetitionMatrix(NetSaltTask):
    """Compute the mode competition matrix."""

    lasing_modes_id = luigi.ListParameter(default=[0])

    def requires(self):
        """"""
        return {
            "graph": CreateQuantumGraph(),
            "modes": FindThresholdModes(lasing_modes_id=self.lasing_modes_id),
            "pump": CreatePumpProfile(lasing_modes_id=self.lasing_modes_id),
        }

    def run(self):
        """"""

        qg = self.get_graph_with_pump(self.input()["graph"].path)
        modes_df = load_modes(self.input()["modes"].path)
        mode_competition_matrix = compute_mode_competition_matrix(qg, modes_df)
        save_mode_competition_matrix(
            mode_competition_matrix, filename=self.output().path
        )


class ComputeModalIntensities(NetSaltTask):
    """Compute modal intensities as a function of pump strenght."""

    lasing_modes_id = luigi.ListParameter(default=[0])
    D0_max = luigi.FloatParameter(default=0.1)

    def requires(self):
        """"""
        return {
            "modes": FindThresholdModes(lasing_modes_id=self.lasing_modes_id),
            "competition_matrix": ComputeModeCompetitionMatrix(
                lasing_modes_id=self.lasing_modes_id
            ),
        }

    def run(self):
        """"""
        modes_df = load_modes(self.input()["modes"].path)
        mode_competition_matrix = load_mode_competition_matrix(
            self.input()["competition_matrix"].path
        )
        modes_df = compute_modal_intensities(
            modes_df, self.D0_max, mode_competition_matrix
        )

        save_modes(modes_df, filename=self.output().path)
