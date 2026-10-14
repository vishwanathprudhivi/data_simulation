"""AC-EXP-04 and FR-EXP-08: distribution fitting recovers parameters and ranks families."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from simcore.experiment.fitting import fit_distributions, read_samples


def test_AC_EXP_04_lognormal_recovered_within_3pct_and_ranked_first() -> None:
    rng = np.random.default_rng(20240501)
    mu, sigma = 1.2, 0.5
    report = fit_distributions(rng.lognormal(mu, sigma, size=5000))
    best = report.best
    assert best.name == "lognormal"
    assert best.params["mu"] == pytest.approx(mu, rel=0.03)
    assert best.params["sigma"] == pytest.approx(sigma, rel=0.03)
    # the emitted spec is directly usable by scenarios
    spec = best.distribution()
    assert spec.kind == "lognormal"


@pytest.mark.parametrize(
    ("family", "draw"),
    [
        ("gamma", lambda g: g.gamma(4.0, 2.5, size=5000)),
        ("weibull", lambda g: 3.0 * g.weibull(1.6, size=5000)),
        ("exponential", lambda g: g.exponential(7.0, size=5000)),
        ("normal", lambda g: g.normal(50.0, 4.0, size=5000)),
    ],
)
def test_FR_EXP_08_ranks_true_family_first(family: str, draw) -> None:  # type: ignore[no-untyped-def]
    report = fit_distributions(draw(np.random.default_rng(3)))
    assert report.best.name == family, [c.name for c in report.candidates]
    assert report.best.ks_pvalue > 0.01
    assert len(report.best.qq) == 50


def test_FR_EXP_08_units_and_non_positive_data() -> None:
    rng = np.random.default_rng(5)
    minutes = rng.exponential(2.0, size=2000)
    report = fit_distributions(minutes, unit="min")
    assert report.summary["mean"] == pytest.approx(minutes.mean() * 60)
    signed = rng.normal(0.0, 1.0, size=500)
    r2 = fit_distributions(signed)
    assert r2.best.name == "normal" and "lognormal" in r2.skipped
    with pytest.raises(ValueError, match="at least 10"):
        fit_distributions([1.0, 2.0])


def test_read_samples_csv_and_plain(tmp_path: Path) -> None:
    csv = tmp_path / "d.csv"
    csv.write_text("id,cycle\n1,3.5\n2,\n3,4.0\n")
    assert read_samples(csv, "cycle") == [3.5, 4.0]
    with pytest.raises(ValueError, match="available"):
        read_samples(csv, "nope")
    plain = tmp_path / "d.txt"
    plain.write_text("1.0\n2.5\n\n3\n")
    assert read_samples(plain) == [1.0, 2.5, 3.0]
