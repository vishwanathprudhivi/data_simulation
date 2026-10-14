"""Warm-up diagnostics (FR-EXP-06) and the M2 CLI commands (FR-IFC-01)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from click.testing import CliRunner

from simcore.api import run_scenario
from simcore.cli.main import cli
from simcore.core.config import load_scenario
from simcore.core.plugins import PluginRegistry
from simcore.experiment.warmup import (
    signal_series,
    suggest_warmup,
    welch_diagnostic,
    welch_moving_average,
)
from tests.conftest import EXAMPLES, MMC


def test_FR_EXP_06_welch_moving_average_shrinking_window() -> None:
    m = np.array([[0.0, 2.0, 4.0, 6.0, 8.0], [2.0, 4.0, 6.0, 8.0, 10.0]])  # ybar 1,3,5,7,9
    ma = welch_moving_average(m, 1)
    assert ma.tolist() == pytest.approx([1.0, 3.0, 5.0, 7.0])
    with pytest.raises(ValueError):
        welch_moving_average(m, 5)


def test_FR_EXP_06_mser_finds_step() -> None:
    rng = np.random.default_rng(0)
    y = np.concatenate([np.linspace(10, 2, 10), 2 + 0.1 * rng.standard_normal(90)])
    d = suggest_warmup(y, bucket=10.0)
    assert d is not None and 80 <= d <= 130


def test_FR_EXP_06_signal_series_queue_length() -> None:
    result = run_scenario(MMC)
    q = signal_series(result.events, "queue_length", 100.0, 2200.0)
    assert len(q) == 22
    # bucket-averaged levels integrate to the overall time average
    assert q.mean() == pytest.approx(
        run_scenario(MMC.parent / "mmc_baseline.yaml", overrides={"warmup": 0}).kpis[
            "queue_length_mean"
        ],
        rel=1e-9,
    )
    with pytest.raises(ValueError, match="unknown warm-up signal"):
        signal_series(result.events, "vibes", 100.0, 2200.0)


def test_FR_EXP_06_backlog_transient_detected(tmp_path: Path, plugins: PluginRegistry) -> None:
    """Start with 300 queued jobs: the diagnostic must place warm-up after the backlog drains."""
    (tmp_path / "behaviors.yaml").write_text((MMC.parent / "behaviors.yaml").read_text())
    doc = yaml.safe_load(MMC.read_text())
    doc["id"] = "backlog"
    doc["demand"].append(
        {
            "kind": "list",
            "stream": "backlog",
            "tasks": [{"id": f"b{i}", "release_time": 0.0} for i in range(300)],
        }
    )
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(doc))
    diag = welch_diagnostic(
        load_scenario(tmp_path / "s.yaml"),
        plugins,
        replications=5,
        signal="queue_length",
        bucket=20.0,
        window=3,
    )
    # 300 jobs drain at (3 - 2.4) jobs/s => ~500 s
    assert diag.suggested is not None and 300 <= diag.suggested <= 900
    assert len(diag.moving_average) == int(2200 / 20) - 3


def test_cli_experiment_report_and_warmup(tmp_path: Path) -> None:
    exp = tmp_path / "exp.yaml"
    exp.write_text(
        yaml.safe_dump(
            {
                "id": "cli_exp",
                "base_scenario": str(MMC),
                "replications": 3,
                "run_length": 300,
                "variants": [{"id": "four", "overrides": {"fleet.server.count": 4}}],
                "kpis": ["wait_time_mean"],
            }
        )
    )
    runner = CliRunner()
    res = runner.invoke(cli, ["experiment", str(exp), "--out", str(tmp_path / "runs")])
    assert res.exit_code == 0, res.output
    assert "four vs baseline wait_time_mean" in res.output
    exp_dir = tmp_path / "runs" / "cli_exp"
    (exp_dir / "report.md").unlink()
    res = runner.invoke(cli, ["report", str(exp_dir)])
    assert res.exit_code == 0 and (exp_dir / "report.md").exists()
    res = runner.invoke(
        cli,
        [
            "warmup",
            str(MMC),
            "--replications",
            "2",
            "--bucket",
            "100",
            "--experiment-dir",
            str(exp_dir),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "<svg" in (exp_dir / "report.html").read_text()
    assert json.loads((exp_dir / "summary.json").read_text())["warmup"]["signal"] == (
        "queue_length"
    )


def test_cli_fit(tmp_path: Path) -> None:
    data = tmp_path / "service.csv"
    rng = np.random.default_rng(1)
    data.write_text("minutes\n" + "\n".join(f"{v:.5f}" for v in rng.gamma(3, 2, 3000)))
    out = tmp_path / "dist.yaml"
    res = CliRunner().invoke(cli, ["fit", str(data), "--unit", "min", "--out", str(out)])
    assert res.exit_code == 0, res.output
    spec = yaml.safe_load(out.read_text())
    assert spec["kind"] == "gamma" and spec["scale"] == pytest.approx(120, rel=0.08)
    res = CliRunner().invoke(cli, ["fit", str(data), "--json"])
    assert json.loads(res.output)["best"] == "gamma"


def test_cli_experiment_example_validates() -> None:
    res = CliRunner().invoke(cli, ["validate", str(EXAMPLES / "experiments/mmc_servers.yaml")])
    assert res.exit_code == 0, res.output
