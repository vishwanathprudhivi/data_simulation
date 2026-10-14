"""API endpoints behind the frontend (M11): FR-UI-06 replay, FR-UI-09 exports, SPA serving."""

from __future__ import annotations

import bisect
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from services.api.app import create_app
from services.platform.settings import Settings

from tests.conftest import EXAMPLES


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[TestClient, str]]:
    tmp = tmp_path_factory.mktemp("ui")
    spa = tmp / "spa"
    spa.mkdir()
    (spa / "index.html").write_text("<!doctype html><title>simcore</title>")
    settings = Settings(data_dir=tmp / "data", examples_dir=EXAMPLES, frontend_dir=spa)
    with TestClient(create_app(settings)) as c:
        job = c.post(
            "/api/v1/experiments",
            json={
                "experiment": {"id": "ui", "replications": 1, "run_length": 600},
                "scenario": {"example": "scenarios/warehouse/baseline.yaml"},
            },
        ).json()["id"]
        end = time.time() + 60
        while c.get(f"/api/v1/jobs/{job}").json()["status"] != "completed":
            assert time.time() < end
            time.sleep(0.05)
        yield c, job


def _position(agent: dict[str, Any], t: float) -> list[float] | None:
    """Independent re-implementation of the viewer's interpolation (frontend/src/api.ts)."""
    segs = agent["segments"]
    if not segs:
        return agent["spawn"]
    i = bisect.bisect_left([s[1] for s in segs], t)
    if i >= len(segs):
        return [segs[-1][4], segs[-1][5]]
    t0, t1, x0, y0, x1, y1 = segs[i]
    if t < t0:
        return [segs[i - 1][4], segs[i - 1][5]] if i else [x0, y0]
    f = 1.0 if t1 <= t0 else min(max((t - t0) / (t1 - t0), 0.0), 1.0)
    return [x0 + (x1 - x0) * f, y0 + (y1 - y0) * f]


def test_FR_UI_06_replay_trajectories_match_server_snapshots(env: Any) -> None:
    c, job = env
    base = f"/api/v1/jobs/{job}/variants/baseline/replications/0"
    data = c.get(f"{base}/replay").json()
    assert data["duration"] == 600 and data["nodes"] and data["edges"] and data["zones"]
    assert sum(len(a["segments"]) for a in data["agents"].values()) > 50
    for t in (0.0, 12.5, 100.0, 299.9, 599.0):
        snap = c.get(f"{base}/snapshot", params={"t": t}).json()["agents"]
        assert set(snap) == set(data["agents"])
        for agent, s in snap.items():
            mine = _position(data["agents"][agent], t)
            if s["position"] is None:
                assert mine is None
            else:
                assert mine == pytest.approx(s["position"][:2], abs=1e-9)
    assert c.get(f"/api/v1/jobs/{job}/variants/nope/replications/0/replay").status_code == 404


def test_FR_UI_09_scenario_yaml_export_applies_levers(env: Any) -> None:
    c, _ = env
    r = c.post(
        "/api/v1/scenarios/export",
        json={"example": "scenarios/warehouse/baseline.yaml", "levers": {"jacks": 15}},
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-yaml")
    doc = yaml.safe_load(r.text)
    fleet = {f["agent_type"]: f["count"] for f in doc["fleet"]}
    assert fleet["pallet_jack_operator"] == 15


def test_spa_is_served_next_to_the_api(env: Any) -> None:
    c, _ = env
    assert "<title>simcore</title>" in c.get("/").text
    assert c.get("/api/v1/health").json()["status"] == "ok"


def test_chat_turn_returns_tool_outputs_for_cards(env: Any) -> None:
    c, _ = env
    sid = c.post(
        "/api/v1/chat/sessions", json={"scenario": {"example": "scenarios/warehouse/baseline.yaml"}}
    ).json()["session_id"]
    turn = c.post(f"/api/v1/chat/sessions/{sid}/messages", json={"text": "add 2 jacks"}).json()
    (call,) = turn["tool_calls"]
    assert call["name"] == "compile_scenario" and call["output"]["card"]["variants"]


def test_generated_frontend_types_cover_every_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    schemas = {p.name.replace(".schema.json", "") for p in (root / "schemas").glob("*.schema.json")}
    generated = {p.stem for p in (root / "frontend" / "src" / "generated").glob("*.ts")} - {"index"}
    assert schemas == generated, "run `make schemas` then `cd frontend && npm run gen:types`"
