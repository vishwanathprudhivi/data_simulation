"""Experiment layer: designs, replications, CRN, statistics, fitting, reports (FR-EXP-*)."""

from simcore.experiment.design import BASELINE_ID, Variant, build_variants, seed_for
from simcore.experiment.evaluator import Evaluation, Evaluator
from simcore.experiment.fitting import FitReport, fit_distributions
from simcore.experiment.runner import ExperimentResult, ExperimentRunner
from simcore.experiment.stats import CIEstimate, mean_ci, paired_ci, welch_ci
from simcore.experiment.warmup import WarmupDiagnostic, welch_diagnostic

__all__ = [
    "BASELINE_ID",
    "CIEstimate",
    "Evaluation",
    "Evaluator",
    "ExperimentResult",
    "ExperimentRunner",
    "FitReport",
    "Variant",
    "WarmupDiagnostic",
    "build_variants",
    "fit_distributions",
    "mean_ci",
    "paired_ci",
    "seed_for",
    "welch_ci",
    "welch_diagnostic",
]
