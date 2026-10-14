"""Evaluator API for optimization loops (FR-IFC-03, AC-IFC-01, BO-3).

``evaluate(params, replications, seed)`` returns KPI means with confidence
intervals. Results are cached on ``(config hash after params, seed,
replications, engine version, domain version, mode)``; a repeated call is served
from the cache without simulating. Plans (task -> agent -> sequence) as inputs
arrive with the plan-follower policy in M5.
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simcore.core.config import ScenarioBundle, load_scenario
from simcore.core.hashing import content_hash
from simcore.core.plugins import PluginRegistry
from simcore.core.provenance import engine_version
from simcore.experiment.runner import ExperimentRunner
from simcore.experiment.stats import CIEstimate, mean_ci
from simcore.specs.experiment import ExperimentSpec
from simcore.specs.plan import PlanSpec

__all__ = ["Evaluation", "Evaluator"]


@dataclass(frozen=True)
class Evaluation:
    key: str
    params: Mapping[str, Any]
    replications: int
    master_seed: int
    confidence: float
    per_replication: list[dict[str, float]]
    failures: list[str]
    cached: bool
    kpis: dict[str, CIEstimate] = field(default_factory=dict)

    def mean(self, metric: str) -> float:
        return self.kpis[metric].mean

    def to_dict(self) -> dict[str, Any]:
        def num(v: float) -> float | None:
            return None if math.isnan(v) or math.isinf(v) else v

        return {
            "key": self.key,
            "params": dict(self.params),
            "replications": self.replications,
            "master_seed": self.master_seed,
            "confidence": self.confidence,
            "cached": self.cached,
            "failures": self.failures,
            "kpis": {
                k: {"mean": num(c.mean), "ci": [num(c.low), num(c.high)], "n": c.n}
                for k, c in self.kpis.items()
            },
        }


def _cis(
    per_rep: list[dict[str, float]], confidence: float, kpis: Sequence[str] | None
) -> dict[str, CIEstimate]:
    names = sorted({k for r in per_rep for k in r}) if not kpis else list(kpis)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return {k: mean_ci([r.get(k, math.nan) for r in per_rep], confidence) for k in names}


class Evaluator:
    def __init__(
        self,
        scenario: ScenarioBundle | str | Path,
        plugins: PluginRegistry | None = None,
        *,
        mode: str = "logical",
        parallelism: int = 1,
        confidence: float = 0.95,
        cache_dir: Path | None = None,
        timeout_s: float | None = None,
    ) -> None:
        if plugins is None:
            from simcore.api.engine import default_plugins

            plugins = default_plugins()
        self.base = scenario if isinstance(scenario, ScenarioBundle) else load_scenario(scenario)
        self.plugins = plugins
        self.plugin = plugins.get(self.base.scenario.domain)
        self.mode = mode
        self.parallelism = parallelism
        self.confidence = confidence
        self.timeout_s = timeout_s
        self.cache_dir = cache_dir
        self._memory: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0

    def cache_key(self, params: Mapping[str, Any], replications: int, seed: int) -> str:
        bundle = self.base.with_overrides(params)
        return content_hash(
            {
                "config_hash": bundle.config_hash,
                "seed": seed,
                "replications": replications,
                "engine_version": engine_version(),
                "domain": [self.plugin.name, self.plugin.version],
                "mode": self.mode,
            }
        )

    def _load(self, key: str) -> dict[str, Any] | None:
        if key in self._memory:
            return self._memory[key]
        if self.cache_dir is not None:
            path = self.cache_dir / f"{key}.json"
            if path.exists():
                entry: dict[str, Any] = json.loads(path.read_text())
                self._memory[key] = entry
                return entry
        return None

    def _store(self, key: str, entry: dict[str, Any]) -> None:
        self._memory[key] = entry
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{key}.json").write_text(json.dumps(entry, sort_keys=True))

    def evaluate(
        self,
        params: Mapping[str, Any] | None = None,
        replications: int = 10,
        seed: int = 0,
        kpis: Sequence[str] | None = None,
        plan: PlanSpec | Mapping[str, Any] | None = None,
    ) -> Evaluation:
        """Run (or fetch from cache) ``replications`` of the scenario with ``params`` overrides.

        Seeds are ``(seed, 0..replications-1)``: two calls with the same seed
        use common random numbers, so their KPI differences can be paired.
        """
        params = dict(params or {})
        if plan is not None:  # evaluate an optimizer's plan (FR-IFC-03 plan_or_params)
            # normalise so equal plans hash equally however they were given
            plan_spec = plan if isinstance(plan, PlanSpec) else PlanSpec.model_validate(dict(plan))
            data = plan_spec.model_dump(mode="json")
            params = {**params, "policy.name": "plan_follower", "policy.params": {"plan": data}}
        key = self.cache_key(params, replications, seed)
        entry = self._load(key)
        cached = entry is not None
        if entry is None:
            self.misses += 1
            spec = ExperimentSpec(
                id="evaluation",
                base_scenario=self.base.source,
                variants=[],
                replications=replications,
                master_seed=seed,
                crn=True,
                confidence=self.confidence,
                parallelism=self.parallelism,
                timeout_s=self.timeout_s,
            )
            result = ExperimentRunner(
                spec, self.base.with_overrides(params), self.plugins, mode=self.mode
            ).run()
            entry = {
                "per_replication": [o.kpis for o in result.outcomes if o.ok],
                "failures": [f"rep {o.replication}: {o.error}" for o in result.failed],
            }
            self._store(key, entry)
        else:
            self.hits += 1
        per_rep = entry["per_replication"]
        return Evaluation(
            key=key,
            params=params,
            replications=replications,
            master_seed=seed,
            confidence=self.confidence,
            per_replication=per_rep,
            failures=entry["failures"],
            cached=cached,
            kpis=_cis(per_rep, self.confidence, kpis),
        )
