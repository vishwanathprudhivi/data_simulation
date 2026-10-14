"""Number-traceability check (FR-CHAT-03, AC-CHAT-02).

Every number in assistant text must come from a tool result (or the user's own
words) in the same session. A number matches evidence when it equals an
evidence value at the precision it is written with, directly, as a percentage
of a fraction, or by magnitude (a "drop of 2.3" for a diff of -2.3).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["GroundingReport", "check_grounding", "evidence_numbers", "extract_numbers"]

# a number not glued to identifier characters (job_12, rep_003, v2, M9, jacks-40)
_NUM_RE = re.compile(
    r"(?<![\w.\-])(?P<sign>[-+\u2212])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<pct>\s?%)?(?![\w\-]|\.\d)"
)
_LIST_MARK = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
_REF_RE = re.compile(r"\{\{([A-Za-z0-9_]+):([A-Za-z0-9_]+)\}\}")


@dataclass(frozen=True)
class NumberMention:
    text: str
    value: float
    decimals: int
    percent: bool
    start: int


@dataclass
class GroundingReport:
    ok: bool
    checked: int
    unreferenced: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "unreferenced": self.unreferenced,
            "refs": self.refs,
        }


def extract_numbers(text: str) -> list[NumberMention]:
    markers = {
        m.start() + len(m.group(0)) - len(m.group(0).lstrip()) for m in _LIST_MARK.finditer(text)
    }
    out: list[NumberMention] = []
    for m in _NUM_RE.finditer(text):
        if m.start() in markers:
            continue  # "1. first point" list numbering
        raw = m["num"].replace(",", "")
        value = float(raw)
        if m["sign"] in ("-", "\u2212"):
            value = -value
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        out.append(NumberMention(m.group(0).strip(), value, decimals, bool(m["pct"]), m.start()))
    return out


def evidence_numbers(evidence: Iterable[Any]) -> list[float]:
    """Every number inside the evidence objects, including numbers written in strings."""
    vals: list[float] = []

    def walk(x: Any) -> None:
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, (int, float)):
            if not (math.isnan(x) or math.isinf(x)):
                vals.append(float(x))
        elif isinstance(x, str):
            vals.extend(n.value for n in extract_numbers(x))
        elif isinstance(x, Mapping):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    for e in evidence:
        walk(e)
    return vals


def _matches(n: NumberMention, values: list[float]) -> bool:
    tol = 0.5 * 10 ** (-n.decimals) + 1e-9
    targets = [n.value, abs(n.value)]
    for v in values:
        for cand in (v, abs(v)) + ((v * 100, abs(v) * 100) if n.percent else ()):
            for t in targets:
                if abs(cand - t) <= tol:
                    return True
                # numbers written with thousands formatting / rounding to integers
                if n.decimals == 0 and abs(cand) >= 100 and round(cand) == round(t):
                    return True
    return False


def check_grounding(text: str, evidence: Iterable[Any]) -> GroundingReport:
    """Flag numbers in ``text`` that no evidence value supports."""
    values = evidence_numbers(evidence)
    refs = [f"{a}:{b}" for a, b in _REF_RE.findall(text)]
    body = _REF_RE.sub("", text)
    mentions = extract_numbers(body)
    bad = [m.text for m in mentions if not _matches(m, values)]
    return GroundingReport(not bad, len(mentions), bad, refs)
