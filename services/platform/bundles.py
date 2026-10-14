"""(De)serialize scenario bundles for the API and workers; resolve references safely."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from simcore.core.config import ConfigError, ConfigIssue, ScenarioBundle, bundle_from_specs
from simcore.core.config import load_scenario as _load_scenario
from simcore.specs import BehaviorsSpec, FacilityManifest, ScenarioSpec

__all__ = ["bundle_from_json", "bundle_to_json", "check_paths", "load_example"]


def bundle_to_json(bundle: ScenarioBundle) -> dict[str, Any]:
    return {
        "scenario": bundle.scenario.model_dump(mode="json", by_alias=True),
        "behaviors": bundle.behaviors.model_dump(mode="json", by_alias=True)
        if bundle.behaviors
        else None,
        "facility": bundle.facility.model_dump(mode="json", by_alias=True)
        if bundle.facility
        else None,
        "source": bundle.source,
    }


def bundle_from_json(
    data: dict[str, Any], allowed_roots: list[Path] | None = None
) -> ScenarioBundle:
    """Inline bundle -> ScenarioBundle. File paths inside (replay demand, plans) must
    live under ``allowed_roots`` (uploads are untrusted; NFR-14)."""
    try:
        scenario = ScenarioSpec.model_validate(data["scenario"])
        behaviors = (
            BehaviorsSpec.model_validate(data["behaviors"]) if data.get("behaviors") else None
        )
        facility = (
            FacilityManifest.model_validate(data["facility"]) if data.get("facility") else None
        )
    except KeyError as exc:
        raise ConfigError(
            [
                ConfigIssue(
                    "<request>", str(exc), "missing field", "send {scenario, behaviors?, facility?}"
                )
            ]
        ) from exc
    except Exception as exc:
        raise ConfigError(
            [ConfigIssue("<request>", "", str(exc), "fix the scenario bundle")]
        ) from exc
    bundle = bundle_from_specs(scenario, behaviors, facility, str(data.get("source", "<api>")))
    if allowed_roots is not None:
        check_paths(bundle, allowed_roots)
    return bundle


def check_paths(bundle: ScenarioBundle, roots: list[Path]) -> None:
    resolved = [r.resolve() for r in roots]

    def ok(path: str) -> bool:
        p = Path(path).resolve()
        return any(p == r or r in p.parents for r in resolved)

    issues = []
    for n, src in enumerate(bundle.scenario.demand):
        path = getattr(src, "path", None)
        if path is not None and not ok(path):
            issues.append(
                ConfigIssue(
                    "<request>",
                    f"demand.{n}.path",
                    "file is outside the server's data directories",
                    "upload the data or use a library scenario",
                )
            )
    plan = bundle.scenario.policy.params.get("plan")
    if isinstance(plan, str) and not ok(plan):
        issues.append(
            ConfigIssue(
                "<request>",
                "policy.params.plan",
                "plan file is outside the server's data directories",
                "send the plan inline",
            )
        )
    if issues:
        raise ConfigError(issues)


def load_example(examples_dir: Path, relative: str) -> ScenarioBundle:
    path = (examples_dir / relative).resolve()
    if examples_dir.resolve() not in path.parents:
        raise ConfigError(
            [
                ConfigIssue(
                    "<request>",
                    "example",
                    f"{relative!r} is not an example",
                    "use a path under the examples directory",
                )
            ]
        )
    return _load_scenario(path)
