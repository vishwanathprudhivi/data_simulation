"""Domain onboarding kit (M12): AC-DOM-01/02, FR-DOM-04/05/06/07/08/10."""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from simcore.cli.main import cli
from simcore.core.config import ConfigError, load_scenario
from simcore.core.plugins import PluginRegistry
from simcore.onboarding import fit_inputs, load_mapping, map_data
from simcore.onboarding.facility_csv import facility_from_csv, facility_usda
from simcore.specs.domain import DomainDefinition
from tests.conftest import EXAMPLES

GOOD = EXAMPLES / "onboarding" / "packaging_line"
BAD = EXAMPLES / "onboarding" / "packaging_line_misspecified"


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


@pytest.fixture(scope="module")
def good_report(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Any]]:
    out = tmp_path_factory.mktemp("report")
    res = _run("domain", "backtest", str(GOOD), "--out", str(out))
    yield out, res


def test_AC_DOM_01_tier0_domain_from_yaml_and_csvs_to_validation_report(
    good_report: tuple[Path, Any],
) -> None:
    out, res = good_report
    assert res.exit_code == 0, res.output
    assert "Validation Report: PASS" in res.output
    report = json.loads((out / "validation_report.json").read_text())
    assert report["rating"] == "pass" and report["periods"] == ["S01", "S02", "S03", "S04"]
    (kpi,) = report["kpis"]
    assert kpi["metric"] == "line_output" and kpi["mape"] < 0.10 and kpi["coverage"] >= 0.75
    assert report["envelope"] == [{"lever": "operators", "min": 2.0, "max": 5.0}]
    md = (out / "validation_report.md").read_text()
    assert "Overall: PASS" in md and "Calibrated envelope" in md and "Input models" in md
    # the fitted draft is a valid, loadable Tier-0 domain with the derived envelope
    fitted = DomainDefinition.model_validate(
        yaml.safe_load((out / "domain.fitted.yaml").read_text())
    )
    assert fitted.version == "0.9.1" and fitted.tier == 0
    assert [(b.lever, b.min, b.max) for b in fitted.envelope] == [("operators", 2.0, 5.0)]
    reg = PluginRegistry()
    reg.load_tier0(out / "domain.fitted.yaml")
    assert "packaging_line" in reg.names
    # zero Python in the workspace: only YAML and CSV
    # (generate_data.py only produces the synthetic sample data)
    assert [p.name for p in GOOD.glob("*.py")] == ["generate_data.py"]


def test_AC_DOM_02_misspecified_domain_fails_with_actionable_report(tmp_path: Path) -> None:
    res = _run("domain", "backtest", str(BAD), "--out", str(tmp_path))
    assert res.exit_code == 1
    report = json.loads((tmp_path / "validation_report.json").read_text())
    assert report["rating"] == "fail" and report["envelope"] == []
    titles = [d["title"] for d in report["diagnostics"] if d["severity"] == "fail"]
    assert (
        "station case_packer: capacity 1 in domain.yaml, 2 parallel servers in the data" in titles
    )
    actions = " ".join(d["action"] for d in report["diagnostics"])
    assert "set `capacity: 2` for station case_packer" in actions
    assert "set `batch_size: 2` for station case_packer" in actions
    assert report["kpis"][0]["bias"] < -0.2  # under-predicts output in every period
    md = (tmp_path / "validation_report.md").read_text()
    assert "Overall: FAIL" in md and "not trusted anywhere" in md


def test_FR_DOM_04_data_quality_report_on_the_sample_exports() -> None:
    spec = load_mapping(GOOD / "mapping.yaml")
    defn = DomainDefinition.model_validate(yaml.safe_load((GOOD / "domain.yaml").read_text()))
    data, dq = map_data(spec, GOOD, defn)
    checks = {(i.source, i.check) for i in dq.issues}
    assert ("station_log", "missing_values") in checks  # the row without an end time
    assert ("station_log", "duplicates") in checks
    assert ("shifts", "timezone") in checks  # naive local times were interpreted in Berlin
    assert dq.ok
    assert set(data.service_times) == {"depal", "filler", "capper", "labeler", "case_packer"}
    assert data.servers["case_packer"] == {"CASE_PACKER-1", "CASE_PACKER-2"}
    assert [p.duration for p in data.periods] == [8 * 3600.0] * 4
    # 06:00 Berlin (CET) is 05:00 UTC
    assert data.periods[0].start % 86400 == 5 * 3600


def _write(path: Path, header: str, rows: list[str]) -> None:
    path.write_text("\n".join([header, *rows]) + "\n")


def test_FR_DOM_04_dq_errors_are_specific(tmp_path: Path) -> None:
    defn = DomainDefinition.model_validate(yaml.safe_load((GOOD / "domain.yaml").read_text()))
    rows = [f"filler,2026-01-01T06:{m:02d}:00,2026-01-01T06:{m:02d}:12" for m in range(30)]
    rows += ["mixer,2026-01-01T07:00:00,2026-01-01T07:00:10", "filler,yesterday,today",
             "filler,2026-01-01T08:00:10,2026-01-01T08:00:00"]  # fmt: skip
    _write(tmp_path / "ev.csv", "Station,Begin,Finish", rows)
    # capper times logged ~60x too long (e.g. a minutes column read as seconds)
    slow = [
        f"capper,2026-01-0{1 + k // 6}T{6 + k % 6:02d}:00:00,2026-01-0{1 + k // 6}T"
        f"{6 + k % 6:02d}:09:40"
        for k in range(30)
    ]
    _write(tmp_path / "slow.csv", "Station,Begin,Finish", slow)
    spec = load_mapping_from(
        tmp_path,
        {
            "ev": {"path": "ev.csv", "kind": "station_events",
                   "columns": {"station": "Station", "start": "Begin", "end": "Finish"}},
            "slow": {"path": "slow.csv", "kind": "station_events",
                     "columns": {"station": "Station", "start": "Begin", "end": "Finish"}},
            "missing": {"path": "ev.csv", "kind": "arrivals", "columns": {"time": "Released"}},
        },
    )  # fmt: skip
    _, dq = map_data(spec, tmp_path, defn)
    by = {(i.source, i.check): i for i in dq.issues}
    assert by[("ev", "unknown_station")].severity == "error"
    assert "mixer" in by[("ev", "unknown_station")].message
    assert by[("ev", "timestamps")].rows == (33,)
    assert by[("ev", "durations")].rows == (34,)
    assert by[("missing", "columns")].hint == "available columns: ['Station', 'Begin', 'Finish']"
    units = by[("station_events", "units")]
    assert "capper" in units.message and "minutes/seconds" in units.hint
    assert not dq.ok


def load_mapping_from(tmp: Path, sources: dict[str, Any]) -> Any:
    path = tmp / "mapping.yaml"
    path.write_text(yaml.safe_dump({"domain": "packaging_line", "sources": sources}))
    return load_mapping(path)


def test_FR_DOM_05_auto_input_modeling_recovers_the_truth() -> None:
    spec = load_mapping(GOOD / "mapping.yaml")
    draft = DomainDefinition.model_validate(yaml.safe_load((GOOD / "domain.yaml").read_text()))
    truth = DomainDefinition.model_validate(
        yaml.safe_load((EXAMPLES.parent / "domains" / "packaging_line" / "domain.yaml").read_text())
    )
    data, _ = map_data(spec, GOOD, draft)
    model = fit_inputs(data, draft)
    from simcore.onboarding.mapping import _default_means

    true_means = _default_means(truth)
    for st in ("depal", "filler", "capper", "labeler"):
        fi = model.get(f"{st}.service_time")
        assert fi is not None and fi.report is not None
        assert fi.report.summary["mean"] == pytest.approx(true_means[st], rel=0.03), st
    assert "filler.mtbf" in model.skipped or model.get("filler.mtbf") is not None
    assert model.get("arrivals.A.interarrival") is not None
    md = model.markdown()
    assert "| filler.service_time |" in md


def test_FR_DOM_07_facility_from_csv_and_usd(tmp_path: Path) -> None:
    _write(tmp_path / "nodes.csv", "id,x,y,zone", ["a,0,0,z1", "b,10,0,z1", "c,10,5,"])
    _write(tmp_path / "edges.csv", "id,from,to,width,directed,surface",
           ["e1,a,b,2.5,false,concrete", "e2,b,c,3,true,", "e3,c,a,3,false,"])  # fmt: skip
    _write(
        tmp_path / "zones.csv", "id,kind,polygon,capacity", ["z1,aisle,0 -1; 10 -1; 10 1; 0 1,4"]
    )
    _write(tmp_path / "locations.csv", "id,kind,node,capacity", ["dock,dock,c,2"])
    m = facility_from_csv(tmp_path, "csv_site")
    assert [e.id for e in m.edges] == ["e1", "e2", "e3"] and m.edges[1].directed
    assert m.edges[0].attributes == {"surface": "concrete"} and m.zones[0].capacity == 4
    usda = facility_usda(m)
    assert usda.startswith("#usda 1.0") and 'def Cube "edge_e1"' in usda and "zone_z1" in usda
    out, usd = str(tmp_path / "facility.yaml"), str(tmp_path / "site.usda")
    res = _run("domain", "ingest-facility", str(tmp_path), "--id", "csv_site", "--out", out,
               "--usd", usd)  # fmt: skip
    assert res.exit_code == 0, res.output
    assert yaml.safe_load((tmp_path / "facility.yaml").read_text())["usd_stage"].endswith(
        "site.usda"
    )
    _write(tmp_path / "edges.csv", "id,from,to,width", ["e1,a,nowhere,2"])
    with pytest.raises(ConfigError):
        facility_from_csv(tmp_path, "broken")


@pytest.mark.parametrize("tier", ["0", "1", "2"])
def test_FR_DOM_08_init_scaffolds_valid_workspace(tmp_path: Path, tier: str) -> None:
    res = _run("domain", "init", "Cold Chain Hub", "--tier", tier, "--dir", str(tmp_path))
    assert res.exit_code == 0, res.output
    root = tmp_path / "cold_chain_hub"
    defn = DomainDefinition.model_validate(yaml.safe_load((root / "domain.yaml").read_text()))
    assert defn.id == "cold_chain_hub" and defn.tier == int(tier)
    assert (root / "__init__.py").exists() == (tier != "0")
    assert load_mapping(root / "mapping.yaml").domain == "cold_chain_hub"
    assert "simcore domain backtest" in (root / "CHECKLIST.md").read_text()
    if tier == "0":  # the scaffold runs as is
        reg = PluginRegistry()
        reg.load_tier0(root / "domain.yaml")
        bundle = load_scenario(root / "scenarios" / "baseline.yaml")
        from simcore.api import run_scenario

        assert run_scenario(bundle, plugins=reg).kpis["tasks_completed"] > 0
    res = _run("domain", "validate", str(root))
    assert res.exit_code == 1 and "does not exist" in res.output  # no data exported yet
    assert _run("domain", "init", "Cold Chain Hub", "--dir", str(tmp_path)).exit_code == 1


def test_FR_DOM_08_validate_sample_workspace() -> None:
    res = _run("domain", "validate", str(GOOD))
    assert res.exit_code == 0, res.output
    assert "definition OK" in res.output and "missing_values" in res.output


def test_FR_DOM_10_publish_gate(tmp_path: Path, good_report: tuple[Path, Any]) -> None:
    out, _ = good_report
    dest = tmp_path / "registry"
    res = _run("domain", "publish", str(GOOD), "--to", str(dest), "--fitted", "--report", str(out))
    assert res.exit_code == 0, res.output
    published = dest / "packaging_line" / "versions" / "0.9.1"
    assert (published / "domain.yaml").exists() and (published / "validation_report.json").exists()
    again = _run(
        "domain", "publish", str(GOOD), "--to", str(dest), "--fitted", "--report", str(out)
    )
    assert again.exit_code == 1 and "already published" in again.output
    reg = PluginRegistry()
    assert reg.discover_tier0([dest]) == ["packaging_line"]
    assert reg.get("packaging_line").version == "0.9.1"
    # a failing report blocks publishing
    bad_out = tmp_path / "bad"
    _run("domain", "backtest", str(BAD), "--out", str(bad_out))
    res = _run("domain", "publish", str(BAD), "--to", str(dest), "--report", str(bad_out))
    assert res.exit_code == 1 and "FAIL" in res.output
    assert _run("domain", "publish", str(BAD), "--to", str(dest)).exit_code == 1  # no report


def test_sample_data_is_synthetic_and_reproducible(tmp_path: Path) -> None:
    text = (GOOD / "generate_data.py").read_text()
    assert "synthetic" in text.lower()
    shifts = (GOOD / "data" / "shifts.csv").read_text().splitlines()
    assert shifts[0] == "Shift,Start,End,Operators,Trays packed" and len(shifts) == 5
    outputs = [int(r.split(",")[-1]) for r in shifts[1:]]
    assert statistics.fmean(outputs) > 1000
