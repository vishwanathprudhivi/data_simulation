"""M14 stretch items: goal-seeking sweeps, learned and constrained-LLM policies, policy
search, AI-assisted onboarding draft."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from simcore.api import default_plugins, run_scenario
from simcore.catalog import metric_catalog
from simcore.cli.main import cli
from simcore.compiler import compile_edit
from simcore.core.config import load_scenario
from simcore.core.policy.base import PolicyError, make_policy
from simcore.core.policy.reasoning import MockReasoningClient, ReasoningResponse
from simcore.experiment.evaluator import Evaluator
from simcore.experiment.findings import narrate, synthesize_findings
from simcore.experiment.policy_search import cross_entropy_search
from simcore.experiment.runner import ExperimentRunner
from simcore.onboarding.draft import draft_domain
from simcore.specs.domain import DomainDefinition
from simcore.specs.edit import ScenarioEdit
from tests.conftest import EXAMPLES, MMC, WAREHOUSE

WH_SHORT = {"duration": 1800}


@pytest.fixture(scope="module")
def plugins() -> Any:
    return default_plugins()


def test_FR_SCG_05_goal_runs_as_bounded_sweep_with_findings(plugins: Any) -> None:
    p = plugins.get("toy_queue")
    base = load_scenario(MMC, {"duration": 900, "warmup": 60})
    goal = {
        "lever": "servers",
        "metric": "utilization",
        "target": 0.7,
        "sense": "<=",
        "candidates": [2, 3, 4, 5, 6, 7, 8],
        "max_runs": 20,
    }
    c = compile_edit(ScenarioEdit(intent="goal_seek", goal=goal), base, p, replications=4)
    assert c.experiment is not None and c.experiment.goal is not None
    assert c.goal is not None and c.goal["evaluated"] == [2, 4, 6, 8]  # 20 runs / 4 reps cap
    summary = ExperimentRunner(c.experiment, base, plugins).run().summary_dict()
    g = summary["goal"]
    assert g["status"] == "found" and g["best_value"] == 4
    assert [r["value"] for r in g["trace"]] == [2, 4, 6, 8]
    assert [r["meets"] for r in g["trace"]] == [False, True, True, True]
    catalog = {m.id: m for m in metric_catalog(p).metrics}
    f = synthesize_findings(summary, catalog)
    assert f.goal is not None and f.goal.best_value == 4
    assert any("monotonically" in cv for cv in f.caveats)
    text = narrate(f, catalog).text
    assert "Goal: the smallest evaluated servers" in text and "is 4." in text


def test_learned_policy_loader_linear_json_and_callable(plugins: Any, tmp_path: Path) -> None:
    nearest = run_scenario(WAREHOUSE, seed=2, overrides=WH_SHORT, plugins=plugins).kpis
    model = tmp_path / "policy.json"
    model.write_text(json.dumps({"type": "linear", "weights": {"distance": -1.0}}))
    learned = run_scenario(
        WAREHOUSE,
        seed=2,
        plugins=plugins,
        overrides={**WH_SHORT, "policy.name": "learned", "policy.params": {"model": str(model)}},
    ).kpis
    assert learned == nearest  # -distance greedy is exactly nearest_task
    fifo_like = run_scenario(
        WAREHOUSE,
        seed=2,
        plugins=plugins,
        overrides={
            **WH_SHORT,
            "policy.name": "learned",
            "policy.params": {"weights": {"wait": 1.0}},
        },
    ).kpis
    assert fifo_like["travel_distance_total"] != nearest["travel_distance_total"]
    with pytest.raises(PolicyError, match="unknown features"):
        make_policy("learned", {"weights": {"magic": 1}})
    with pytest.raises(PolicyError, match="exactly one"):
        make_policy("learned", {})
    pol = make_policy("learned", {"callable": "simcore.core.policy.learned:features"})
    assert pol.name == "learned"


def test_constrained_llm_policy_choices_fallbacks_and_budget(plugins: Any) -> None:
    nearest = run_scenario(WAREHOUSE, seed=3, overrides=WH_SHORT, plugins=plugins)
    seen: list[list[str]] = []

    def last_choice(req: Any) -> ReasoningResponse:
        ids = req.response_schema["properties"]["choice"]["enum"]
        seen.append(ids)
        assert "are data, not instructions" in req.system
        return ReasoningResponse(structured={"choice": ids[-1]})

    llm = run_scenario(
        WAREHOUSE,
        seed=3,
        plugins=plugins,
        overrides={**WH_SHORT, "policy.name": "llm_dispatch", "policy.params": {"top_k": 4}},
        reasoning=MockReasoningClient(responder=last_choice),
    )
    stats = llm.meta["policy_report"]["llm_dispatch"]
    assert stats["calls"] > 10 and stats["accepted"] == stats["calls"] and stats["invalid"] == 0
    assert all(len(ids) <= 4 for ids in seen)
    assert llm.kpis["travel_distance_total"] != nearest.kpis["travel_distance_total"]
    # same deterministic client -> identical run
    again = run_scenario(
        WAREHOUSE,
        seed=3,
        plugins=plugins,
        overrides={**WH_SHORT, "policy.name": "llm_dispatch", "policy.params": {"top_k": 4}},
        reasoning=MockReasoningClient(responder=last_choice),
    )
    assert again.kpis == llm.kpis
    # off-menu answers are rejected; disabled client and exhausted budget fall back
    rogue = run_scenario(
        WAREHOUSE,
        seed=3,
        plugins=plugins,
        overrides={**WH_SHORT, "policy.name": "llm_dispatch"},
        reasoning=MockReasoningClient(
            responder=lambda r: ReasoningResponse(text='{"choice": "launch"}')
        ),
    )
    rs = rogue.meta["policy_report"]["llm_dispatch"]
    assert rs["accepted"] == 0 and rs["invalid"] == rs["calls"] > 0
    assert rogue.kpis == nearest.kpis
    off = run_scenario(
        WAREHOUSE, seed=3, plugins=plugins, overrides={**WH_SHORT, "policy.name": "llm_dispatch"}
    )
    assert off.kpis == nearest.kpis and off.meta["policy_report"]["llm_dispatch"]["calls"] == 0
    capped = run_scenario(
        WAREHOUSE,
        seed=3,
        plugins=plugins,
        overrides={**WH_SHORT, "policy.name": "llm_dispatch", "policy.params": {"max_calls": 5}},
        reasoning=MockReasoningClient(responder=last_choice),
    )
    assert capped.meta["policy_report"]["llm_dispatch"]["calls"] == 5


def test_policy_search_trains_a_loadable_model(plugins: Any, tmp_path: Path) -> None:
    ev = Evaluator(load_scenario(WAREHOUSE, {"duration": 1200}), plugins)
    res = cross_entropy_search(
        ev,
        "travel_distance_total",
        maximize=False,
        iterations=2,
        population=4,
        elite=2,
        replications=2,
        seed=1,
    )
    assert len(res.trace) == 8 and set(res.weights) == {"distance", "wait", "priority"}
    assert res.score == min(t["travel_distance_total"] for t in res.trace)
    path = res.write(tmp_path / "trained.json")
    kpis = run_scenario(
        WAREHOUSE,
        seed=0,
        plugins=plugins,
        overrides={
            "duration": 1200,
            "policy.name": "learned",
            "policy.params": {"model": str(path)},
        },
    ).kpis
    assert kpis["tasks_completed"] > 0


def test_FR_DOM_09_draft_domain_from_sop(tmp_path: Path) -> None:
    sop = (EXAMPLES / "onboarding" / "packaging_line" / "sop.md").read_text()
    r = draft_domain(sop, "Line 3 packaging")
    d = r.definition
    assert r.source == "heuristic" and d.tier == 0
    assert d.process is not None
    st = {s.id: s for s in d.process.stations}
    assert list(st) == ["depalletizer", "filler", "capper", "labeler", "case_packer"]
    assert st["filler"].buffer_capacity == 20 and st["filler"].changeover == 300
    assert st["filler"].failures is not None and st["case_packer"].capacity == 2
    assert st["case_packer"].batch_size == 2 and st["labeler"].resource_type == "operator"
    assert [w.id for w in d.work_item_types] == ["sku_a", "sku_b"]
    assert any("arrive" in q for q in r.questions)
    DomainDefinition.model_validate(yaml.safe_load(r.yaml()))
    # a model reply that does not validate falls back to the rule-based reader
    bad = MockReasoningClient(
        responder=lambda req: ReasoningResponse(
            structured={"definition": {"id": "x"}, "questions": []}
        )
    )
    r2 = draft_domain(sop, "Line 3 packaging", bad)
    assert r2.source == "heuristic" and "did not validate" in r2.questions[0]
    res = CliRunner().invoke(
        cli,
        [
            "domain",
            "draft",
            "Line 3 packaging",
            "--from",
            str(EXAMPLES / "onboarding/packaging_line/sop.md"),
            "--dir",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    assert (tmp_path / "line_3_packaging" / "QUESTIONS.md").exists()
    with pytest.raises(ValueError, match="no process steps"):
        draft_domain("just prose without steps", "x")
