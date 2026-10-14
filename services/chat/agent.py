"""Deterministic chat agent: the LLM-disabled analyst (FR-CHAT-09) and test double.

It routes the planner's message to tools with simple rules, then writes the
answer only from tool results (so its replies are grounded by construction). It
never acts on text found inside tool results: instructions there are data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.chat.heuristic import HeuristicCompilerClient, _Vocab
from simcore.core.policy.reasoning import ReasoningRequest, ReasoningResponse

__all__ = ["HeuristicChatClient"]

_CHANGE = re.compile(
    r"\b(add|remove|drop \d|hire|set|sweep|vary|increase|raise|reduce|decrease|lower|cut|"
    r"smallest|fewest|largest|install|introduce|with \d+|use \w+|switch to)\b"
)
_DIMS = {
    "hour": ("time_bucket", 3600),
    "hours": ("time_bucket", 3600),
    "time": ("time_bucket", 3600),
    "zone": ("zone", None),
    "zones": ("zone", None),
    "agent type": ("agent_type", None),
    "agent": ("agent", None),
    "agents": ("agent", None),
    "location": ("location", None),
    "locations": ("location", None),
    "state": ("state", None),
    "task type": ("task_type", None),
    "dock door": ("dock_door", None),
    "replication": ("replication", None),
}
MAX_CHART_REPAIRS = 2


@dataclass
class _Done:
    name: str
    input: dict[str, Any]
    result: Any
    is_error: bool


def _turn(messages: Sequence[Mapping[str, Any]]) -> tuple[str, list[_Done], list[_Done]]:
    """Last user text, tool calls made since, and all earlier tool calls of the session."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    history: list[_Done] = []
    current: list[_Done] = []
    text = ""
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant" and isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_use":
                    calls[b["id"]] = (b["name"], dict(b.get("input") or {}))
        elif m["role"] == "user":
            if isinstance(content, str):
                if not content.startswith("<grounding_check>"):
                    history.extend(current)
                    current, text = [], content
            else:
                for b in content:
                    if b.get("type") == "tool_result" and b["tool_use_id"] in calls:
                        name, args = calls[b["tool_use_id"]]
                        try:
                            result = json.loads(b.get("content") or "null")
                        except json.JSONDecodeError:
                            result = b.get("content")
                        current.append(_Done(name, args, result, bool(b.get("is_error"))))
    return text, current, history


def _intent(text: str, state: Mapping[str, Any]) -> str:
    t = " ".join(text.lower().split())
    job = bool(state.get("job_id"))
    if state.get("has_draft") and re.search(
        r"^(ok[, ]+)?(yes|yep|sure|go ahead|run it|launch( it)?|submit( it)?|do it|confirm|"
        r"start( it)?|please run)\b",
        t,
    ):
        return "submit"
    if re.search(r"\b(status|progress|is it (done|finished|ready)|still running)\b", t):
        return "status"
    if re.search(r"\bsave\b.*\b(chart|analysis)\b", t):
        return "save_analysis"
    if re.search(r"\bsave\b", t):
        return "save_scenario"
    if job and re.search(r"\bwhy\b", t):
        return "why"
    if job and re.search(r"\b(chart|plot|graph|visuali[sz]e|draw)\b", t):
        return "chart"
    if job and re.search(r"\b(by|per|split by|broken down by)\s+(" + "|".join(_DIMS) + r")\b", t):
        return "drill"
    if re.search(
        r"\b(what can i (change|do)|which levers|what levers|levers|describe the domain|"
        r"which metrics|what metrics|capabilit)", t
    ):  # fmt: skip
        return "describe"
    if re.search(r"\b(list|show|which|what)\b.*\bscenarios\b", t):
        return "list_scenarios"
    if re.search(r"\b(list|show|recent)\b.*\b(experiments|jobs)\b", t):
        return "list_experiments"
    if re.search(
        r"\b(request|ask for|please support|feature request)\b.*\b(support|feature|extension)", t
    ):
        return "extension"
    if _CHANGE.search(t):
        return "compile"
    if job and re.search(
        r"\b(findings|results?|how did|what happened|summary|summari[sz]e|did it (help|work)|"
        r"outcome|compare|difference)\b", t
    ):  # fmt: skip
        return "findings"
    if re.search(r"\b(baseline|current setup|as-is)\b", t):
        return "baseline"
    return "compile"


def _find(done: Sequence[_Done], name: str) -> _Done | None:
    for d in reversed(done):
        if d.name == name and not d.is_error:
            return d
    return None


def _metric_vocab(history: Sequence[_Done]) -> _Vocab | None:
    d = _find(history, "describe_domain")
    if d is None:
        return None
    syn = d.result.get("metric_synonyms", {})
    return _Vocab([{**m, "synonyms": syn.get(m["id"], [])} for m in d.result["metrics"]])


def _metric_phrase(text: str) -> str:
    t = text.lower()
    m = re.search(
        r"\b(?:chart|plot|graph|visuali[sz]e|draw|show)\s+(?:me\s+)?(?:the\s+|a\s+)?"
        r"(?:chart\s+of\s+|plot\s+of\s+)?(?P<p>[a-z][a-z \-]*?)"
        r"(?=\s+(?:by|for|across|in|split|broken)\b|[?.!,]|$)",
        t,
    )
    if m is None:
        m = re.search(r"\b(?:why did|why is|why was)\s+(?:the\s+)?(?P<p>[a-z][a-z \-]*?)\s+"
                      r"(?:drop|fall|rise|go|get|change|increase|decrease)", t)  # fmt: skip
    if m is None:
        m = re.search(r"(?P<p>[a-z][a-z \-]*?)\s+(?:by|per)\s+", t)
    return (m["p"] if m else "").strip()


def _variant_phrase(text: str) -> str | None:
    m = re.search(r"\b(?:in|for|of)\s+(?:the\s+)?((?:variant|alternative|option)\s+\w+|baseline)",
                  text.lower())  # fmt: skip
    return m[1] if m else None


def _fmt(x: Any, digits: int = 3) -> str:
    if isinstance(x, bool) or x is None:
        return str(x)
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        if abs(x) >= 100:
            return f"{x:.1f}"
        return f"{x:.{digits}g}"
    return str(x)


class HeuristicChatClient(HeuristicCompilerClient):
    """Handles ``chat`` requests (and ``compile_scenario`` via the parent class)."""

    model = "heuristic-1"

    def complete(self, request: ReasoningRequest) -> ReasoningResponse:
        task = request.metadata.get("task")
        if task == "compile_scenario":
            return super().complete(request)
        if task != "chat":
            raise ValueError(f"unsupported task {task!r}")
        state = dict(request.metadata.get("state") or {})
        text, done, history = _turn(request.messages)
        step = int(request.metadata.get("step", 0))
        nxt = self._next(text, state, done, history)
        if nxt is None:
            return ReasoningResponse(text=self._compose(text, state, done, history),
                                     model=self.model)  # fmt: skip
        name, args = nxt
        call = {"id": f"tu_{step}_{len(done)}", "name": name, "input": args}
        return ReasoningResponse(tool_calls=[call], model=self.model)

    # ------------------------------------------------------------------ planning

    def _next(
        self,
        text: str,
        state: Mapping[str, Any],
        done: Sequence[_Done],
        history: Sequence[_Done],
    ) -> tuple[str, dict[str, Any]] | None:
        intent = _intent(text, state)
        names = [d.name for d in done]
        domain = state.get("domain")
        if intent == "submit":
            return None if names else ("submit_experiment", {})
        if intent == "status":
            if not names:
                return ("get_job_status", {})
            st = done[-1].result if not done[-1].is_error else {}
            if names == ["get_job_status"] and st.get("status") == "completed":
                return ("get_findings", {})
            return None
        if intent == "findings":
            return None if names else ("get_findings", {})
        if intent in ("chart", "drill", "why"):
            vocab = _metric_vocab([*history, *done])
            if vocab is None and domain and "describe_domain" not in names:
                return ("describe_domain", {"domain": domain})
            if intent == "why":
                if "get_findings" not in names:
                    return ("get_findings", {})
                variant = _variant_phrase(text) or self._worst_variant(done)
                explained = [d.input.get("variant") for d in done if d.name == "explain_run"]
                if variant and variant not in explained:
                    return ("explain_run", {"variant": variant})
                if "baseline" not in explained and not any(
                    d.is_error for d in done if d.name == "explain_run"
                ):
                    return ("explain_run", {"variant": "baseline"})
                return None
            if intent == "chart":
                charts = [d for d in done if d.name == "render_chart"]
                if not charts:
                    return ("render_chart", {"chart": self._chart(text, vocab)})
                last = charts[-1]
                if last.is_error and len(charts) <= MAX_CHART_REPAIRS:
                    fixed = self._repair_chart(last)
                    if fixed is not None:
                        return ("render_chart", {"chart": fixed})
                return None
            if intent == "drill":
                if "query_metrics" not in names:
                    return ("query_metrics", {"query": self._drill(text, vocab, state)})
                return None
        simple = {
            "describe": ("describe_domain", {"domain": domain or "warehouse"}),
            "list_scenarios": ("list_scenarios", {}),
            "list_experiments": ("list_experiments", {}),
            "baseline": ("get_baseline_summary", {}),
            "save_analysis": ("save_analysis", {"name": self._name(text, "analysis")}),
            "save_scenario": ("save_scenario", {"name": self._name(text, "scenario")}),
            "extension": ("request_domain_extension", {"request": text.strip()[:500]}),
            "compile": ("compile_scenario", {"request": text.strip()}),
        }
        if intent in simple:
            return None if names else simple[intent]
        return None  # pragma: no cover

    @staticmethod
    def _name(text: str, default: str) -> str:
        m = re.search(r"\bas\s+['\"]?([\w .-]{1,60}?)['\"]?\s*$", text.strip(), re.IGNORECASE)
        return m[1].strip() if m else default

    @staticmethod
    def _worst_variant(done: Sequence[_Done]) -> str | None:
        f = _find(done, "get_findings")
        if f is None:
            return None
        cmps = f.result["findings"]["comparisons"]
        sig = [c for c in cmps if c["significant"]] or cmps
        return sig[0]["variant"] if sig else None

    @staticmethod
    def _chart(text: str, vocab: _Vocab | None) -> dict[str, Any]:
        phrase = _metric_phrase(text)
        ids = vocab.match(phrase) if (vocab and phrase) else []
        metric = ids[0] if len(ids) == 1 else re.sub(r"[^a-z0-9]+", "_", phrase).strip("_")
        return {
            "template": "bar_ci",
            "title": phrase or metric,
            "query": {"metrics": [metric or "tasks_completed"], "dimensions": ["variant"]},
        }

    @staticmethod
    def _repair_chart(last: _Done) -> dict[str, Any] | None:
        """Apply the repair-ready suggestion of a rejected chart (AC-VIZ-01)."""
        detail = last.result.get("detail") if isinstance(last.result, dict) else None
        issues = detail.get("issues", []) if isinstance(detail, dict) else []
        chart = json.loads(json.dumps(last.input["chart"]))
        changed = False
        for issue in issues:
            sug = re.search(r"did you mean '([^']+)'", issue.get("suggestion") or "")
            m = re.match(r"(?:chart\.)?query\.metrics\[(\d+)\]", issue.get("path", ""))
            if sug and m:
                chart["query"]["metrics"][int(m[1])] = sug[1]
                changed = True
        return chart if changed else None

    @staticmethod
    def _drill(text: str, vocab: _Vocab | None, state: Mapping[str, Any]) -> dict[str, Any]:
        t = text.lower()
        dim, bucket = "variant", None
        for word in sorted(_DIMS, key=len, reverse=True):
            if re.search(rf"\b(by|per|split by|broken down by)\s+{word}\b", t):
                dim, bucket = _DIMS[word]
                break
        phrase = _metric_phrase(text)
        ids = vocab.match(phrase) if (vocab and phrase) else []
        metric = ids[0] if len(ids) == 1 else re.sub(r"[^a-z0-9]+", "_", phrase).strip("_")
        q: dict[str, Any] = {"metrics": [metric or "completions"], "dimensions": [dim]}
        if bucket:
            q["time_bucket"] = bucket
        v = _variant_phrase(text)
        if v:
            variants = list(state.get("variants") or [])
            others = [x for x in variants if x != "baseline"]
            key = v.split()[-1].lower()
            if key == "baseline":
                q["scope"] = {"variants": ["baseline"]}
            elif len(key) == 1 and key.isalpha() and ord(key) - 97 < len(others):
                q["scope"] = {"variants": [others[ord(key) - 97]]}
            elif key in variants:
                q["scope"] = {"variants": [key]}
        return q

    # ------------------------------------------------------------------ composing

    def _compose(
        self,
        text: str,
        state: Mapping[str, Any],
        done: Sequence[_Done],
        history: Sequence[_Done],
    ) -> str:
        self._text, self._history = text, list(history)
        if not done:
            return "I am not sure what to do with that; try describing a change to test."
        last = done[-1]
        if last.is_error:
            err = last.result.get("error") if isinstance(last.result, dict) else str(last.result)
            if last.name == "render_chart":
                return f"I could not render that chart: {err}"
            return f"{last.name} failed: {err}"
        r = last.result
        fn = getattr(self, f"_say_{last.name}", None)
        if fn is None:
            return json.dumps(r, default=str)[:1500]
        out: str = fn(r, done)
        return out

    def _say_compile_scenario(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        if not r.get("ok"):
            issues = "; ".join(r.get("issues") or []) or r.get("error") or "unknown problem"
            return f"I could not turn that into a valid scenario: {issues}"
        card = r["card"]
        lines: list[str] = []
        if card.get("goal") and not card["variants"]:
            g = card["goal"]
            cands = g["candidates"]
            return (
                f"Goal-seek draft: find the {'smallest' if g['minimize_lever'] else 'largest'} "
                f"{g['lever']} (candidates {cands[0]} to {cands[-1]}) with {g['metric']} "
                f"{g['sense']} {g['target']} for the whole confidence interval, capped at "
                f"{g['max_runs']} replications. Assumptions: "
                + "; ".join(card["assumptions"])
                + ". Goal-seek runs through the Python API (simcore.experiment.goalseek) for now."
            )
        if not card["runnable"]:
            msgs = r.get("messages") or []
            if card["ambiguities"] and not card["capability_gaps"]:
                return f"One question before I draft this: {card['ambiguities'][0]}"
            return " ".join(msgs) or "Nothing in that request maps to a supported change."
        lines.append("Draft scenario (nothing has run yet):")
        for v in card["variants"]:
            flag = " [extrapolated: outside the calibrated envelope]" if v["extrapolated"] else ""
            diff = "; ".join(d["text"] for d in v["diff"]) or "no change vs baseline"
            lines.append(f"- {v['id']}: {diff}{flag}")
        if card.get("goal") and card["goal"].get("evaluated"):
            g = card["goal"]
            lines.append(
                f"Goal: the {'smallest' if g['minimize_lever'] else 'largest'} {g['lever']} "
                f"with {g['metric']} {g['sense']} {g['target']} for the whole confidence "
                f"interval, checked at {', '.join(str(v) for v in g['evaluated'])} "
                f"(capped at {g['max_runs']} replications)."
            )
        if card["assumptions"]:
            lines.append("Assumptions: " + "; ".join(card["assumptions"]) + ".")
        for m in r.get("messages") or []:
            lines.append(m)
        conf = r.get("confirmation")
        if conf:
            est = conf["estimate"]
            lines.append(
                f"Estimate: {est['runs']} runs, about {_fmt(est['minutes'])} min in "
                f"{conf['fidelity']} mode."
                + (" This needs your confirmation." if conf["needs_confirmation"] else "")
            )
            lines.append("Say 'run it' to launch.")
        return "\n".join(lines)

    def _say_submit_experiment(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        if r["status"] == "pending_confirmation":
            return (
                f"This run is {r['reason']}, so I have not launched it. Confirm action "
                f"{r['action_id']} in the UI to start it (estimate: {r['estimate']['runs']} runs)."
            )
        return f"Launched job {r['job_id']} ({r['estimate']['runs']} runs). Ask me for the status."

    def _say_get_job_status(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        p = r.get("progress") or {}
        prog = f" ({p.get('completed')}/{p.get('planned')} replications)" if p else ""
        err = f": {r['error']}" if r.get("error") else ""
        return f"Job {r['id']} is {r['status']}{prog}{err}."

    def _say_get_findings(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        f = r["findings"]
        text = str(r["narrative"]["text"])
        tips = []
        for d in f.get("drivers", [])[:1]:
            tips.append(
                f"Suggested follow-up: {d['metric']} moved the most alongside the headline; "
                f"ask me to break {d['metric']} down by location or hour."
            )
        if f.get("comparisons") and not any(c["significant"] for c in f["comparisons"]):
            tips.append("Suggested follow-up: more replications would narrow the intervals.")
        return "\n".join([text, *tips])

    def _say_describe_domain(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        levers = ", ".join(f"{lv['display_name']} ({lv['id']})" for lv in r["levers"])
        metrics = ", ".join(m["display_name"] for m in r["metrics"])
        env = "; ".join(
            f"{b['lever']} in [{_fmt(b['min'])}, {_fmt(b['max'])}]" for b in r["envelope"]
        )
        out = f"In {r['domain']} you can change: {levers}.\nMetrics: {metrics}."
        if env:
            out += f"\nCalibrated envelope: {env}."
        return out

    def _say_list_scenarios(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        rows = r["scenarios"]
        if not rows:
            return "There are no saved scenarios in this workspace yet."
        return "Scenarios: " + ", ".join(
            f"{s['id']}{' (pinned baseline)' if s.get('pinned') else ''}" for s in rows
        )

    def _say_list_experiments(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        rows = r["jobs"]
        if not rows:
            return "No experiments have run in this workspace yet."
        return "Recent jobs: " + ", ".join(f"{j['id']} ({j['status']})" for j in rows)

    def _say_get_baseline_summary(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        vals = ", ".join(f"{k} = {_fmt(v)}" for k, v in sorted(r["lever_values"].items()))
        return f"Baseline {r['scenario']} ({r['domain']}): {vals}."

    def _say_save_scenario(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        return f"Saved scenario {r['id']} as version {r['version']}."

    def _say_save_analysis(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        return f"Saved analysis '{r['name']}' ({r['id']})."

    def _say_request_domain_extension(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        return f"Logged the request for the domain owners ({r['gap_id']})."

    def _say_render_chart(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        retries = sum(1 for d in done if d.name == "render_chart") - 1
        note = (
            f" (I corrected the metric name after {retries} rejected attempt"
            f"{'s' if retries != 1 else ''})"
            if retries
            else ""
        )
        rows = r["query"]["rows"]
        metric = r["query"]["provenance"]["query"]["metrics"][0]
        parts = []
        for row in rows[:6]:
            lo, hi = row.get(f"{metric}.ci_low"), row.get(f"{metric}.ci_high")
            ci = f" (CI {_fmt(lo)} to {_fmt(hi)})" if lo is not None and hi is not None else ""
            parts.append(f"{row.get('variant', '')}: {_fmt(row.get(metric))}{ci}")
        return f"Chart {r['ref_id']} of {metric} by variant{note}: " + "; ".join(parts) + "."

    def _say_query_metrics(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        q = r["provenance"]["query"]
        metric, dims = q["metrics"][0], q.get("dimensions") or []
        dim = dims[0] if dims else None
        rows = r["rows"]
        if not rows:
            return f"No data for {metric}."
        top = sorted(rows, key=lambda x: -(x.get(metric) or 0))[:8]
        parts = [
            f"{row.get(dim, '')}"
            + (f" ({row['variant']})" if dim != "variant" and "variant" in row else "")
            + f": {_fmt(row.get(metric))}"
            for row in top
        ]
        conf = q.get("confidence", 0.95)
        return (
            f"{metric} by {dim} (query {r['ref_id']}, means over replications, "
            f"{_fmt(conf)} confidence): " + "; ".join(parts) + "."
        )

    def _say_explain_run(self, r: Mapping[str, Any], done: Sequence[_Done]) -> str:
        f = _find(done, "get_findings")
        lines = []
        if f is not None:
            cmps = f.result["findings"]["comparisons"]
            first = next((d for d in done if d.name == "explain_run" and not d.is_error), None)
            target = first.result["variant"] if first is not None else r["variant"]
            mine = [c for c in cmps if c["variant"] == target]
            vocab = _metric_vocab([*getattr(self, "_history", []), *done])
            phrase = _metric_phrase(getattr(self, "_text", ""))
            asked = vocab.match(phrase) if (vocab and phrase) else []
            if asked:
                mine = [c for c in mine if c["metric"] in asked] or mine
            for c in (mine or cmps)[:2]:
                d = c["paired_diff"]
                verdict = "significant" if c["significant"] else "not significant"
                lines.append(
                    f"{c['variant']}: {c['metric']} changed by {_fmt(d['mean'])} "
                    f"(CI {_fmt(d['ci'][0])} to {_fmt(d['ci'][1])}, {verdict})."
                )
        for d in done:
            if d.name != "explain_run" or d.is_error:
                continue
            res = d.result
            st = res["state_time"]["rows"]
            states = sorted(st, key=lambda x: -(x.get("state_time") or 0))[:4]
            st_text = ", ".join(f"{x['state']} {_fmt(x['state_time'])} s" for x in states)
            bl = res["top_blocking_locations"]["rows"][:3]
            bl_text = ", ".join(f"{x['location']} {_fmt(x['blocking_time'])} s" for x in bl)
            lines.append(
                f"{res['variant']}: agent time by state: {st_text or 'n/a'}; "
                f"most blocking at: {bl_text or 'none'}."
            )
        return "\n".join(lines)
