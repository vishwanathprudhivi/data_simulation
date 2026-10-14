"""Domain onboarding kit (BRD 6.16, M12): data mapping, auto input modeling,
backtest / Validation Report, facility ingestion and the ``simcore domain`` CLI.

Everything here is domain-agnostic; a Tier-0 domain needs no Python at all.
"""

from simcore.onboarding.backtest import ValidationReport, backtest
from simcore.onboarding.inputs import InputModel, apply_inputs, fit_inputs
from simcore.onboarding.mapping import DQReport, MappedData, load_mapping, map_data

__all__ = [
    "DQReport",
    "InputModel",
    "MappedData",
    "ValidationReport",
    "apply_inputs",
    "backtest",
    "fit_inputs",
    "load_mapping",
    "map_data",
]
