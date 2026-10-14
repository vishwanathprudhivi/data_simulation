"""AC-EXP-03: a CRN paired comparison gives a materially narrower CI than independent seeds.

Same experiment twice (baseline vs a 4% lower arrival rate, 10 replications),
once with CRN and once with independent seeds. "Materially" is set at a factor
of 2 for the gate; measured ratios are recorded in the CHANGELOG.
"""

from __future__ import annotations

from pathlib import Path

from simcore.core.config import load_scenario
from simcore.core.plugins import PluginRegistry
from simcore.experiment.runner import ExperimentRunner
from simcore.specs.experiment import ExperimentSpec
from tests.conftest import MMC

METRICS = ("wait_time_mean", "throughput", "utilization")


def _run(crn: bool, plugins: PluginRegistry, out: Path | None = None):  # type: ignore[no-untyped-def]
    spec = ExperimentSpec(
        id=f"crn_{crn}",
        base_scenario=str(MMC),
        variants=[
            {
                "id": "slower_arrivals",
                "overrides": {"demand.demand.interarrival.mean": 0.4340277777777778},
            }
        ],
        replications=10,
        master_seed=777,
        crn=crn,
    )
    return ExperimentRunner(spec, load_scenario(MMC), plugins, out_root=out, parallelism=4).run()


def test_AC_EXP_03_crn_narrows_paired_ci(tmp_path: Path, plugins: PluginRegistry) -> None:
    crn = _run(True, plugins, tmp_path)
    ind = _run(False, plugins)
    for m in METRICS:
        paired = crn.comparison("slower_arrivals", m)
        independent = ind.comparison("slower_arrivals", m)
        assert paired.paired and not independent.paired
        ratio = independent.diff.half_width / paired.diff.half_width
        assert ratio > 2.0, (m, ratio)
        # Same data with pairing ignored is also much wider: the report shows this column.
        assert paired.unpaired_half_width is not None
        assert paired.unpaired_half_width > 2.0 * paired.diff.half_width, m
    # with CRN even this small change is detected; without it, it is not on wait time
    assert crn.comparison("slower_arrivals", "wait_time_mean").ci_excludes_zero
    report = (tmp_path / "crn_True" / "report.md").read_text()
    assert "x narrower" in report and "unpaired" in report
