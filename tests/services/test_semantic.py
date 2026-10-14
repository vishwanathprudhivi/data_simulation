"""Semantic layer and visualization (M8): FR-SEM-01..05, FR-VIZ-01..06, AC-SEM-01, AC-VIZ-01."""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import vl_convert as vlc
import yaml
from fastapi.testclient import TestClient
from services.api.app import create_app
from services.platform.settings import Settings

from simcore.core.plugins import PluginRegistry
from tests.conftest import EXAMPLES, ROOT

WH_EXP = {
    "id": "wh_sem",
    "replications": 2,
    "run_length": 1800,
    "master_seed": 3,
    "variants": [{"id": "more", "overrides": {"fleet.pallet_jack_operator.count": 16}}],
}
MMC_EXP = {
    "id": "mmc_sem",
    "replications": 6,
    "run_length": 600,
    "warmup": 60,
    "variants": [{"id": "four", "overrides": {"fleet.server.count": 4}}],
}


def _wait(c: TestClient, job_id: str) -> None:
    end = time.time() + 120
    while c.get(f"/api/v1/jobs/{job_id}").json()["status"] not in ("completed", "failed"):
        assert time.time() < end
        time.sleep(0.05)
    assert c.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, str, str]]:
    tmp = tmp_path_factory.mktemp("sem")
    settings = Settings(
        data_dir=tmp / "data", examples_dir=EXAMPLES, cpu_workers=2, sql_enabled=True
    )
    with TestClient(create_app(settings)) as c:
        wh = c.post(
            "/api/v1/experiments",
            json={
                "experiment": WH_EXP,
                "scenario": {"example": "scenarios/warehouse/narrow_aisles.yaml"},
            },
        ).json()["id"]
        mmc = c.post(
            "/api/v1/experiments",
            json={
                "experiment": MMC_EXP,
                "scenario": {"example": "scenarios/toy_queue/mmc_baseline.yaml"},
            },
        ).json()["id"]
        _wait(c, wh)
        _wait(c, mmc)
        yield c, wh, mmc


def _q(c: TestClient, job: str, **query: Any) -> Any:
    return c.post("/api/v1/query", json={"job_id": job, "query": query})


def test_FR_SEM_03_05_replication_aware_kpi_query(env: Any) -> None:
    c, _, mmc = env
    r = _q(
        c,
        mmc,
        metrics=["wait_time_mean"],
        dimensions=["variant"],
        comparison={"kind": "paired_vs_baseline", "baseline": "baseline"},
    ).json()
    rows = {row["variant"]: row for row in r["rows"]}
    four = rows["four"]
    assert four["wait_time_mean.n"] == 6
    assert four["wait_time_mean.ci_low"] < four["wait_time_mean"] < four["wait_time_mean.ci_high"]
    assert four["wait_time_mean.diff"] < 0 and four["wait_time_mean.ci_excludes_zero"] is True
    ind = _q(
        c,
        mmc,
        metrics=["wait_time_mean"],
        dimensions=["variant"],
        comparison={"kind": "independent_vs_baseline", "baseline": "baseline"},
    ).json()
    w_ind = {x["variant"]: x for x in ind["rows"]}["four"]
    paired_w = four["wait_time_mean.diff_ci_high"] - four["wait_time_mean.diff_ci_low"]
    ind_w = w_ind["wait_time_mean.diff_ci_high"] - w_ind["wait_time_mean.diff_ci_low"]
    assert paired_w <= ind_w
    raw = _q(c, mmc, metrics=["utilization"], dimensions=["variant", "replication"]).json()
    assert len(raw["rows"]) == 12 and "utilization.ci_low" not in raw["columns"]


def test_FR_SEM_03_event_metrics_buckets_filters_scope(env: Any) -> None:
    c, wh, _ = env
    r = _q(
        c, wh, metrics=["completions"], dimensions=["variant", "time_bucket"], time_bucket=600
    ).json()
    assert {row["time_bucket"] for row in r["rows"]} == {0.0, 600.0, 1200.0, 1800.0}
    total = sum(row["completions"] for row in r["rows"] if row["variant"] == "baseline")
    kpi = _q(c, wh, metrics=["tasks_completed"], dimensions=["variant"]).json()["rows"]
    base_kpi = {x["variant"]: x for x in kpi}["baseline"]["tasks_completed"]
    assert total == pytest.approx(base_kpi, abs=15)  # bucket counts sum to the KPI
    by_loc = _q(
        c,
        wh,
        metrics=["blocking_time"],
        dimensions=["location"],
        scope={"variants": ["baseline"]},
        filters=[{"dimension": "location", "op": "in", "value": ["e_c01_00", "e_c02_00"]}],
    ).json()
    assert {row["location"] for row in by_loc["rows"]} <= {"e_c01_00", "e_c02_00"}
    states = _q(c, wh, metrics=["state_time"], dimensions=["variant", "state"]).json()["rows"]
    assert {"travel", "pick", "drop", "idle"} <= {row["state"] for row in states}


def test_FR_SEM_04_provenance_and_stable_ref_ids(env: Any) -> None:
    c, _, mmc = env
    a = _q(c, mmc, metrics=["throughput"], dimensions=["variant"]).json()
    b = _q(c, mmc, metrics=["throughput"], dimensions=["variant"]).json()
    other = _q(c, mmc, metrics=["utilization"], dimensions=["variant"]).json()
    assert a["ref_id"] == b["ref_id"] != other["ref_id"] and a["ref_id"].startswith("q_")
    prov = a["provenance"]
    for key in (
        "job_id",
        "variants",
        "seeds",
        "domain",
        "engine_version",
        "query",
        "data_digest",
        "mode",
    ):
        assert key in prov, key
    assert set(prov["variants"]) == {"baseline", "four"} and len(prov["seeds"]) == 12


def test_query_errors_are_repair_ready(env: Any) -> None:
    c, _, mmc = env
    r = _q(c, mmc, metrics=["wait_tme_mean"], dimensions=["agent"])
    assert r.status_code == 422
    issue = r.json()["detail"]["issues"][0]
    assert issue["suggestion"] == "did you mean 'wait_time_mean'?" and issue["allowed"]
    r2 = _q(c, mmc, metrics=["completions"], dimensions=["time_bucket"])
    assert "needs query.time_bucket" in r2.text
    r3 = _q(c, mmc, metrics=["utilization"], dimensions=["agent"])
    assert "cannot be sliced" in r3.text or "only support" in r3.text


CHARTS = {
    "bar_ci": {"metrics": ["cycle_time_mean"], "dimensions": ["variant"]},
    "point_ci": {"metrics": ["utilization"], "dimensions": ["variant"]},
    "timeseries_band": {
        "metrics": ["completions"],
        "dimensions": ["variant", "time_bucket"],
        "time_bucket": 600,
    },
    "box": {"metrics": ["cycle_time_mean"], "dimensions": ["variant", "replication"]},
    "violin": {"metrics": ["cycle_time_mean"], "dimensions": ["variant", "replication"]},
    "histogram": {"metrics": ["cycle_time_mean"], "dimensions": ["variant", "replication"]},
    "ecdf": {"metrics": ["cycle_time_mean"], "dimensions": ["variant", "replication"]},
    "heatmap": {
        "metrics": ["blocking_time"],
        "dimensions": ["agent_type", "time_bucket"],
        "time_bucket": 600,
    },
    "stacked_breakdown": {"metrics": ["state_time"], "dimensions": ["variant", "state"]},
    "waterfall": {
        "metrics": ["cycle_time_mean", "blocked_time_per_agent"],
        "dimensions": ["variant"],
        "comparison": {"baseline": "baseline"},
    },
    "tornado": {
        "metrics": ["cycle_time_mean"],
        "dimensions": ["variant"],
        "comparison": {"baseline": "baseline"},
    },
    "pareto": {"metrics": ["pallets_per_labor_hour", "cycle_time_mean"], "dimensions": ["variant"]},
    "gantt": {
        "metrics": ["state_intervals"],
        "dimensions": ["agent"],
        "scope": {"variants": ["baseline"]},
        "limit": 400,
        "filters": [{"dimension": "replication", "op": "eq", "value": 0}],
    },
    "comparison_table": {
        "metrics": ["dock_on_time_rate"],
        "dimensions": ["variant"],
        "comparison": {"baseline": "baseline"},
    },
    "floorplan_overlay": {
        "metrics": ["edge_occupancy_time"],
        "dimensions": ["location"],
        "scope": {"variants": ["baseline"]},
    },
}


@pytest.mark.parametrize("template", sorted(CHARTS))
def test_FR_VIZ_02_every_template_renders_valid_vega_lite(env: Any, template: str) -> None:
    c, wh, _ = env
    chart = {"template": template, "title": template, "query": CHARTS[template]}
    r = c.post("/api/v1/charts/render", json={"job_id": wh, "chart": chart})
    assert r.status_code == 200, r.text
    body = r.json()
    vega = body["vega_lite"]
    assert vega["usermeta"]["ref_id"] == body["ref_id"] == body["query"]["ref_id"]
    svg = vlc.vegalite_to_svg(vega)  # throws if the Vega-Lite spec is invalid
    assert svg.startswith("<svg")
    assert body["query"]["provenance"]["job_id"] == wh  # FR-VIZ-04 show your work


def test_FR_VIZ_03_floorplan_with_time_scrubber(env: Any) -> None:
    c, wh, _ = env
    chart = {
        "template": "floorplan_overlay",
        "query": {
            "metrics": ["blocking_time"],
            "dimensions": ["location", "time_bucket"],
            "time_bucket": 600,
            "scope": {"variants": ["baseline"]},
        },
    }
    vega = c.post("/api/v1/charts/render", json={"job_id": wh, "chart": chart}).json()["vega_lite"]
    assert vega["params"][0]["name"] == "t" and vega["params"][0]["bind"]["input"] == "range"
    edges = vega["layer"][0]["data"]["values"]
    assert len({e["edge"] for e in edges}) == 92
    vlc.vegalite_to_svg(vega)


def test_AC_VIZ_01_unknown_metric_rejected_with_repair_ready_error(env: Any) -> None:
    c, wh, _ = env
    r = c.post(
        "/api/v1/charts/validate",
        json={
            "job_id": wh,
            "chart": {
                "template": "bar_ci",
                "query": {"metrics": ["pallet_per_labour_hour"], "dimensions": ["variant"]},
            },
        },
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["repairable"] is True
    issue = detail["issues"][0]
    assert issue["path"] == "query.metrics[0]"
    assert issue["suggestion"] == "did you mean 'pallets_per_labor_hour'?"
    assert "pallets_per_labor_hour" in issue["allowed"]


def test_FR_VIZ_01_05_06_spec_rules(env: Any) -> None:
    c, wh, _ = env

    def validate(chart: dict[str, Any], **kw: Any) -> Any:
        return c.post("/api/v1/charts/validate", json={"job_id": wh, "chart": chart, **kw})

    q = {"metrics": ["cycle_time_mean"], "dimensions": ["variant"]}
    inline = validate({"template": "bar_ci", "query": q, "data": {"values": [{"y": 1}]}})
    assert inline.status_code == 422 and "may not contain data" in inline.text
    bad_t = validate({"template": "pie", "query": q})
    assert "unknown template" in bad_t.text and "bar_ci" in bad_t.text
    box = validate({"template": "box", "query": q})
    assert "add 'replication'" in box.text
    no_unc = validate({"template": "bar_ci", "query": q, "show_uncertainty": False})
    assert no_unc.status_code == 422 and "FR-VIZ-06" in no_unc.text
    ok = validate(
        {"template": "bar_ci", "query": q, "show_uncertainty": False},
        user_requested_no_uncertainty=True,
    )
    assert ok.status_code == 200
    enc = validate({"template": "bar_ci", "query": q, "encoding": {"x": {"field": "colour"}}})
    assert "is not in the query" in enc.text
    assert set(c.get("/api/v1/charts/templates").json()) == set(CHARTS)


def test_FR_VIZ_04_exports(env: Any) -> None:
    c, _, mmc = env
    chart = {"template": "bar_ci", "query": {"metrics": ["utilization"], "dimensions": ["variant"]}}
    for fmt, ctype, magic in (
        ("csv", "text/csv", b"variant"),
        ("svg", "image/svg+xml", b"<svg"),
        ("png", "image/png", b"\x89PNG"),
        ("json", "application/json", b"{"),
    ):
        r = c.post("/api/v1/charts/export", json={"job_id": mmc, "chart": chart, "format": fmt})
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)
        assert r.content.startswith(magic), fmt


def test_FR_SEM_03_advanced_sql_sandbox(env: Any) -> None:
    c, _, mmc = env
    ok = c.post(
        "/api/v1/sql",
        json={
            "job_id": mmc,
            "sql": "SELECT variant, AVG(value) AS v "
            "FROM results WHERE metric = 'utilization' GROUP BY 1",
        },
    )
    assert ok.status_code == 200 and len(ok.json()["rows"]) == 2
    for bad in (
        "DELETE FROM results",
        "SELECT 1; SELECT 2",
        "SELECT * FROM read_parquet('/etc/passwd')",
        "COPY results TO '/tmp/x.csv'",
        "SELECT getenv('HOME')",
    ):
        r = c.post("/api/v1/sql", json={"job_id": mmc, "sql": bad})
        assert r.status_code == 422, bad
    capped = c.post(
        "/api/v1/sql", json={"job_id": mmc, "sql": "SELECT * FROM events", "max_rows": 10}
    ).json()
    assert capped["truncated"] and len(capped["rows"]) == 10


def test_AC_SEM_01_new_domain_metric_reaches_api_and_charts(tmp_path: Path) -> None:
    dom = tmp_path / "domains" / "packaging_line"
    dom.mkdir(parents=True)
    doc = yaml.safe_load((ROOT / "domains/packaging_line/domain.yaml").read_text())
    doc["metrics"].append(
        {
            "id": "labeler_utilization",
            "display_name": "Labeler utilization",
            "unit": "1",
            "formula": "station_labeler_utilization",
            "allowed_dimensions": ["variant"],
        }
    )
    (dom / "domain.yaml").write_text(yaml.safe_dump(doc))
    reg = PluginRegistry()
    reg.discover()
    reg.discover_tier0([tmp_path / "domains"])
    settings = Settings(data_dir=tmp_path / "data", examples_dir=EXAMPLES, cpu_workers=1)
    with TestClient(create_app(settings, plugins=reg)) as c:
        ids = {m["id"] for m in c.get("/api/v1/domains/packaging_line/metrics").json()["metrics"]}
        assert "labeler_utilization" in ids
        chart = {
            "template": "bar_ci",
            "query": {"metrics": ["labeler_utilization"], "dimensions": ["variant"]},
        }
        assert (
            c.post(
                "/api/v1/charts/validate", json={"domain": "packaging_line", "chart": chart}
            ).status_code
            == 200
        )
        job = c.post(
            "/api/v1/experiments",
            json={
                "experiment": {"id": "pl", "replications": 2, "run_length": 3600, "warmup": 600},
                "scenario": {"example": "scenarios/packaging_line/baseline.yaml"},
            },
        ).json()["id"]
        _wait(c, job)
        r = c.post("/api/v1/charts/render", json={"job_id": job, "chart": chart})
        assert r.status_code == 200, r.text
        assert r.json()["query"]["rows"][0]["labeler_utilization"] > 0


def test_sql_disabled_by_default(tmp_path: Path) -> None:
    shutil.rmtree(tmp_path / "x", ignore_errors=True)
    with TestClient(create_app(Settings(data_dir=tmp_path / "x", cpu_workers=0))) as c:
        r = c.post("/api/v1/sql", json={"job_id": "job_x", "sql": "SELECT 1"})
        assert r.status_code == 403
