"""Config loading: YAML -> migrated, merged, validated specs (FR-ENG-08, FR-SCN-05, BRD 7.1).

Errors are collected into a :class:`ConfigError` whose message says *what* is
wrong, *where* (file and field path) and *how to fix it*.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from simcore.core.hashing import content_hash
from simcore.specs.behaviors import BehaviorsSpec
from simcore.specs.common import SCHEMA_VERSION
from simcore.specs.experiment import ExperimentSpec
from simcore.specs.facility import FacilityManifest
from simcore.specs.registry import SPECS
from simcore.specs.scenario import ScenarioSpec

__all__ = [
    "ConfigError",
    "ConfigIssue",
    "ScenarioBundle",
    "apply_overrides",
    "deep_merge",
    "load_document",
    "load_experiment",
    "load_scenario",
    "load_spec",
    "register_migration",
    "scenario_hash",
    "validate_spec",
]

M = TypeVar("M", bound=BaseModel)

#: Keys used to match list items when merging or addressing by path.
LIST_KEYS: tuple[str, ...] = ("id", "agent_type", "stream", "name")


@dataclass(frozen=True)
class ConfigIssue:
    source: str
    location: str
    message: str
    hint: str = ""

    def __str__(self) -> str:
        loc = f" at '{self.location}'" if self.location else ""
        hint = f"\n    fix: {self.hint}" if self.hint else ""
        return f"{self.source}{loc}: {self.message}{hint}"


class ConfigError(ValueError):
    """Invalid configuration. ``issues`` lists every problem found."""

    def __init__(self, issues: Sequence[ConfigIssue]) -> None:
        self.issues = list(issues)
        lines = "\n  - ".join(str(i) for i in self.issues)
        super().__init__(f"{len(self.issues)} configuration problem(s):\n  - {lines}")


# --------------------------------------------------------------------------- migrations

Migration = Callable[[dict[str, Any]], dict[str, Any]]
_MIGRATIONS: dict[tuple[str, str], tuple[str, Migration]] = {}


def register_migration(kind: str, from_version: str, to_version: str, fn: Migration) -> None:
    """Register a migration hook upgrading ``kind`` documents from one version to the next."""
    _MIGRATIONS[(kind, from_version)] = (to_version, fn)


def _migrate(kind: str, doc: dict[str, Any], source: str) -> dict[str, Any]:
    version = str(doc.get("schema_version", SCHEMA_VERSION))
    seen: set[str] = set()
    while version != SCHEMA_VERSION:
        if version in seen or (kind, version) not in _MIGRATIONS:
            raise ConfigError(
                [
                    ConfigIssue(
                        source,
                        "schema_version",
                        f"unsupported schema_version {version!r} for {kind} "
                        f"(this simcore supports {SCHEMA_VERSION!r})",
                        "upgrade simcore or migrate the document",
                    )
                ]
            )
        seen.add(version)
        version, fn = _MIGRATIONS[(kind, version)]
        doc = fn(copy.deepcopy(doc))
        doc["schema_version"] = version
    return doc


# --------------------------------------------------------------------------- yaml + merge


def load_document(path: Path) -> dict[str, Any]:
    """Read a YAML/JSON mapping from ``path`` with actionable errors."""
    if not path.exists():
        raise ConfigError(
            [ConfigIssue(str(path), "", "file not found", "check the path (relative to the file)")]
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(
            [ConfigIssue(str(path), where, f"invalid YAML: {exc}", "fix the YAML syntax")]
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            [ConfigIssue(str(path), "", "top level must be a mapping", "use 'key: value' pairs")]
        )
    return data


def _item_key(item: Any) -> tuple[str, Any] | None:
    if isinstance(item, Mapping):
        for k in LIST_KEYS:
            if k in item:
                return (k, item[k])
    return None


def deep_merge(base: Any, override: Any) -> Any:
    """Merge ``override`` onto ``base``.

    Mappings merge recursively. Lists whose items are all keyed mappings (by one
    of ``id``/``agent_type``/``stream``/``name``) merge item-wise by key, with
    new items appended; other lists are replaced.
    """
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        out = dict(base)
        for k, v in override.items():
            out[k] = deep_merge(base[k], v) if k in base else copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(override, list):
        bkeys = [_item_key(i) for i in base]
        okeys = [_item_key(i) for i in override]
        if base and override and None not in bkeys and None not in okeys:
            merged = list(base)
            index = {k: n for n, k in enumerate(bkeys)}
            for k, item in zip(okeys, override):
                if k in index:
                    merged[index[k]] = deep_merge(merged[index[k]], item)
                else:
                    merged.append(copy.deepcopy(item))
            return merged
    return copy.deepcopy(override)


def _split_path(path: str) -> list[str]:
    parts = [p for p in path.replace("[", ".").replace("]", "").split(".") if p]
    if not parts:
        raise ValueError("empty override path")
    return parts


def apply_overrides(doc: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``doc`` with dotted-path ``overrides`` applied.

    Path segments address mapping keys, list indexes, or list items by key, e.g.
    ``fleet.server.count`` selects the fleet item whose ``agent_type`` is
    ``server``. Missing mapping keys are created; missing list items are errors.
    """
    out: dict[str, Any] = copy.deepcopy(dict(doc))
    for path in sorted(overrides):  # sorted: deterministic and parent-before-child
        parts = _split_path(path)
        node: Any = out
        for n, part in enumerate(parts):
            last = n == len(parts) - 1
            if isinstance(node, dict):
                if last:
                    node[part] = copy.deepcopy(overrides[path])
                else:
                    node = node.setdefault(part, {})
            elif isinstance(node, list):
                idx = _list_index(node, part, path)
                if last:
                    node[idx] = copy.deepcopy(overrides[path])
                else:
                    node = node[idx]
            else:
                raise ValueError(f"override {path!r}: cannot descend into {type(node).__name__}")
    return out


def _list_index(items: list[Any], part: str, path: str) -> int:
    if part.isdigit():
        idx = int(part)
        if idx >= len(items):
            raise ValueError(f"override {path!r}: index {idx} out of range ({len(items)} items)")
        return idx
    for idx, item in enumerate(items):
        key = _item_key(item)
        if key is not None and str(key[1]) == part:
            return idx
    keys = [str(k[1]) for k in (_item_key(i) for i in items) if k is not None]
    raise ValueError(f"override {path!r}: no list item keyed {part!r}; available: {keys}")


def _resolve_extends(path: Path, chain: tuple[Path, ...] = ()) -> dict[str, Any]:
    path = path.resolve()
    if path in chain:
        cycle = " -> ".join(p.name for p in (*chain, path))
        raise ConfigError([ConfigIssue(str(path), "extends", f"inheritance cycle: {cycle}")])
    doc = load_document(path)
    _absolutize_refs(doc, path.parent)
    parent_ref = doc.get("extends")
    if parent_ref is None:
        return doc
    parent = _resolve_extends(Path(str(parent_ref)), (*chain, path))
    merged = deep_merge(parent, {k: v for k, v in doc.items() if k != "extends"})
    merged["extends"] = str(parent_ref)
    return dict(merged)


_REF_FIELDS = ("extends", "facility", "behaviors")


def _absolutize_refs(doc: dict[str, Any], base: Path) -> None:
    for key in _REF_FIELDS:
        ref = doc.get(key)
        if isinstance(ref, str) and not Path(ref).is_absolute():
            doc[key] = str((base / ref).resolve())
    policy = doc.get("policy")
    if isinstance(policy, dict) and isinstance(policy.get("params"), dict):
        plan = policy["params"].get("plan")
        if isinstance(plan, str) and not Path(plan).is_absolute():
            policy["params"]["plan"] = str((base / plan).resolve())
    for src in doc.get("demand", []) or []:
        path = src.get("path") if isinstance(src, dict) else None
        if isinstance(path, str) and not Path(path).is_absolute():
            src["path"] = str((base / path).resolve())


# --------------------------------------------------------------------------- validation


def _issues_from(exc: ValidationError, source: str) -> list[ConfigIssue]:
    issues = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"]
        if "input" in err and err["type"] not in ("missing",):
            got = repr(err["input"])
            if len(got) <= 80:
                msg = f"{msg} (got {got})"
        hint = {
            "missing": "add this required field",
            "extra_forbidden": "remove this field or check its spelling",
            "literal_error": "use one of the allowed values",
            "union_tag_invalid": "set 'kind' to one of the allowed values",
        }.get(err["type"], "")
        issues.append(ConfigIssue(source, loc, msg, hint))
    return issues


def validate_spec(model: type[M], data: Mapping[str, Any], source: str = "<input>") -> M:
    """Validate ``data`` against ``model`` raising :class:`ConfigError` on failure."""
    kind = next((k for k, v in SPECS.items() if v is model), model.__name__)
    migrated = _migrate(kind, dict(data), source)
    try:
        return model.model_validate(migrated)
    except ValidationError as exc:
        raise ConfigError(_issues_from(exc, source)) from exc


def load_spec(model: type[M], path: Path) -> M:
    """Load and validate one document (no ``extends`` resolution)."""
    return validate_spec(model, load_document(path), str(path))


# --------------------------------------------------------------------------- scenario bundle


@dataclass(frozen=True)
class ScenarioBundle:
    """A resolved scenario with its referenced facility and behaviors."""

    scenario: ScenarioSpec
    behaviors: BehaviorsSpec | None
    facility: FacilityManifest | None
    source: str
    hashes: dict[str, str] = field(default_factory=dict)

    @property
    def config_hash(self) -> str:
        """Hash over all three layers: identifies the run configuration."""
        return content_hash(self.hashes)

    def with_scenario(self, scenario: ScenarioSpec) -> ScenarioBundle:
        hashes = dict(self.hashes)
        hashes["scenario"] = scenario_hash(scenario)
        return ScenarioBundle(scenario, self.behaviors, self.facility, self.source, hashes)

    def with_overrides(self, overrides: Mapping[str, Any]) -> ScenarioBundle:
        """Apply dotted-path overrides to the scenario and re-validate."""
        if not overrides:
            return self
        try:
            data = apply_overrides(self.scenario.model_dump(mode="json", by_alias=True), overrides)
        except ValueError as exc:
            raise ConfigError([ConfigIssue(self.source, "overrides", str(exc))]) from exc
        return self.with_scenario(validate_spec(ScenarioSpec, data, f"{self.source} (overrides)"))


def scenario_hash(scenario: ScenarioSpec) -> str:
    """Content hash of a scenario that is independent of where files live on disk.

    File references are excluded (their contents are hashed separately); replay
    demand files are hashed by content.
    """
    data = scenario.model_dump(mode="json")
    for key in _REF_FIELDS:
        data.pop(key, None)
    for src in data.get("demand", []):
        if src.get("kind") == "replay":
            file = Path(src["path"])
            src["path"] = (
                hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else src["path"]
            )
    return content_hash(data)


def bundle_from_specs(
    scenario: ScenarioSpec,
    behaviors: BehaviorsSpec | None = None,
    facility: FacilityManifest | None = None,
    source: str = "<memory>",
) -> ScenarioBundle:
    hashes = {"scenario": scenario_hash(scenario)}
    if behaviors is not None:
        hashes["behaviors"] = content_hash(behaviors.model_dump(mode="json"))
    if facility is not None:
        hashes["facility"] = content_hash(facility.model_dump(mode="json"))
    return ScenarioBundle(scenario, behaviors, facility, source, hashes)


def load_scenario(path: Path | str, overrides: Mapping[str, Any] | None = None) -> ScenarioBundle:
    """Load ``scenario.yaml`` resolving ``extends``, refs and optional overrides."""
    path = Path(path)
    raw = _resolve_extends(path)
    if overrides:
        try:
            raw = apply_overrides(raw, overrides)
        except ValueError as exc:
            raise ConfigError([ConfigIssue(str(path), "overrides", str(exc))]) from exc
    scenario = validate_spec(ScenarioSpec, raw, str(path))
    issues: list[ConfigIssue] = []
    behaviors = facility = None
    if scenario.behaviors is not None:
        try:
            behaviors = load_spec(BehaviorsSpec, Path(scenario.behaviors))
        except ConfigError as exc:
            issues.extend(exc.issues)
    if scenario.facility is not None:
        try:
            facility = load_spec(FacilityManifest, Path(scenario.facility))
        except ConfigError as exc:
            issues.extend(exc.issues)
    if behaviors is not None:
        known = {a.id for a in behaviors.agent_types}
        for n, entry in enumerate(scenario.fleet):
            if entry.agent_type not in known:
                issues.append(
                    ConfigIssue(
                        str(path),
                        f"fleet.{n}.agent_type",
                        f"agent type {entry.agent_type!r} is not defined in behaviors",
                        f"define it in {scenario.behaviors} or use one of {sorted(known)}",
                    )
                )
    elif scenario.fleet:
        issues.append(
            ConfigIssue(
                str(path),
                "behaviors",
                "scenario declares a fleet but no behaviors file",
                "add 'behaviors: <path to behaviors.yaml>'",
            )
        )
    if issues:
        raise ConfigError(issues)
    return bundle_from_specs(scenario, behaviors, facility, str(path))


def load_experiment(path: Path | str) -> tuple[ExperimentSpec, ScenarioBundle]:
    """Load ``experiment.yaml`` and its base scenario (path relative to the experiment)."""
    path = Path(path)
    doc = load_document(path)
    base = doc.get("base_scenario")
    if isinstance(base, str) and not Path(base).is_absolute():
        doc["base_scenario"] = str((path.parent / base).resolve())
    spec = validate_spec(ExperimentSpec, doc, str(path))
    return spec, load_scenario(spec.base_scenario)
