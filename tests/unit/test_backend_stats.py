"""Logical backend motion (FR-ENG-09) and replication statistics warnings (FR-EXP-04/10)."""

from __future__ import annotations

import math

import pytest

from simcore.adapters import get_backend
from simcore.adapters.logical import LogicalBackend
from simcore.experiment.stats import LowReplicationWarning, mean_ci, paired_ci


def test_FR_ENG_09_logical_backend_interpolates_moves() -> None:
    b = LogicalBackend()
    b.build(None)
    b.spawn_agent("a", "t", (0.0, 0.0))
    assert b.position("a", 5.0) == (0.0, 0.0)
    b.move("a", [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)], [0.0, 10.0, 15.0])
    assert b.position("a", 5.0) == pytest.approx((5.0, 0.0))
    assert b.position("a", 12.5) == pytest.approx((10.0, 2.5))
    assert b.position("a", 99.0) == (10.0, 5.0)
    with pytest.raises(ValueError, match="overlapping"):
        b.move("a", [(10.0, 5.0), (0.0, 0.0)], [14.0, 20.0])
    with pytest.raises(ValueError, match="already spawned"):
        b.spawn_agent("a", "t", None)


def test_FR_ENG_09_backend_selection() -> None:
    assert get_backend("logical").mode == "logical"
    with pytest.raises(ValueError, match="unknown mode"):
        get_backend("cartoon")


def test_FR_EXP_04_t_interval() -> None:
    ci = mean_ci([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    # mean 3.5, sd 1.8708, t(0.975, 5) = 2.5706 => half width 1.9633
    assert ci.mean == pytest.approx(3.5) and ci.half_width == pytest.approx(1.96333, rel=1e-4)
    assert ci.excludes_zero and not ci.warnings


def test_FR_EXP_10_low_n_and_degenerate_variance_warn() -> None:
    with pytest.warns(LowReplicationWarning, match="not meaningful"):
        ci = mean_ci([1.0, 2.0, 3.0])
    assert any("not meaningful" in w for w in ci.warnings)
    with pytest.warns(LowReplicationWarning, match="zero variance"):
        mean_ci([2.0] * 6)
    with pytest.warns(LowReplicationWarning, match="undefined"):
        assert math.isinf(mean_ci([1.0]).half_width)


def test_FR_EXP_05_paired_ci_sign_and_length_check() -> None:
    ci = paired_ci([1.0, 2.0, 3.0, 4.0, 5.0], [1.5, 2.4, 3.6, 4.5, 5.4])
    assert ci.mean == pytest.approx(0.48) and ci.excludes_zero
    with pytest.raises(ValueError, match="equal lengths"):
        paired_ci([1.0], [1.0, 2.0])
