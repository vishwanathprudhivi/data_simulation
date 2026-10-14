"""Deterministic, rule-based compile client (LLM-disabled path, FR-CHAT-09).

Understands a small, documented grammar over the domain's Lever and Metric
Catalogs (ids, display names and ``synonyms``):

* ``add 5 jacks`` / ``remove 2 operators`` / ``with 20 jacks`` / ``set jacks to 20``
* ``increase shift length by 25%``
* ``use fifo dispatch`` (any enum choice named in the text)
* ``sweep jacks from 10 to 30 by 5``
* ``smallest fleet that meets a 95% on-time rate``
* ``... with 20 replications``

Anything else becomes an ambiguity or a capability gap; it never guesses a
lever silently. The request text is treated as data: it is only matched against
catalog words, never interpreted as instructions.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from simcore.core.policy.reasoning import ReasoningClient, ReasoningRequest, ReasoningResponse

__all__ = ["HeuristicCompilerClient", "compile_text"]

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a couple of": 2, "a few": 3,
}  # fmt: skip
_NUM = r"(\d+(?:\.\d+)?|a couple of|a few|an|a|one|two|three|four|five|six|seven|eight|nine|ten)"
_STOP = {"the", "of", "number", "more", "extra", "additional", "new", "our", "my", "total"}
_FILLER = r"(?:more |extra |additional |new )?"


def _num(token: str) -> float:
    token = token.strip().lower()
    if token in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[token])
    return float(token)


def _clean(n: float) -> int | float:
    return int(n) if float(n).is_integer() else n


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9%]+", text.lower().replace("_", " ")) if w not in _STOP]


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


class _Vocab:
    """Phrase -> catalog id matching over ids, display names and synonyms."""

    def __init__(self, items: Sequence[Mapping[str, Any]]) -> None:
        self.items = {str(i["id"]): i for i in items}
        self.phrases: list[tuple[str, str]] = []
        for i in items:
            names = [str(i["id"]).replace("_", " "), str(i.get("display_name", ""))]
            names += [str(s) for s in i.get("synonyms", [])]
            for n in names:
                if n.strip():
                    self.phrases.append((" ".join(_stem(w) for w in _words(n)), str(i["id"])))

    def match(self, phrase: str) -> list[str]:
        """Ids whose name/synonym best matches ``phrase`` (ties -> several ids)."""
        words = [_stem(w) for w in _words(phrase)]
        if not words:
            return []
        text = " ".join(words)
        best: dict[str, float] = {}
        for p, i in self.phrases:
            if not p:
                continue
            if p == text:
                score = 3.0
            elif f" {p} " in f" {text} ":
                score = 2.0 + len(p) / 100
            else:
                pw = set(p.split())
                overlap = len(pw & set(words))
                score = overlap / max(len(pw), 1) if overlap else 0.0
            best[i] = max(best.get(i, 0.0), score)
        if not best:
            return []
        top = max(best.values())
        if top < 0.5:
            return []
        return sorted(i for i, s in best.items() if s == top)

    def nearest(self, phrase: str, numeric_only: bool = False) -> str | None:
        """Closest id by string similarity (for capability-gap alternatives)."""
        cands = [
            (p, i)
            for p, i in self.phrases
            if not numeric_only or self.items[i].get("type") in ("int", "float", "duration")
        ]
        if not cands:
            return None
        text = " ".join(_stem(w) for w in _words(phrase))
        scored = sorted(
            ((difflib.SequenceMatcher(None, text, p).ratio(), i) for p, i in cands),
            key=lambda x: (-x[0], x[1]),
        )
        return scored[0][1]


def compile_text(
    text: str, levers: Sequence[Mapping[str, Any]], metrics: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Map ``text`` to a ``ScenarioEdit`` dict using only the catalogs."""
    lv, mv = _Vocab(levers), _Vocab(metrics)
    low = " ".join(text.lower().split())
    out: dict[str, Any] = {
        "intent": "compare",
        "summary": text.strip()[:200],
        "changes": [],
        "metrics": [],
        "assumptions": [],
        "ambiguities": [],
        "unsupported": [],
    }
    consumed: list[tuple[int, int]] = []

    def lever_for(noun: str, span: tuple[int, int], numeric: bool) -> str | None:
        ids = lv.match(noun)
        if numeric:
            ids = [i for i in ids if lv.items[i].get("type") in ("int", "float", "duration")]
        if len(ids) == 1:
            consumed.append(span)
            return ids[0]
        if len(ids) > 1:
            consumed.append(span)
            out["ambiguities"].append(f"'{noun.strip()}' could mean any of {ids}; which one?")
            return None
        consumed.append(span)
        near = lv.nearest(noun, numeric_only=numeric)
        out["unsupported"].append(
            {
                "request": noun.strip(),
                "reason": "not a lever in this domain's Lever Catalog",
                "nearest_lever": near,
            }
        )
        return None

    # goal seeking: "smallest fleet that meets a 95% on-time rate"
    goal = re.search(
        r"\b(smallest|fewest|minimum|least|lowest|largest|most|maximum|highest)\s+"
        r"(?P<noun>[a-z][a-z \-]*?)\s+(?:that|which|to|needed to|required to)\s+"
        r"(?:still\s+)?(?:meets?|achieves?|reach(?:es)?|hits?|keeps?|gets?)\s+"
        r"(?:an?\s+|the\s+|at least\s+)?(?P<num>\d+(?:\.\d+)?)\s*(?P<pct>%|percent)?\s*"
        r"(?P<metric>[a-z][a-z \-]*?)(?:\s+target)?(?:[.,;]|$|\s+with\b)",
        low,
    )
    if goal:
        lever = lever_for(goal["noun"], goal.span(), numeric=True)
        mids = mv.match(goal["metric"])
        target = float(goal["num"]) / (100 if goal["pct"] else 1)
        if lever and len(mids) == 1:
            out["intent"] = "goal_seek"
            out["goal"] = {
                "lever": lever,
                "metric": mids[0],
                "target": target,
                "sense": ">=",
                "minimize_lever": goal[1] in ("smallest", "fewest", "minimum", "least", "lowest"),
            }
            out["metrics"] = [mids[0]]
            out["assumptions"].append(
                f"'{goal['metric'].strip()}' read as metric {mids[0]}; target {target:g} must "
                "hold for the whole confidence interval"
            )
        elif not mids:
            out["ambiguities"].append(f"which metric is '{goal['metric'].strip()}'?")

    # sweep: "sweep jacks from 10 to 30 by 5"
    sweep = re.search(
        r"\b(?:sweep|vary|try)\s+(?P<noun>[a-z][a-z \-]*?)\s+from\s+(?P<a>\d+(?:\.\d+)?)\s+"
        r"to\s+(?P<b>\d+(?:\.\d+)?)(?:\s+(?:by|in steps of|step)\s+(?P<c>\d+(?:\.\d+)?))?",
        low,
    )
    if sweep and "goal" not in out:
        lever = lever_for(sweep["noun"], sweep.span(), numeric=True)
        if lever:
            step = float(sweep["c"]) if sweep["c"] else 1.0
            if not sweep["c"]:
                out["assumptions"].append("no step given; using steps of 1")
            out["intent"] = "sweep"
            out["sweep"] = {
                "lever": lever,
                "start": float(sweep["a"]),
                "stop": float(sweep["b"]),
                "step": step,
            }

    def free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in consumed)

    noun = (
        r"(?P<noun>[a-z][a-z \-]*?)"
        r"(?=\s+(?:and|to|by|vs|versus|compared|then|with|on|in|for)\b|[.,;]|$)"
    )
    # relative changes
    for m in re.finditer(
        rf"\b(?P<verb>add|hire|remove|drop|cut|lose)\s+{_NUM}\s+{_FILLER}{noun}", low
    ):
        if not free(m.span()):
            continue
        lever = lever_for(m["noun"], m.span(), numeric=True)
        if lever:
            n = _num(m[2])
            sign = -1 if m["verb"] in ("remove", "drop", "cut", "lose") else 1
            out["changes"].append({"lever": lever, "op": "add", "value": _clean(sign * n)})
    for m in re.finditer(
        rf"\b(?P<verb>increase|raise|reduce|decrease|lower|cut)\s+{noun}\s+by\s+"
        r"(?P<n>\d+(?:\.\d+)?)\s*(?P<pct>%|percent)?",
        low,
    ):
        if not free(m.span()):
            continue
        lever = lever_for(m["noun"], m.span(), numeric=True)
        if lever:
            n = float(m["n"])
            down = m["verb"] in ("reduce", "decrease", "lower", "cut")
            if m["pct"]:
                factor = 1 - n / 100 if down else 1 + n / 100
                out["changes"].append({"lever": lever, "op": "multiply", "value": factor})
            else:
                out["changes"].append(
                    {"lever": lever, "op": "add", "value": _clean(-n if down else n)}
                )
    # absolute values: "set jacks to 20", "with 20 jacks"
    for m in re.finditer(rf"\b(?:set|change|make)\s+{noun}\s+(?:to|=)\s+(?P<val>[\w.\-]+)", low):
        if not free(m.span()):
            continue
        lever = lever_for(m["noun"], m.span(), numeric=False)
        if lever:
            val: Any = m["val"]
            try:
                val = _clean(float(val))
            except ValueError:
                val = _choice(lv.items[lever], val) or val
            out["changes"].append({"lever": lever, "op": "set", "value": val})
    for m in re.finditer(
        rf"\b(?:with|using|use|run|have)\s+(?P<n>\d+(?:\.\d+)?)\s+{_FILLER}{noun}", low
    ):
        if not free(m.span()) or re.match(r"(replication|rep|run|seed)s?\b", m["noun"]):
            continue
        lever = lever_for(m["noun"], m.span(), numeric=True)
        if lever:
            out["changes"].append({"lever": lever, "op": "set", "value": _clean(float(m["n"]))})
    # enum choices mentioned anywhere ("use fifo dispatch")
    for ldef in levers:
        if ldef.get("type") != "enum":
            continue
        for choice in ldef.get("choices", []):
            phrase = str(choice).replace("_", " ")
            hit = re.search(rf"\b{re.escape(phrase)}\b", low)
            if (
                hit
                and free(hit.span())
                and not any(c["lever"] == ldef["id"] for c in out["changes"])
            ):
                consumed.append(hit.span())
                out["changes"].append({"lever": ldef["id"], "op": "set", "value": choice})
    # "add a conveyor" style requests with no number: capability gap unless a lever
    for m in re.finditer(
        r"\b(?:add|install|introduce|build)\s+(?:a|an|another)\s+"
        r"(?P<noun>[a-z][a-z\-]*(?: [a-z\-]+)?)",
        low,
    ):
        if free(m.span()):
            gap_noun = re.sub(r"\s+(?:and|to|vs|then|with|for|in|on)$", "", m["noun"])
            lever_for(gap_noun, m.span(), numeric=True)
    reps = re.search(r"\b(\d+)\s+(?:replications|reps|replicates|runs)\b", low)
    if reps:
        out["replications"] = int(reps[1])
    for mdef in metrics:
        for name in [
            str(mdef["id"]).replace("_", " "),
            str(mdef.get("display_name", "")),
            *mdef.get("synonyms", []),
        ]:
            if name and re.search(rf"\b{re.escape(name.lower())}\b", low):
                if mdef["id"] not in out["metrics"]:
                    out["metrics"].append(mdef["id"])
                break
    if out["changes"]:
        for c in out["changes"]:
            name = lv.items[c["lever"]].get("display_name", c["lever"])
            out["assumptions"].append(f"'{c['lever']}' is {name}; change: {c['op']} {c['value']}")
        out["assumptions"].append("compared against the unchanged baseline")
    if not (out["changes"] or out.get("sweep") or out.get("goal")):
        if out["unsupported"]:
            out["intent"] = "compare"
        else:
            out["intent"] = "clarify"
            ids = sorted(lv.items)
            out["ambiguities"].append(
                f"I could not map the request to a lever; supported levers are {ids}"
            )
    return out


def _choice(lever: Mapping[str, Any], text: str) -> Any:
    for c in lever.get("choices", []):
        if str(c).replace("_", " ") == text.replace("_", " "):
            return c
    return None


class HeuristicCompilerClient(ReasoningClient):
    """Answers ``compile_scenario`` requests with :func:`compile_text`.

    It reads the request text and catalogs from ``request.metadata`` (filled by the
    compiler alongside the prompt), so it gives the same answer as long as the
    catalogs are the same.
    """

    model = "heuristic-1"

    def complete(self, request: ReasoningRequest) -> ReasoningResponse:
        md = request.metadata
        if md.get("task") != "compile_scenario":
            raise ValueError("the heuristic client only handles compile_scenario requests")
        edit = compile_text(str(md["request"]), md.get("levers", []), md.get("metrics", []))
        return ReasoningResponse(structured=edit, model=self.model)
