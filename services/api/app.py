"""simcore HTTP API (FR-API-01..09, AC-API-01). Run with ``make api``.

Every capability the UI or chat uses is an endpoint here first (API-first rule).
Versioned under ``/api/v1``; the OpenAPI document is at ``/api/v1/openapi.json``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from services.platform.artifacts import ArtifactStore, open_artifact_store
from services.platform.auth import AuthError, authenticate, issue_token
from services.platform.bundles import bundle_from_json, bundle_to_json
from services.platform.jobs import (
    TERMINAL,
    InProcessBroker,
    JobService,
    QuotaExceeded,
    RedisBroker,
    SubmissionRejected,
)
from services.platform.metadata import MetadataStore
from services.platform.settings import Settings
from simcore import __version__
from simcore.api import default_plugins
from simcore.catalog import (
    apply_levers,
    capability_manifest,
    dimension_catalog,
    lever_catalog,
    metric_catalog,
)
from simcore.core.config import ConfigError, ScenarioBundle, load_scenario
from simcore.core.facility.graph import NavGraph
from simcore.core.plugins import PluginError, PluginRegistry
from simcore.specs.experiment import ExperimentSpec
from simcore.specs.registry import SPECS, schema_for

__all__ = ["Container", "create_app"]

log = logging.getLogger("simcore.api")
API = "/api/v1"


# ----------------------------------------------------------------------------- container


@dataclass
class Container:
    settings: Settings
    store: MetadataStore
    artifacts: ArtifactStore
    plugins: PluginRegistry
    jobs: JobService


@dataclass(frozen=True)
class Caller:
    user: str
    workspace: str


def _seed_examples(c: Container) -> None:
    """Register example scenarios in the default workspace library (pinned baselines)."""
    root = c.settings.examples_dir
    if root is None or not (root / "scenarios").exists():
        return
    if c.store.scenarios("default"):
        return
    for path in sorted((root / "scenarios").glob("*/*.yaml")):
        if path.name == "behaviors.yaml":
            continue
        try:
            bundle = load_scenario(path)
        except ConfigError as exc:
            log.warning("skipping example %s: %s", path, exc)
            continue
        if bundle.scenario.domain not in c.plugins:
            continue
        c.store.save_scenario(
            "default",
            bundle.scenario.id,
            bundle.scenario.name or bundle.scenario.id,
            bundle.scenario.domain,
            bundle.scenario.domain_version,
            bundle_to_json(bundle),
            ["example", *bundle.scenario.tags],
            pinned="baseline" in path.stem,
            user="system",
        )


# ----------------------------------------------------------------------------- request models


class ScenarioRef(BaseModel):
    """Exactly one of: a library scenario (id + optional version), an example path, or an
    inline bundle ``{scenario, behaviors?, facility?}``."""

    model_config = ConfigDict(extra="forbid")
    scenario_id: str | None = None
    version: int | None = None
    example: str | None = None
    bundle: dict[str, Any] | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    levers: dict[str, Any] = Field(default_factory=dict)


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment: dict[str, Any]
    scenario: ScenarioRef
    mode: str = "logical"
    priority: int = Field(default=0, ge=-10, le=10)
    confirmation_token: str | None = None


class SaveScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: ScenarioRef
    id: str | None = None
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    pin: bool = False


class LoginRequest(BaseModel):
    user: str
    password: str


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    query: dict[str, Any]


class ChartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str | None = None
    domain: str | None = None
    chart: dict[str, Any]
    user_requested_no_uncertainty: bool = False
    format: str = Field(default="json", pattern="^(json|csv|svg|png|vega)$")


class SqlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    sql: str
    max_rows: int = Field(default=1000, ge=1, le=20000)


class GapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(max_length=100)
    request: str = Field(min_length=1, max_length=2000)
    detail: str = Field(default="", max_length=4000)


class LiveStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: ScenarioRef
    seed: int | None = None
    speed: float = Field(default=60.0, gt=0, le=100_000)


class LiveControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(play|pause|step|speed|inject)$")
    seconds: float | None = Field(default=None, gt=0, le=10_000_000)
    speed: float | None = Field(default=None, gt=0, le=100_000)
    inject: dict[str, Any] | None = None


class AttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(description="CSV text (max 5 MB).")


class ApplyAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: ScenarioRef
    source: dict[str, Any] = Field(description="SourceSpec without 'path' (kind, columns, ...).")
    arrival_paths: dict[str, str] = Field(default_factory=dict)
    lever: str | None = Field(default=None, description="samples: distribution lever to set.")


class DraftDomainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)


class CompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    scenario: ScenarioRef
    replications: int | None = Field(default=None, ge=1, le=1000)
    mode: str = "logical"


class ChatStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: ScenarioRef | None = None


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)


class ChatConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    name: str
    payload: dict[str, Any]


# ----------------------------------------------------------------------------- app


def create_app(settings: Settings | None = None, plugins: PluginRegistry | None = None) -> FastAPI:
    settings = settings or Settings()
    store = MetadataStore(settings.metadata)
    artifacts = open_artifact_store(settings.artifacts)
    plugins = plugins or default_plugins()
    if settings.broker == "redis":
        if not settings.redis_url:
            raise ValueError("SIMCORE_BROKER=redis needs SIMCORE_REDIS_URL")
        broker: Any = RedisBroker(settings.redis_url)
    else:
        broker = InProcessBroker(settings.cpu_workers, settings.gpu_workers)
    jobs = JobService(settings, store, artifacts, plugins, broker)
    from services.chat.llm import make_client

    reasoning = make_client(settings.reasoning)
    container = Container(settings, store, artifacts, plugins, jobs)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.ensure_workspace("default")
        _seed_examples(container)
        broker.start(jobs.run_job)
        yield
        broker.shutdown()

    app = FastAPI(
        title="simcore API",
        version=__version__,
        lifespan=lifespan,
        openapi_url=f"{API}/openapi.json",
        docs_url=f"{API}/docs",
    )
    app.state.container = container
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )
    _install_errors(app)
    _install_rate_limit(app, settings)

    def caller(
        authorization: str | None = Header(default=None),
        x_workspace: str | None = Header(default=None),
    ) -> Caller:
        if not settings.auth_enabled:
            ws = x_workspace or "default"
            store.ensure_workspace(ws)
            return Caller("dev", ws)
        try:
            user = authenticate(store, authorization)
        except AuthError as exc:
            raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
        allowed = store.workspaces_of(user)
        ws = x_workspace or (allowed[0] if allowed else "")
        if ws not in allowed:
            raise HTTPException(403, f"user {user!r} is not a member of workspace {ws!r}")
        return Caller(user, ws)

    # ------------------------------------------------------------------ meta

    @app.get(f"{API}/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "broker": settings.broker,
            "domains": plugins.names,
        }

    @app.post(f"{API}/auth/login")
    def login(body: LoginRequest) -> dict[str, str]:
        try:
            return {"token": issue_token(store, body.user, body.password), "type": "bearer"}
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.get(f"{API}/schemas")
    def schemas() -> list[str]:
        return sorted(SPECS)

    @app.get(f"{API}/schemas/{{name}}")
    def schema(name: str) -> dict[str, Any]:
        try:
            return schema_for(name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    # ------------------------------------------------------------------ domains (FR-DOM-03)

    @app.get(f"{API}/domains")
    def domains(who: Caller = Depends(caller)) -> list[dict[str, Any]]:
        out = []
        for name in plugins.names:
            m = capability_manifest(plugins.get(name))
            out.append(
                {
                    "id": m.domain,
                    "name": m.name,
                    "version": m.domain_version,
                    "tier": m.tier,
                    "description": m.description,
                }
            )
        return out

    @app.get(f"{API}/domains/{{domain}}")
    def domain(domain: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        return capability_manifest(plugins.get(domain)).model_dump(mode="json")

    @app.get(f"{API}/domains/{{domain}}/levers")
    def levers(domain: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        return lever_catalog(plugins.get(domain)).model_dump(mode="json")

    @app.get(f"{API}/domains/{{domain}}/metrics")
    def metrics(domain: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        return metric_catalog(plugins.get(domain)).model_dump(mode="json")

    @app.get(f"{API}/domains/{{domain}}/dimensions")
    def dimensions(domain: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        return dimension_catalog(plugins.get(domain)).model_dump(mode="json")

    @app.post(f"{API}/domains/draft")
    def draft_domain_(body: DraftDomainRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        """Draft a Tier-0 domain from a process description (FR-DOM-09). Never published."""
        from simcore.onboarding.draft import draft_domain

        try:
            result = draft_domain(body.text, body.name, reasoning)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        store.audit(
            "domain.draft",
            user=who.user,
            workspace=who.workspace,
            target=result.definition.id,
            outcome=result.source,
        )
        return {
            "definition": result.definition.model_dump(mode="json", exclude_none=True),
            "yaml": result.yaml(),
            "questions": result.questions,
            "source": result.source,
            "published": False,
        }

    @app.get(f"{API}/gaps")
    def gaps(domain: str | None = None, who: Caller = Depends(caller)) -> list[dict[str, Any]]:
        return store.gaps(domain)

    @app.post(f"{API}/gaps", status_code=201)
    def add_gap(body: GapRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        """Domain-extension request for the domain owners (FR-DOM-12)."""
        gap_id = store.add_gap(
            who.workspace, body.domain, body.request, {"detail": body.detail, "user": who.user}
        )
        return {"id": gap_id}

    # ------------------------------------------------------------------ scenarios (FR-SCG-08)

    def resolve(ref: ScenarioRef, who: Caller) -> tuple[ScenarioBundle, dict[str, Any]]:
        given = [ref.scenario_id is not None, ref.example is not None, ref.bundle is not None]
        if sum(given) != 1:
            raise HTTPException(422, "give exactly one of scenario_id, example or bundle")
        roots = [settings.data_dir] + ([settings.examples_dir] if settings.examples_dir else [])
        if ref.scenario_id is not None:
            row = store.scenario(who.workspace, ref.scenario_id, ref.version)
            if row is None:
                raise HTTPException(
                    404, f"scenario {ref.scenario_id!r} not found in workspace {who.workspace!r}"
                )
            bundle = bundle_from_json(json.loads(row["bundle_json"]))
        elif ref.example is not None:
            if settings.examples_dir is None:
                raise HTTPException(404, "no examples directory is configured")
            from services.platform.bundles import load_example

            bundle = load_example(settings.examples_dir, ref.example)
        else:
            assert ref.bundle is not None
            bundle = bundle_from_json(ref.bundle, allowed_roots=roots)
        info: dict[str, Any] = {"outside_envelope": {}, "lever_overrides": {}}
        if ref.levers:
            app_ = apply_levers(bundle, plugins.get(bundle.scenario.domain), ref.levers)
            bundle = app_.bundle
            info = {"outside_envelope": app_.outside_envelope, "lever_overrides": app_.overrides}
        if ref.overrides:
            bundle = bundle.with_overrides(ref.overrides)
        return bundle, info

    @app.get(f"{API}/scenarios")
    def list_scenarios(
        domain: str | None = None, who: Caller = Depends(caller)
    ) -> list[dict[str, Any]]:
        rows = store.scenarios(who.workspace, domain)
        return [
            {
                **{k: v for k, v in r.items() if k not in ("bundle_json", "tags_json")},
                "tags": json.loads(r["tags_json"]),
            }
            for r in rows
        ]

    @app.get(f"{API}/scenarios/{{scenario_id}}")
    def get_scenario(
        scenario_id: str, version: int | None = None, who: Caller = Depends(caller)
    ) -> dict[str, Any]:
        row = store.scenario(who.workspace, scenario_id, version)
        if row is None:
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        return {
            **{k: v for k, v in row.items() if k not in ("bundle_json", "tags_json")},
            "tags": json.loads(row["tags_json"]),
            "bundle": json.loads(row["bundle_json"]),
        }

    @app.post(f"{API}/scenarios/validate")
    def validate_scenario(ref: ScenarioRef, who: Caller = Depends(caller)) -> dict[str, Any]:
        bundle, info = resolve(ref, who)
        plugin = plugins.get(bundle.scenario.domain)
        plugin.validate_scenario(bundle)
        from simcore.catalog import lever_values

        return {
            "valid": True,
            "config_hash": bundle.config_hash,
            "lever_values": lever_values(bundle, plugin),
            "scenario": bundle.scenario.model_dump(mode="json"),
            **info,
        }

    @app.post(f"{API}/scenarios", status_code=201)
    def save_scenario(body: SaveScenarioRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        bundle, info = resolve(body.scenario, who)
        plugin = plugins.get(bundle.scenario.domain)
        plugin.validate_scenario(bundle)
        sid = body.id or bundle.scenario.id
        if sid != bundle.scenario.id:
            bundle = bundle.with_overrides({"id": sid})
        parent = None
        if body.scenario.scenario_id is not None:
            prev = store.scenario(who.workspace, body.scenario.scenario_id, body.scenario.version)
            parent = (body.scenario.scenario_id, int(prev["version"])) if prev else None
        version = store.save_scenario(
            who.workspace,
            sid,
            body.name or bundle.scenario.name or sid,
            bundle.scenario.domain,
            bundle.scenario.domain_version or plugin.version,
            bundle_to_json(bundle),
            body.tags,
            parent=parent,
            pinned=body.pin,
            user=who.user,
        )
        store.audit(
            "scenario.save",
            user=who.user,
            workspace=who.workspace,
            target=sid,
            spec_hash=bundle.config_hash,
            outcome=f"v{version}",
        )
        return {"id": sid, "version": version, "config_hash": bundle.config_hash, **info}

    @app.get(f"{API}/scenarios/{{scenario_id}}/facility")
    def facility(
        scenario_id: str, version: int | None = None, who: Caller = Depends(caller)
    ) -> dict[str, Any]:
        row = store.scenario(who.workspace, scenario_id, version)
        if row is None:
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        bundle = bundle_from_json(json.loads(row["bundle_json"]))
        if bundle.facility is None:
            raise HTTPException(404, f"scenario {scenario_id!r} has no facility")
        nav = NavGraph(bundle.facility)
        return {
            "manifest": bundle.facility.model_dump(mode="json", by_alias=True),
            "nodes": {n: list(p) for n, p in nav.positions.items()},
            "edges": [
                {
                    "id": e.id,
                    "source": e.source,
                    "target": e.target,
                    "width": e.width,
                    "length": e.length,
                    "directed": e.directed,
                    "zone": e.zone,
                }
                for e in nav.edges.values()
            ],
        }

    # ------------------------------------------------------------------ experiments & jobs

    def prepare(body: ExperimentRequest, who: Caller) -> Any:
        bundle, info = resolve(body.scenario, who)
        exp = {"base_scenario": "<api>", **body.experiment}
        spec = ExperimentSpec.model_validate(exp)
        return jobs.prepare(spec, bundle, body.mode), info

    @app.post(f"{API}/experiments/estimate")
    def estimate(body: ExperimentRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        p, info = prepare(body, who)
        needs = jobs.needs_confirmation(p)
        return {
            "estimate": p.estimate.to_dict(),
            "needs_confirmation": needs,
            "confirmation_token": jobs.confirmation_token(p, who.workspace),
            "cache_key": p.cache_key,
            "cached": store.cached_job(p.cache_key) is not None,
            **info,
        }

    @app.post(f"{API}/experiments")
    def submit(body: ExperimentRequest, who: Caller = Depends(caller)) -> JSONResponse:
        p, info = prepare(body, who)
        started = time.time()
        job = jobs.submit(
            p,
            workspace=who.workspace,
            user=who.user,
            confirmation_token=body.confirmation_token,
            priority=body.priority,
        )
        # 201: a new job will run; 200: an existing active job or a cache hit was returned
        fresh = job["created_at"] >= started and not job.get("cached_from")
        return JSONResponse(_job_view(job) | info, status_code=201 if fresh else 200)

    @app.get(f"{API}/jobs")
    def list_jobs(
        status: str | None = None, limit: int = Query(50, le=500), who: Caller = Depends(caller)
    ) -> list[dict[str, Any]]:
        return [_job_view(j) for j in store.jobs(who.workspace, status, limit)]

    def own_job(job_id: str, who: Caller) -> dict[str, Any]:
        job = store.job(job_id)
        if job is None or job["workspace"] != who.workspace:
            raise HTTPException(404, f"job {job_id!r} not found")
        return job

    @app.get(f"{API}/jobs/{{job_id}}")
    def get_job(job_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        return _job_view(own_job(job_id, who))

    @app.post(f"{API}/jobs/{{job_id}}/cancel")
    def cancel(job_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        own_job(job_id, who)
        return _job_view(jobs.cancel(job_id, who.user))

    @app.get(f"{API}/jobs/{{job_id}}/events")
    async def job_events(job_id: str, who: Caller = Depends(caller)) -> StreamingResponse:
        own_job(job_id, who)

        async def stream() -> AsyncIterator[str]:
            last: tuple[Any, ...] | None = None
            while True:
                job = store.job(job_id) or {}
                state = (
                    job.get("status"),
                    job.get("progress_completed"),
                    job.get("progress_planned"),
                )
                if state != last:
                    last = state
                    yield f"event: progress\ndata: {json.dumps(_job_view(job))}\n\n"
                if job.get("status") in TERMINAL:
                    yield f"event: done\ndata: {json.dumps(_job_view(job))}\n\n"
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    def result_key(job: dict[str, Any], name: str) -> str:
        if job["status"] != "completed" or not job["result_prefix"]:
            raise HTTPException(409, f"job {job['id']} has no results yet (status {job['status']})")
        return f"{job['result_prefix']}/{name}"

    @app.get(f"{API}/jobs/{{job_id}}/summary")
    def summary(job_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        job = own_job(job_id, who)
        data: dict[str, Any] = json.loads(artifacts.get_bytes(result_key(job, "summary.json")))
        return data

    @app.get(f"{API}/jobs/{{job_id}}/report")
    def report(
        job_id: str,
        format: str = Query("html", pattern="^(html|md|pptx)$"),
        who: Caller = Depends(caller),
    ) -> Response:
        job = own_job(job_id, who)
        if format == "pptx":  # stretch export (FR-UI-09), needs python-pptx
            try:
                from services.platform.pptx_export import findings_pptx

                result, story, catalog = findings_for(job)
                deck = findings_pptx(result, story.text, catalog, f"Experiment {job_id}")
            except ImportError as exc:
                raise HTTPException(501, f"PPTX export needs python-pptx: {exc}") from exc
            return Response(
                deck,
                media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                headers={"Content-Disposition": f'attachment; filename="{job_id}.pptx"'},
            )
        body = artifacts.get_bytes(result_key(job, f"report.{format}"))
        return Response(body, media_type="text/html" if format == "html" else "text/markdown")

    @app.get(f"{API}/jobs/{{job_id}}/runs")
    def runs(job_id: str, who: Caller = Depends(caller)) -> list[dict[str, Any]]:
        import pyarrow.parquet as pq

        job = own_job(job_id, who)
        table = pq.read_table(io.BytesIO(artifacts.get_bytes(result_key(job, "runs.parquet"))))
        rows: list[dict[str, Any]] = table.to_pylist()
        return rows

    @app.get(f"{API}/jobs/{{job_id}}/artifacts")
    def list_artifacts(job_id: str, who: Caller = Depends(caller)) -> list[str]:
        job = own_job(job_id, who)
        prefix = result_key(job, "").rstrip("/")
        return [k[len(prefix) + 1 :] for k in artifacts.keys(prefix)]

    @app.get(f"{API}/jobs/{{job_id}}/artifacts/{{path:path}}")
    def get_artifact(job_id: str, path: str, who: Caller = Depends(caller)) -> Response:
        job = own_job(job_id, who)
        try:
            data = artifacts.get_bytes(result_key(job, path))
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, f"artifact {path!r} not found") from exc
        media = {
            "json": "application/json",
            "html": "text/html",
            "md": "text/markdown",
            "parquet": "application/octet-stream",
            "jsonl": "application/x-ndjson",
        }
        return Response(
            data, media_type=media.get(Path(path).suffix.lstrip("."), "application/octet-stream")
        )

    @app.get(f"{API}/jobs/{{job_id}}/variants/{{variant}}/replications/{{rep}}/events")
    def run_events(
        job_id: str,
        variant: str,
        rep: int,
        offset: int = 0,
        limit: int = Query(1000, le=20000),
        types: str | None = None,
        who: Caller = Depends(caller),
    ) -> dict[str, Any]:
        import pyarrow.parquet as pq

        job = own_job(job_id, who)
        key = result_key(job, f"{variant}/rep_{rep:03d}/events.parquet")
        try:
            table = pq.read_table(io.BytesIO(artifacts.get_bytes(key)))
        except KeyError as exc:
            raise HTTPException(404, "event log not found") from exc
        rows = table.to_pylist()
        if types:
            wanted = set(types.split(","))
            rows = [r for r in rows if r["type"] in wanted]
        page = rows[offset : offset + limit]
        for r in page:
            r["data"] = json.loads(r["data"])
        return {"total": len(rows), "offset": offset, "rows": page}

    # ------------------------------------------------------------------ interactive (M13)

    import threading

    live: dict[str, tuple[Any, threading.Lock, str]] = {}
    MAX_LIVE = 8

    def own_live(session_id: str, who: Caller) -> tuple[Any, threading.Lock]:
        entry = live.get(session_id)
        if entry is None or entry[2] != who.workspace:
            raise HTTPException(404, f"interactive session {session_id!r} not found")
        return entry[0], entry[1]

    @app.post(f"{API}/interactive/sessions", status_code=201)
    def live_start(body: LiveStartRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        """Start a live replication (FR-IFC-05); beats in the scenario drive the demo."""
        from simcore.interactive import InteractiveSession

        if sum(1 for e in live.values() if e[2] == who.workspace) >= MAX_LIVE:
            raise HTTPException(429, f"at most {MAX_LIVE} interactive sessions per workspace")
        bundle, _ = resolve(body.scenario, who)
        sess = InteractiveSession(bundle, plugins, seed=body.seed, speed=body.speed)
        sid = f"live_{uuid.uuid4().hex[:12]}"
        live[sid] = (sess, threading.Lock(), who.workspace)
        out: dict[str, Any] = {"id": sid, "state": sess.state(), "facility": None}
        if bundle.facility is not None:
            nav = NavGraph(bundle.facility)
            out["facility"] = {
                "nodes": {n: list(p) for n, p in nav.positions.items()},
                "edges": [{"id": e.id, "source": e.source, "target": e.target}
                          for e in nav.edges.values()],
            }  # fmt: skip
        store.audit("live.start", user=who.user, workspace=who.workspace, target=sid)
        return out

    def live_view(sess: Any) -> dict[str, Any]:
        return {**sess.state(), "positions": sess.positions()}

    @app.post(f"{API}/interactive/sessions/{{session_id}}/control")
    def live_control(
        session_id: str, body: LiveControlRequest, who: Caller = Depends(caller)
    ) -> dict[str, Any]:
        sess, lock = own_live(session_id, who)
        with lock:
            try:
                if body.action == "play":
                    sess.play()
                elif body.action == "pause":
                    sess.pause()
                elif body.action == "step":
                    sess.step(body.seconds or 60.0)
                elif body.action == "speed":
                    sess.set_speed(body.speed or sess.speed)
                elif body.action == "inject":
                    if not body.inject:
                        raise HTTPException(422, "inject needs an 'inject' object")
                    sess.inject(body.inject)
                    store.audit("live.inject", user=who.user, workspace=who.workspace,
                                target=session_id, outcome=str(body.inject.get("id")))  # fmt: skip
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return live_view(sess)

    @app.get(f"{API}/interactive/sessions/{{session_id}}")
    def live_state(
        session_id: str,
        advance: float = Query(0.0, ge=0, le=10),
        who: Caller = Depends(caller),
    ) -> dict[str, Any]:
        """Current state; ``advance`` wall-seconds of playback are applied first."""
        sess, lock = own_live(session_id, who)
        with lock:
            sess.tick(advance)
            return live_view(sess)

    @app.get(f"{API}/interactive/sessions/{{session_id}}/stream")
    async def live_stream(session_id: str, who: Caller = Depends(caller)) -> StreamingResponse:
        """SSE while playing: the server plays forward and pushes HUD + positions."""
        sess, lock = own_live(session_id, who)

        async def gen() -> AsyncIterator[str]:
            last = time.monotonic()
            while session_id in live:
                now = time.monotonic()
                with lock:
                    sess.tick(min(now - last, 1.0))
                    view = live_view(sess)
                last = now
                yield f"event: state\ndata: {json.dumps(view, default=str)}\n\n"
                if view["finished"] or not view["playing"]:
                    yield "event: done\ndata: {}\n\n"  # finished, paused or stopped by a beat
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})  # fmt: skip

    @app.get(f"{API}/interactive/sessions/{{session_id}}/recorded")
    def live_recorded(session_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        """The session as a reproducible scenario (injections become scheduled disruptions)."""
        sess, lock = own_live(session_id, who)
        with lock:
            return {"bundle": bundle_to_json(sess.recorded_scenario())}

    @app.delete(f"{API}/interactive/sessions/{{session_id}}")
    def live_close(session_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        sess, lock = own_live(session_id, who)
        with lock:
            sess.close()
            live.pop(session_id, None)
        return {"closed": session_id}

    # ------------------------------------------------------------------ attachments (FR-SCG-10)

    @app.post(f"{API}/attachments", status_code=201)
    def upload_attachment(body: AttachmentRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        """Store a CSV and propose how it maps to scenario inputs (not applied yet)."""
        from services.platform.attachments import inspect_csv, propose_mapping

        try:
            info = inspect_csv(body.content)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        att_id = f"att_{uuid.uuid4().hex[:12]}"
        artifacts.put_bytes(f"{who.workspace}/attachments/{att_id}.csv", body.content.encode())
        store.audit("attachment.upload", user=who.user, workspace=who.workspace, target=att_id,
                    outcome=body.name[:100])  # fmt: skip
        return {"id": att_id, "name": body.name, **info, "proposal": propose_mapping(info)}

    @app.post(f"{API}/attachments/{{att_id}}/apply")
    def apply_attachment_(
        att_id: str, body: ApplyAttachmentRequest, who: Caller = Depends(caller)
    ) -> dict[str, Any]:
        """Validate a mapping on the uploaded data and return validated scenario inputs."""
        from pydantic import ValidationError

        from services.platform.attachments import apply_attachment
        from simcore.specs.mapping import SourceSpec

        if not re.fullmatch(r"att_[0-9a-f]{12}", att_id):
            raise HTTPException(404, f"attachment {att_id!r} not found")
        try:
            content = artifacts.get_bytes(f"{who.workspace}/attachments/{att_id}.csv").decode()
        except KeyError as exc:
            raise HTTPException(404, f"attachment {att_id!r} not found") from exc
        try:
            source = SourceSpec.model_validate({"path": "attachment.csv", **body.source})
        except ValidationError as exc:
            raise HTTPException(422, exc.errors(include_url=False)) from exc
        bundle, _ = resolve(body.scenario, who)
        out = apply_attachment(content, source, domain=bundle.scenario.domain,
                               arrival_paths=body.arrival_paths)  # fmt: skip
        levers: dict[str, Any] = {}
        if body.lever and out.get("fitted"):
            levers[body.lever] = out["fitted"]["spec"]
        if out["data_quality"]["ok"] and (out["overrides"] or levers):
            ref = body.scenario.model_copy(
                update={
                    "overrides": {**body.scenario.overrides, **out["overrides"]},
                    "levers": {**body.scenario.levers, **levers},
                }
            )
            validated, info = resolve(ref, who)
            plugins.get(validated.scenario.domain).validate_scenario(validated)
            out["scenario"] = ref.model_dump(mode="json", exclude_defaults=True)
            out["config_hash"] = validated.config_hash
            out.update(info)
        return out

    # ------------------------------------------------------------------ compile & findings (M9)

    @app.post(f"{API}/compile")
    def compile_request(body: CompileRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        """NL -> draft scenario card; never runs anything (FR-SCG-02/03/07/09)."""
        from services.chat.compiler import ScenarioCompiler

        base, _ = resolve(body.scenario, who)
        plugin = plugins.get(base.scenario.domain)
        res = ScenarioCompiler(reasoning, store).compile(
            body.text, base, plugin, workspace=who.workspace, replications=body.replications
        )
        out = {"domain": plugin.name, **res.to_dict()}
        store.audit(
            "scenario.compile",
            user=who.user,
            workspace=who.workspace,
            target=base.scenario.id,
            spec_hash=base.config_hash,
            outcome="ok" if res.ok else "failed",
        )
        exp = res.compiled.experiment if res.compiled else None
        if exp is not None:
            p = jobs.prepare(exp, base, body.mode)
            out["confirmation"] = {
                "estimate": p.estimate.to_dict(),
                "fidelity": body.mode,
                "needs_confirmation": jobs.needs_confirmation(p),
                "confirmation_token": jobs.confirmation_token(p, who.workspace),
                "cached": store.cached_job(p.cache_key) is not None,
            }
            out["submit"] = {
                "experiment": exp.model_dump(mode="json"),
                "scenario": body.scenario.model_dump(mode="json", exclude_defaults=True),
                "mode": body.mode,
            }
        return out

    def findings_for(job: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
        from simcore.catalog import metric_catalog
        from simcore.compiler import extrapolated_variants
        from simcore.experiment.findings import narrate, synthesize_findings

        data = json.loads(artifacts.get_bytes(result_key(job, "summary.json")))
        spec, bundle, _ = JobService.load(job)
        plugin = plugins.get(bundle.scenario.domain)
        catalog = {m.id: m for m in metric_catalog(plugin).metrics}
        flags = extrapolated_variants(data, bundle, plugin)
        definition = plugin.domain_definition()
        own = [m.id for m in definition.metrics] if definition else []
        headline = list(spec.kpis) or [
            m for m in data["metrics"] if m in own and catalog[m].direction != "neutral"
        ]
        result = synthesize_findings(data, catalog, extrapolated=flags, headline=headline or None)
        return result, narrate(result, catalog), catalog

    @app.get(f"{API}/jobs/{{job_id}}/findings")
    def findings(job_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        """Deterministic ``Findings`` and the guarded narrative (FR-SCG-11, FR-CHAT-04)."""
        result, story, _ = findings_for(own_job(job_id, who))
        return {
            "findings": result.model_dump(mode="json"),
            "narrative": {"text": story.text, "template": story.template, "refs": story.refs},
        }

    # ------------------------------------------------------------------ chat (M10)

    chats: dict[str, Any] = {}

    def gateway(request: Request) -> Any:
        """Tools reach the API in-process with the caller's own credentials (FR-CHAT-01)."""
        from fastapi.testclient import TestClient

        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() in ("authorization", "x-workspace")
        }
        inner = TestClient(app, raise_server_exceptions=False, headers=headers)

        def call(method: str, path: str, body: Any, params: Any) -> tuple[int, Any]:
            r = inner.request(method, f"{API}{path}", json=body, params=params or None)
            try:
                data = r.json()
            except ValueError:
                data = {"detail": r.text}
            return r.status_code, data

        return call

    def own_chat(session_id: str, who: Caller, request: Request) -> Any:
        sess = chats.get(session_id)
        if sess is None or sess.state.workspace != who.workspace:
            raise HTTPException(404, f"chat session {session_id!r} not found")
        sess.api = gateway(request)
        return sess

    def save_chat(sess: Any, who: Caller) -> None:
        store.save_chat(sess.id, who.workspace, who.user, sess.to_payload())

    @app.post(f"{API}/chat/sessions", status_code=201)
    def chat_start(
        body: ChatStartRequest, request: Request, who: Caller = Depends(caller)
    ) -> dict[str, Any]:
        from services.chat.session import ChatSession

        sess = ChatSession(reasoning, gateway(request), workspace=who.workspace)
        if body.scenario is not None:
            bundle, _ = resolve(body.scenario, who)
            sess.state.scenario = body.scenario.model_dump(mode="json", exclude_defaults=True)
            sess.state.domain = bundle.scenario.domain
        chats[sess.id] = sess
        save_chat(sess, who)
        store.audit("chat.start", user=who.user, workspace=who.workspace, target=sess.id)
        return {"session_id": sess.id, "state": sess.state.to_dict()}

    @app.post(f"{API}/chat/sessions/{{session_id}}/messages")
    def chat_message(
        session_id: str, body: ChatMessageRequest, request: Request,
        who: Caller = Depends(caller),
    ) -> dict[str, Any]:  # fmt: skip
        sess = own_chat(session_id, who, request)
        turn = sess.send(body.text)
        save_chat(sess, who)
        return {**turn.to_dict(), "state": sess.state.to_dict()}

    @app.post(f"{API}/chat/sessions/{{session_id}}/confirm")
    def chat_confirm(
        session_id: str, body: ChatConfirmRequest, request: Request,
        who: Caller = Depends(caller),
    ) -> dict[str, Any]:  # fmt: skip
        """The user (not the model) approves a pending launch (FR-SCG-07)."""
        from services.chat.tools import ToolError

        sess = own_chat(session_id, who, request)
        try:
            job = sess.confirm(body.action_id)
        except ToolError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        save_chat(sess, who)
        store.audit("chat.confirm", user=who.user, workspace=who.workspace, target=job["id"])
        return {"job": job, "state": sess.state.to_dict()}

    @app.get(f"{API}/chat/sessions/{{session_id}}")
    def chat_get(session_id: str, request: Request, who: Caller = Depends(caller)) -> Any:
        sess = chats.get(session_id)
        if sess is not None and sess.state.workspace == who.workspace:
            return sess.to_payload()
        row = store.chat(session_id, who.workspace)
        if row is None:
            raise HTTPException(404, f"chat session {session_id!r} not found")
        return json.loads(row["payload_json"])

    # ------------------------------------------------------------------ replay (FR-UI-06, AC-UI-02)

    def load_log(job: dict[str, Any], variant: str, rep: int) -> Any:
        import tempfile

        from simcore.core.obs.eventlog import EventLog

        key = result_key(job, f"{variant}/rep_{rep:03d}/events.parquet")
        try:
            data = artifacts.get_bytes(key)
        except KeyError as exc:
            raise HTTPException(404, "event log not found") from exc
        with tempfile.NamedTemporaryFile(suffix=".parquet") as fh:
            fh.write(data)
            fh.flush()
            return EventLog.from_parquet(Path(fh.name))

    def nav_for(job: dict[str, Any], variant: str) -> Any:
        spec, bundle, _ = JobService.load(job)
        from simcore.experiment.design import build_variants

        for v in build_variants(spec, bundle):
            if v.id == variant and v.bundle is not None:
                bundle = v.bundle
        if bundle.facility is None:
            raise HTTPException(404, "this run has no facility")
        return NavGraph(bundle.facility), bundle

    @app.get(f"{API}/jobs/{{job_id}}/variants/{{variant}}/replications/{{rep}}/replay")
    def replay_data(
        job_id: str, variant: str, rep: int, who: Caller = Depends(caller)
    ) -> dict[str, Any]:
        """Facility + piecewise-linear trajectories rebuilt from the event log (FR-OBS-04)."""
        from simcore.core.obs.replay import trajectories_from_log

        job = own_job(job_id, who)
        log = load_log(job, variant, rep)
        nav, bundle = nav_for(job, variant)
        store_ = trajectories_from_log(log, nav)
        zones: dict[str, list[str]] = {}
        for e in nav.edges.values():
            if e.zone:
                zones.setdefault(e.zone, []).extend([e.source, e.target])
        return {
            "duration": bundle.scenario.duration,
            "nodes": {n: list(p) for n, p in nav.positions.items()},
            "edges": [
                {"id": e.id, "source": e.source, "target": e.target, "zone": e.zone}
                for e in nav.edges.values()
            ],
            "zones": [{"id": z, "nodes": sorted(set(ns))} for z, ns in sorted(zones.items())],
            "agents": {
                a: {
                    "spawn": list(sp) if (sp := store_.spawn[a]) is not None else None,
                    "segments": [
                        [sg.start_time, sg.end_time, sg.start[0], sg.start[1], sg.end[0],
                         sg.end[1]]
                        for sg in store_.segments[a]
                    ],
                }
                for a in sorted(store_.spawn)
            },
        }  # fmt: skip

    @app.get(f"{API}/jobs/{{job_id}}/variants/{{variant}}/replications/{{rep}}/snapshot")
    def replay_snapshot(
        job_id: str, variant: str, rep: int, t: float, who: Caller = Depends(caller)
    ) -> dict[str, Any]:
        """Agent positions and states at sim-time ``t`` computed server-side from the log."""
        from simcore.core.obs.replay import snapshot

        job = own_job(job_id, who)
        nav, _ = nav_for(job, variant)
        snap = snapshot(load_log(job, variant, rep), nav, t)
        return {
            "t": t,
            "agents": {
                a: {**v, "position": list(v["position"]) if v["position"] is not None else None}
                for a, v in snap.items()
            },
        }

    @app.post(f"{API}/scenarios/export")
    def export_scenario(ref: ScenarioRef, who: Caller = Depends(caller)) -> Response:
        """Scenario YAML with levers/overrides applied (FR-UI-09)."""
        import yaml

        bundle, _ = resolve(ref, who)
        doc = bundle.scenario.model_dump(mode="json", by_alias=True, exclude_none=True)
        body = yaml.safe_dump(doc, sort_keys=False)
        return Response(
            body,
            media_type="application/x-yaml",
            headers={"Content-Disposition": f'attachment; filename="{bundle.scenario.id}.yaml"'},
        )

    # ------------------------------------------------------------------ semantic layer (M8)

    engines: dict[str, Any] = {}

    def engine_for(job: dict[str, Any]) -> Any:
        from services.semantic.query import SemanticEngine
        from services.semantic.sources import ExperimentData

        prefix = result_key(job, "").rstrip("/")
        if prefix not in engines:
            root = artifacts.local_dir(prefix)
            if root is None:  # pragma: no cover - remote store: materialise locally
                import tempfile

                root = Path(tempfile.mkdtemp(prefix="simcore-sem-"))
                for key in artifacts.keys(prefix):
                    dest = root / key[len(prefix) + 1 :]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(artifacts.get_bytes(key))
            _, bundle, _ = JobService.load(job)
            engines[prefix] = (
                SemanticEngine(
                    ExperimentData(root), plugins.get(bundle.scenario.domain), {"job_id": job["id"]}
                ),
                bundle,
            )
            while len(engines) > 32:
                engines.pop(next(iter(engines)))
        return engines[prefix]

    def query_error(exc: Exception) -> HTTPException:
        issues = [i.to_dict() for i in getattr(exc, "issues", [])]
        return HTTPException(
            422,
            {"error": "invalid_query", "repairable": True, "issues": issues, "message": str(exc)},
        )

    @app.post(f"{API}/query")
    def run_query(body: QueryRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        from services.semantic.query import QueryError

        engine, _ = engine_for(own_job(body.job_id, who))
        try:
            result: dict[str, Any] = engine.query(body.query).to_dict()
        except (QueryError, ValueError) as exc:
            raise query_error(exc) from exc
        return result

    @app.get(f"{API}/charts/templates")
    def chart_templates() -> dict[str, str]:
        from services.semantic.charts import TEMPLATE_RULES

        return TEMPLATE_RULES

    def chart_context(body: ChartRequest, who: Caller) -> tuple[Any, Any, dict[str, Any]]:
        from services.semantic.charts import ChartValidationError, validate_chart

        if body.job_id is not None:
            engine, bundle = engine_for(own_job(body.job_id, who))
            catalog = engine.catalog
        elif body.domain is not None:
            engine, bundle = None, None
            catalog = {m.id: m for m in metric_catalog(plugins.get(body.domain)).metrics}
        else:
            raise HTTPException(422, "give job_id or domain")
        try:
            chart = validate_chart(
                body.chart,
                catalog,
                user_requested_no_uncertainty=body.user_requested_no_uncertainty,
            )
        except ChartValidationError as exc:
            raise HTTPException(422, exc.to_dict()) from exc
        return chart, engine, {"bundle": bundle}

    @app.post(f"{API}/charts/validate")
    def chart_validate(body: ChartRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        chart, _, _ = chart_context(body, who)
        return {"valid": True, "chart": chart.model_dump(mode="json")}

    def render(body: ChartRequest, who: Caller) -> tuple[dict[str, Any], Any]:
        from services.semantic.charts import render_chart
        from services.semantic.query import QueryError

        chart, engine, ctx = chart_context(body, who)
        if engine is None:
            raise HTTPException(422, "rendering needs job_id (the data source)")
        try:
            result = engine.query(chart.query)
        except (QueryError, ValueError) as exc:
            raise query_error(exc) from exc
        facility = None
        if chart.template == "floorplan_overlay":
            bundle = ctx["bundle"]
            if bundle is None or bundle.facility is None:
                raise HTTPException(422, "this run has no facility for a floor plan")
            nav = NavGraph(bundle.facility)
            facility = {
                "manifest": bundle.facility.model_dump(mode="json", by_alias=True),
                "nodes": {n: list(p) for n, p in nav.positions.items()},
                "edges": [
                    {"id": e.id, "source": e.source, "target": e.target} for e in nav.edges.values()
                ],
            }
        return render_chart(chart, result, facility), result

    @app.post(f"{API}/charts/render")
    def chart_render(body: ChartRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        """Vega-Lite + 'show your work': query spec, data table and provenance (FR-VIZ-04)."""
        vega, result = render(body, who)
        return {"ref_id": result.ref_id, "vega_lite": vega, "query": result.to_dict()}

    @app.post(f"{API}/charts/export")
    def chart_export(body: ChartRequest, who: Caller = Depends(caller)) -> Response:
        vega, result = render(body, who)
        fmt = body.format
        if fmt == "csv":
            return Response(result.to_csv(), media_type="text/csv")
        if fmt in ("json", "vega"):
            payload = (
                vega
                if fmt == "vega"
                else {"chart": body.chart, "ref_id": result.ref_id, "provenance": result.provenance}
            )
            return Response(json.dumps(payload, default=str), media_type="application/json")
        import vl_convert as vlc

        if fmt == "svg":
            return Response(vlc.vegalite_to_svg(vega), media_type="image/svg+xml")
        return Response(vlc.vegalite_to_png(vega), media_type="image/png")

    @app.post(f"{API}/sql")
    def sql(body: SqlRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        from services.semantic.sources import ExperimentData
        from services.semantic.sql import SqlRejected, run_readonly_sql

        if not settings.sql_enabled:
            raise HTTPException(403, "advanced SQL mode is disabled (SIMCORE_SQL_ENABLED)")
        job = own_job(body.job_id, who)
        root = artifacts.local_dir(result_key(job, "").rstrip("/"))
        if root is None:  # pragma: no cover
            raise HTTPException(501, "SQL mode needs a local artifact store")
        try:  # a private, locked-down database per request
            out = run_readonly_sql(ExperimentData(root), body.sql, body.max_rows)
        except SqlRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        store.audit(
            "sql",
            user=who.user,
            workspace=who.workspace,
            target=job["id"],
            detail={"sql": body.sql[:500]},
        )
        return out

    # ------------------------------------------------------------------ analyses & audit

    @app.get(f"{API}/analyses")
    def list_analyses(who: Caller = Depends(caller)) -> list[dict[str, Any]]:
        return store.analyses(who.workspace)

    @app.post(f"{API}/analyses", status_code=201)
    def save_analysis(body: AnalysisRequest, who: Caller = Depends(caller)) -> dict[str, Any]:
        aid = store.save_analysis(who.workspace, body.name, body.payload, body.id, who.user)
        store.audit("analysis.save", user=who.user, workspace=who.workspace, target=aid)
        return {"id": aid}

    @app.get(f"{API}/analyses/{{analysis_id}}")
    def get_analysis(analysis_id: str, who: Caller = Depends(caller)) -> dict[str, Any]:
        row = store.analysis(who.workspace, analysis_id)
        if row is None:
            raise HTTPException(404, f"analysis {analysis_id!r} not found")
        return {
            **{k: v for k, v in row.items() if k != "payload_json"},
            "payload": json.loads(row["payload_json"]),
        }

    @app.get(f"{API}/audit")
    def audit(limit: int = Query(200, le=2000), who: Caller = Depends(caller)) -> list[Any]:
        return store.audit_log(who.workspace, limit)

    if settings.frontend_dir is not None and (settings.frontend_dir / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="spa")

    return app


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in job.items() if k not in ("spec_json", "estimate_json")}
    view["estimate"] = json.loads(job["estimate_json"]) if job.get("estimate_json") else None
    view["cached"] = job.get("cached_from") is not None
    return view


# ----------------------------------------------------------------------------- middleware


def _install_errors(app: FastAPI) -> None:
    @app.exception_handler(ConfigError)
    async def config_error(request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "config_error",
                "issues": [
                    {
                        "source": i.source,
                        "location": i.location,
                        "message": i.message,
                        "hint": i.hint,
                    }
                    for i in exc.issues
                ],
            },
        )

    @app.exception_handler(PluginError)
    async def plugin_error(request: Request, exc: PluginError) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"error": "unknown_domain", "message": str(exc)}
        )

    @app.exception_handler(QuotaExceeded)
    async def quota(request: Request, exc: QuotaExceeded) -> JSONResponse:
        return JSONResponse(status_code=429, content={"error": "quota", "message": str(exc)})

    @app.exception_handler(SubmissionRejected)
    async def rejected(request: Request, exc: SubmissionRejected) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"error": "confirmation_required", "message": str(exc)}
        )

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid", "message": str(exc)})


def _install_rate_limit(app: FastAPI, settings: Settings) -> None:
    """Per-client token bucket on mutating requests (NFR-14)."""
    buckets: dict[str, tuple[float, float]] = defaultdict(
        lambda: (float(settings.rate_limit_per_minute), time.monotonic())
    )

    @app.middleware("http")
    async def limit(request: Request, call_next: Any) -> Response:
        if request.method in ("POST", "PUT", "DELETE"):
            key = request.headers.get("authorization") or (
                request.client.host if request.client else "anon"
            )
            tokens, last = buckets[key]
            t = time.monotonic()
            tokens = min(
                settings.rate_limit_per_minute,
                tokens + (t - last) * settings.rate_limit_per_minute / 60.0,
            )
            if tokens < 1:
                return JSONResponse(
                    status_code=429,
                    content={"error": "rate_limited", "message": "too many requests"},
                )
            buckets[key] = (tokens - 1, t)
        request.state.request_id = uuid.uuid4().hex[:12]
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response
