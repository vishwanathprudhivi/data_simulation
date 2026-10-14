"""Golden-set evaluation harness (FR-CHAT-10, AC-CHAT-01/02).

Each case is a natural-language request with the expected outcome:

* ``expect.diff``: final lever values of the single what-if (``{lever: value}``);
* ``expect.variants``: variant ids of a sweep / named variants;
* ``expect.goal``: subset of the goal (lever, metric, target);
* ``expect.unsupported``: requests that must become capability gaps;
* ``expect.intent``: e.g. ``clarify``; ``expect.error``: compile must fail;
* ``expect.query``: subset of the QuerySpec of the first query/chart tool call.

The harness runs with any :class:`ReasoningClient`: the deterministic heuristic
client in CI, a :class:`ReplayReasoningClient` over recordings, or a live model
on demand (``SIMCORE_REASONING=anthropic``, marked ``live_llm``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from services.chat.agent import HeuristicChatClient
from services.chat.compiler import ScenarioCompiler
from services.chat.grounding import check_grounding
from services.chat.session import CHAT_SYSTEM_PROMPT
from services.chat.tools import tool_schemas
from simcore.catalog import capability_manifest
from simcore.core.config import ScenarioBundle
from simcore.core.plugins import DomainPlugin
from simcore.core.policy.reasoning import ReasoningClient, ReasoningRequest

__all__ = ["CaseResult", "GoldenReport", "load_cases", "run_golden"]


@dataclass
class CaseResult:
    id: str
    passed: bool
    problems: list[str] = field(default_factory=list)
    grounded: bool | None = None
    reply: str = ""


@dataclass
class GoldenReport:
    results: list[CaseResult]

    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.results) / max(len(self.results), 1)

    @property
    def grounded_rate(self) -> float:
        checked = [r for r in self.results if r.grounded is not None]
        return sum(bool(r.grounded) for r in checked) / max(len(checked), 1)

    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        lines = [
            f"{sum(r.passed for r in self.results)}/{len(self.results)} passed "
            f"({self.pass_rate:.0%}); grounded replies {self.grounded_rate:.0%}"
        ]
        lines += [f"  FAIL {r.id}: {'; '.join(r.problems)}" for r in self.failures()]
        return "\n".join(lines)


def load_cases(path: Path) -> list[dict[str, Any]]:
    doc = yaml.safe_load(path.read_text())
    return list(doc["cases"])


def _subset(expected: Any, actual: Any, path: str, problems: list[str]) -> None:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            problems.append(f"{path}: expected an object, got {actual!r}")
            return
        for k, v in expected.items():
            _subset(v, actual.get(k), f"{path}.{k}", problems)
    elif isinstance(expected, float) or isinstance(actual, float):
        try:
            if abs(float(expected) - float(actual)) > 1e-6:
                problems.append(f"{path}: expected {expected!r}, got {actual!r}")
        except (TypeError, ValueError):
            problems.append(f"{path}: expected {expected!r}, got {actual!r}")
    elif expected != actual:
        problems.append(f"{path}: expected {expected!r}, got {actual!r}")


def _compile_case(
    case: Mapping[str, Any],
    client: ReasoningClient,
    base: ScenarioBundle,
    plugin: DomainPlugin,
) -> CaseResult:
    exp = case["expect"]
    res = ScenarioCompiler(client).compile(case["text"], base, plugin, log_gaps=False)
    problems: list[str] = []
    if exp.get("error"):
        if res.ok:
            problems.append("expected the compile to fail")
        return CaseResult(case["id"], not problems, problems)
    if not res.ok or res.compiled is None:
        return CaseResult(case["id"], False, [f"compile failed: {res.issues or res.error}"])
    c = res.compiled
    if "intent" in exp and c.edit.intent != exp["intent"]:
        problems.append(f"intent: expected {exp['intent']}, got {c.edit.intent}")
    if "diff" in exp:
        if len(c.diffs) != 1:
            problems.append(f"expected one what-if, got variants {sorted(c.diffs)}")
        else:
            got = {d.lever: d.value for d in next(iter(c.diffs.values()))}
            _subset(exp["diff"], got, "diff", problems)
            extra = sorted(set(got) - set(exp["diff"]))
            if extra:
                problems.append(f"diff: unexpected changes {extra}")
    if "variants" in exp:
        got_ids = [v.id for v in c.experiment.variants] if c.experiment else []
        if got_ids != list(exp["variants"]):
            problems.append(f"variants: expected {exp['variants']}, got {got_ids}")
    if "goal" in exp:
        _subset(exp["goal"], c.goal or {}, "goal", problems)
    if "unsupported" in exp:
        got_gaps = sorted(g.request for g in c.gaps)
        if got_gaps != sorted(exp["unsupported"]):
            problems.append(f"unsupported: expected {exp['unsupported']}, got {got_gaps}")
    if "extrapolated" in exp and bool(c.extrapolated) != bool(exp["extrapolated"]):
        problems.append(f"extrapolated: expected {exp['extrapolated']}")
    reps = c.experiment.replications if c.experiment is not None else None
    if "replications" in exp and reps is not None and reps != exp["replications"]:
        problems.append(f"replications: got {reps}")
    if "metrics" in exp and list(c.edit.metrics) != list(exp["metrics"]):
        problems.append(f"metrics: expected {exp['metrics']}, got {c.edit.metrics}")
    # the chat reply for this draft must be grounded in the compile result (AC-CHAT-02)
    payload = {"domain": plugin.name, **res.to_dict()}
    reply = HeuristicChatClient()._say_compile_scenario(payload, [])
    grounding = check_grounding(reply, [payload, case["text"]])
    if not grounding.ok:
        problems.append(f"ungrounded numbers in reply: {grounding.unreferenced}")
    return CaseResult(case["id"], not problems, problems, grounding.ok, reply)


def _query_case(
    case: Mapping[str, Any],
    client: ReasoningClient,
    plugin: DomainPlugin,
) -> CaseResult:
    """The first query/chart tool call the client makes for the request."""
    m = capability_manifest(plugin)
    describe = {
        "domain": m.domain,
        "levers": [lv.model_dump(mode="json") for lv in m.levers],
        "metrics": [mt.model_dump(mode="json") for mt in m.metrics],
        "metric_synonyms": {mt.id: mt.synonyms for mt in m.metrics},
        "dimensions": [d.id for d in m.dimensions],
        "envelope": [b.model_dump(mode="json") for b in m.envelope],
        "policies": m.policies,
    }
    variants = list(case.get("variants", ["baseline", "what_if"]))
    state = {"job_id": "job_golden", "variants": variants, "domain": m.domain}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Describe the {m.domain} domain."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "g0",
                    "name": "describe_domain",
                    "input": {"domain": m.domain},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "g0", "content": json.dumps(describe)}
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        {"role": "user", "content": case["text"]},
    ]
    problems: list[str] = []
    for step in range(4):
        r = client.complete(
            ReasoningRequest(
                system=CHAT_SYSTEM_PROMPT,
                messages=messages,
                tools=tool_schemas(),
                metadata={"task": "chat", "step": step, "state": state},
            )
        )
        calls = [c for c in r.tool_calls if c["name"] in ("query_metrics", "render_chart")]
        if calls:
            args = calls[0]["input"]
            query = args.get("query") or (args.get("chart") or {}).get("query") or {}
            _subset(case["expect"]["query"], query, "query", problems)
            if "tool" in case["expect"] and calls[0]["name"] != case["expect"]["tool"]:
                problems.append(f"tool: expected {case['expect']['tool']}, got {calls[0]['name']}")
            return CaseResult(case["id"], not problems, problems)
        if not r.tool_calls:
            break
        # answer other tool calls with an empty result and continue
        messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": c["id"], "name": c["name"],
                         "input": c["input"]} for c in r.tool_calls],
        })  # fmt: skip
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": c["id"], "content": "{}"}
                        for c in r.tool_calls],
        })  # fmt: skip
    return CaseResult(case["id"], False, ["no query_metrics/render_chart call was made"])


def run_golden(
    cases: Sequence[Mapping[str, Any]],
    client: ReasoningClient,
    bases: Mapping[str, ScenarioBundle],
    plugins: Mapping[str, DomainPlugin],
) -> GoldenReport:
    results = []
    for case in cases:
        domain = case.get("domain", "warehouse")
        try:
            if "query" in case["expect"]:
                results.append(_query_case(case, client, plugins[domain]))
            else:
                results.append(_compile_case(case, client, bases[domain], plugins[domain]))
        except Exception as exc:  # a crash is a failed case, not a crashed harness
            results.append(CaseResult(case["id"], False, [f"{type(exc).__name__}: {exc}"]))
    return GoldenReport(results)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m services.chat.golden [--client SPEC] [--cases FILE]``.

    ``--client record:tests/golden/recordings`` runs against Claude and stores the
    responses so CI can replay them (``--client replay:tests/golden/recordings``).
    """
    import argparse
    import sys

    from services.chat.llm import make_client
    from simcore.api import default_plugins
    from simcore.core.config import load_scenario

    root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--client", default="heuristic")
    ap.add_argument("--cases", type=Path, default=root / "tests/golden/chat_golden.yaml")
    ns = ap.parse_args(argv)
    plugins = default_plugins()
    examples = root / "examples" / "scenarios"
    report = run_golden(
        load_cases(ns.cases),
        make_client(ns.client),
        {
            "warehouse": load_scenario(examples / "warehouse" / "baseline.yaml"),
            "toy_queue": load_scenario(examples / "toy_queue" / "mmc_baseline.yaml"),
        },
        {"warehouse": plugins.get("warehouse"), "toy_queue": plugins.get("toy_queue")},
    )
    sys.stdout.write(report.summary() + "\n")
    return 0 if report.pass_rate >= 0.9 and report.grounded_rate == 1.0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
