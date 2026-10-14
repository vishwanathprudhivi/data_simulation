"""Service layer (M7): FR-API-01..09, FR-SCG-07/08, AC-API-01."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from services.api.admin import create_user
from services.api.app import create_app
from services.platform.settings import Settings

from tests.conftest import EXAMPLES

SMALL = {
    "id": "api_exp",
    "replications": 3,
    "run_length": 300,
    "warmup": 30,
    "variants": [{"id": "four", "overrides": {"fleet.server.count": 4}}],
    "kpis": ["wait_time_mean", "utilization"],
}
MMC_REF = {"example": "scenarios/toy_queue/mmc_baseline.yaml"}


def _settings(tmp: Path, **kw: Any) -> Settings:
    return Settings(data_dir=tmp / "data", examples_dir=EXAMPLES, cpu_workers=2, **kw)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(tmp_path))) as c:
        yield c


def _sse(client: TestClient, job_id: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    with client.stream("GET", f"/api/v1/jobs/{job_id}/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        kind = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                events.append((kind or "", json.loads(line[6:])))
                if kind == "done":
                    break
    return events


def _wait(client: TestClient, job_id: str, timeout: float = 60) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_AC_API_01_submit_stream_results_and_cache(client: TestClient) -> None:
    body = {"experiment": SMALL, "scenario": MMC_REF}
    r = client.post("/api/v1/experiments", json=body)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] in ("queued", "running", "completed") and not job["cached"]
    events = _sse(client, job["id"])
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done" and events[-1][1]["status"] == "completed"
    progress = [e["progress_completed"] for k, e in events if k == "progress"]
    assert progress == sorted(progress) and max(progress) == 6
    summary = client.get(f"/api/v1/jobs/{job['id']}/summary").json()
    assert summary["metrics"] == ["wait_time_mean", "utilization"]
    assert {c["variant"] for c in summary["comparisons"]} == {"four"}
    assert "<html" in client.get(f"/api/v1/jobs/{job['id']}/report").text.lower()
    runs = client.get(f"/api/v1/jobs/{job['id']}/runs").json()
    assert len(runs) == 6 and all(r["status"] == "ok" for r in runs)
    # resubmitting the identical spec is served from cache without re-running
    again = client.post("/api/v1/experiments", json=body)
    assert again.status_code == 200 and again.json()["cached"]
    assert again.json()["status"] == "completed"
    assert again.json()["result_prefix"] == _wait(client, job["id"])["result_prefix"]
    est = client.post("/api/v1/experiments/estimate", json=body).json()
    assert est["cached"] is True
    actions = [
        a["outcome"]
        for a in client.get("/api/v1/audit").json()
        if a["action"] == "experiment.submit"
    ]
    assert actions == ["cache_hit", "queued"]  # newest first


def test_FR_API_01_domains_schemas_and_catalogs(client: TestClient) -> None:
    domains = {d["id"]: d for d in client.get("/api/v1/domains").json()}
    assert {"warehouse", "toy_queue", "packaging_line"} <= set(domains)
    wh = client.get("/api/v1/domains/warehouse").json()
    assert "jacks" in {lv["id"] for lv in wh["levers"]}
    assert "dock_on_time_rate" in {
        m["id"] for m in client.get("/api/v1/domains/warehouse/metrics").json()["metrics"]
    }
    assert client.get("/api/v1/domains/brewery").status_code == 404
    assert "scenario" in client.get("/api/v1/schemas").json()
    assert client.get("/api/v1/schemas/chart_spec").json()["$id"].startswith("urn:simcore")
    assert client.get("/api/v1/openapi.json").status_code == 200


def test_FR_SCG_08_scenario_library_versions_levers_and_pins(client: TestClient) -> None:
    lib = {s["id"]: s for s in client.get("/api/v1/scenarios").json()}
    assert "wh_baseline" in lib and lib["wh_baseline"]["pinned"] == 1
    assert "example" in lib["wh_baseline"]["tags"]
    r = client.post(
        "/api/v1/scenarios",
        json={
            "scenario": {"scenario_id": "wh_baseline", "levers": {"jacks": 40}},
            "id": "wh_40_jacks",
            "tags": ["what-if"],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1 and r.json()["outside_envelope"]["jacks"]["max"] == 30
    r2 = client.post(
        "/api/v1/scenarios",
        json={"scenario": {"scenario_id": "wh_40_jacks", "levers": {"jacks": 25}}},
    )
    assert r2.json()["version"] == 2
    got = client.get("/api/v1/scenarios/wh_40_jacks").json()
    assert got["version"] == 2 and got["parent_id"] == "wh_40_jacks"
    assert got["bundle"]["scenario"]["fleet"][0]["count"] == 25
    fac = client.get("/api/v1/scenarios/wh_baseline/facility").json()
    assert len(fac["nodes"]) == 88 and len(fac["edges"]) == 92


def test_FR_ENG_08_validation_errors_are_structured(client: TestClient) -> None:
    r = client.post(
        "/api/v1/scenarios/validate",
        json={"scenario_id": "wh_baseline", "levers": {"jacks": -3, "wings": 2}},
    )
    assert r.status_code == 422
    issues = r.json()["issues"]
    assert {i["location"] for i in issues} == {"levers.jacks", "levers.wings"}
    assert all(i["hint"] for i in issues)
    bad = client.post("/api/v1/scenarios/validate", json={"scenario_id": "nope"})
    assert bad.status_code == 404


def test_FR_SCG_07_confirmation_gate(client: TestClient) -> None:
    big = {
        **SMALL,
        "id": "big",
        "variants": [],
        "factors": [
            {"name": "servers", "path": "fleet.server.count", "levels": list(range(3, 15))}
        ],
    }
    body = {"experiment": big, "scenario": MMC_REF}
    r = client.post("/api/v1/experiments", json=body)
    assert r.status_code == 409 and "needs confirmation" in r.json()["message"]
    est = client.post("/api/v1/experiments/estimate", json=body).json()
    assert est["needs_confirmation"] and est["estimate"]["variants"] == 13  # + baseline
    ok = client.post(
        "/api/v1/experiments", json={**body, "confirmation_token": est["confirmation_token"]}
    )
    assert ok.status_code == 201
    client.post(f"/api/v1/jobs/{ok.json()['id']}/cancel")


def test_FR_API_03_quota_and_cancellation(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, max_jobs_per_workspace=1))) as c:
        long = {**SMALL, "id": "long", "replications": 10, "run_length": 2200}
        j = c.post("/api/v1/experiments", json={"experiment": long, "scenario": MMC_REF}).json()
        other = {**SMALL, "id": "other", "master_seed": 99}
        r = c.post("/api/v1/experiments", json={"experiment": other, "scenario": MMC_REF})
        assert r.status_code == 429 and "active jobs" in r.json()["message"]
        cancelled = c.post(f"/api/v1/jobs/{j['id']}/cancel").json()
        final = _wait(c, j["id"])
        assert final["status"] == "cancelled" or cancelled["status"] == "cancelled"
        assert (
            c.post(
                "/api/v1/experiments", json={"experiment": other, "scenario": MMC_REF}
            ).status_code
            == 201
        )


def test_FR_API_02_idempotent_while_running(client: TestClient) -> None:
    body = {
        "experiment": {**SMALL, "id": "idem", "replications": 6, "run_length": 2200},
        "scenario": MMC_REF,
    }
    a = client.post("/api/v1/experiments", json=body).json()
    b = client.post("/api/v1/experiments", json=body).json()
    assert a["id"] == b["id"]
    _wait(client, a["id"])


def test_FR_API_07_auth_and_workspace_isolation(tmp_path: Path) -> None:
    settings = _settings(tmp_path, auth_enabled=True)
    app = create_app(settings)
    with TestClient(app) as c:
        store = app.state.container.store
        create_user(store, "alice", "s3cret", ["team_a"])
        create_user(store, "bob", "hunter2", ["team_b"])
        assert c.get("/api/v1/domains").status_code == 401
        assert (
            c.post("/api/v1/auth/login", json={"user": "alice", "password": "x"}).status_code == 401
        )
        tok = c.post("/api/v1/auth/login", json={"user": "alice", "password": "s3cret"}).json()[
            "token"
        ]
        alice = {"Authorization": f"Bearer {tok}"}
        bob = {"Authorization": "Basic " + __import__("base64").b64encode(b"bob:hunter2").decode()}
        job = c.post(
            "/api/v1/experiments", headers=alice, json={"experiment": SMALL, "scenario": MMC_REF}
        ).json()
        assert job["workspace"] == "team_a"
        assert c.get(f"/api/v1/jobs/{job['id']}", headers=bob).status_code == 404
        assert c.get("/api/v1/jobs", headers={**alice, "X-Workspace": "team_b"}).status_code == 403
        assert c.get("/api/v1/jobs", headers=bob).json() == []
        end = time.time() + 60
        while c.get(f"/api/v1/jobs/{job['id']}", headers=alice).json()["status"] not in (
            "completed",
            "failed",
        ):
            assert time.time() < end
            time.sleep(0.05)


def test_run_events_endpoint_pages_event_log(client: TestClient) -> None:
    job = client.post("/api/v1/experiments", json={"experiment": SMALL, "scenario": MMC_REF}).json()
    _wait(client, job["id"])
    page = client.get(
        f"/api/v1/jobs/{job['id']}/variants/baseline/replications/0/events",
        params={"limit": 5, "types": "task.completed"},
    ).json()
    assert page["total"] > 100 and len(page["rows"]) == 5
    assert all(r["type"] == "task.completed" for r in page["rows"])
    files = client.get(f"/api/v1/jobs/{job['id']}/artifacts").json()
    assert "summary.json" in files and "baseline/rep_000/events.parquet" in files
    assert client.get(f"/api/v1/jobs/{job['id']}/artifacts/../../etc/passwd").status_code == 404


def test_inline_bundles_cannot_read_server_files(client: TestClient) -> None:
    bundle = client.get("/api/v1/scenarios/wh_baseline").json()["bundle"]
    bundle["scenario"]["demand"][0]["path"] = "/etc/passwd"
    r = client.post("/api/v1/scenarios/validate", json={"bundle": bundle})
    assert r.status_code == 422 and "outside the server" in r.text


def test_FR_API_06_estimator_uses_history(client: TestClient) -> None:
    body = {"experiment": SMALL, "scenario": MMC_REF}
    before = client.post("/api/v1/experiments/estimate", json=body).json()["estimate"]
    assert before["basis"] == "default" and before["runs"] == 6
    _wait(client, client.post("/api/v1/experiments", json=body).json()["id"])
    after = client.post(
        "/api/v1/experiments/estimate", json={**body, "experiment": {**SMALL, "master_seed": 5}}
    ).json()
    assert after["estimate"]["basis"] == "history"


def test_analyses_round_trip(client: TestClient) -> None:
    aid = client.post(
        "/api/v1/analyses", json={"name": "canvas", "payload": {"charts": []}}
    ).json()["id"]
    assert client.get(f"/api/v1/analyses/{aid}").json()["payload"] == {"charts": []}
    assert [a["id"] for a in client.get("/api/v1/analyses").json()] == [aid]


@pytest.mark.redis
def test_FR_API_03_redis_broker_with_standalone_worker(tmp_path: Path) -> None:
    from services.workers.__main__ import serve

    url = os.environ["SIMCORE_REDIS_URL"]
    settings = _settings(tmp_path, broker="redis", redis_url=url)
    with TestClient(create_app(settings)) as c:
        job = c.post(
            "/api/v1/experiments",
            json={"experiment": {**SMALL, "id": "redis_exp"}, "scenario": MMC_REF},
        ).json()
        assert job["status"] == "queued"
        worker = threading.Thread(target=serve, args=("cpu", settings), kwargs={"once": True})
        worker.start()
        worker.join(timeout=120)
        assert _wait(c, job["id"])["status"] == "completed"
