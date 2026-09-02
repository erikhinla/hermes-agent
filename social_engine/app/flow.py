"""FLOW Control API for the vendored Agent AS dashboard.

Mounted at /v1/flow and /api/flow (nginx used to proxy /api → bizbrain /v1).
Social briefs always review_required; Postiz stays draft; CTA is locked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import parse_copy
from app.models import CTA_LINE

RISK_ROUTING = {
    "reputation": "alpha",
    "time_loss": "beta",
    "downtime_security_money": "gamma",
}
VALID_SOURCES = ("landing_page", "manual", "telegram", "api", "discord")
QUEUE_NAMES = ("pending", "active", "completed", "escalated", "blocked")


class FlowControlError(ValueError):
    pass


class SubmitRequest(BaseModel):
    title: str = Field(..., min_length=5, max_length=200)
    goal: str = Field(..., min_length=10, max_length=2000)
    risk_tier: str
    owner_role: Optional[str] = None
    source: str = "landing_page"
    inputs: dict[str, Any] = Field(default_factory=dict)
    output_required: Optional[str] = None


class ApprovalRequest(BaseModel):
    task_id: str
    actor: str = "landing_page"


class BlockRequest(BaseModel):
    task_id: str
    reason: str = Field(..., min_length=3, max_length=500)
    actor: str = "landing_page"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_error(exc: FlowControlError, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))


def _brief_from_submit(request: SubmitRequest) -> str:
    parts = [request.title.strip(), request.goal.strip()]
    notes = request.inputs.get("notes") if isinstance(request.inputs, dict) else None
    if notes:
        parts.append(str(notes).strip())
    return "\n\n".join(p for p in parts if p)


def derive_status(task: dict[str, Any], draft: dict[str, Any]) -> tuple[str, str]:
    if task.get("envelope_status") == "blocked" or task.get("block_reason"):
        return "blocked", "blocked"
    draft_status = draft.get("status") or ""
    if draft_status in ("awaiting_approval",):
        return "review_required", "pending"
    if draft_status in ("processing_assets", "approved"):
        return "active", "active"
    if draft_status == "staged":
        return "completed", "completed"
    if draft_status == "failed":
        return "failed", "blocked"
    if draft_status in ("pending_copy", "revising"):
        return "pending", "pending"
    return str(task.get("envelope_status") or "pending"), str(task.get("queue") or "pending")


def serialize_task(
    task: dict[str, Any],
    draft: dict[str, Any],
    media: Optional[list[dict[str, Any]]] = None,
    audit: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    copy = parse_copy(draft.get("current_copy_json") or draft.get("approved_copy_json"))
    media = media or []
    media_urls = {row["platform"]: row["image_url"] for row in media if row.get("image_url")}
    status, queue = derive_status(task, draft)
    artifacts: dict[str, str] = {}
    copy_payload = None
    if copy:
        copy_payload = copy.model_dump()
        artifacts["linkedin"] = copy.linkedin
        artifacts["instagram"] = copy.instagram
        artifacts["facebook"] = copy.facebook
        artifacts["reddit"] = copy.reddit
        artifacts["x"] = copy.x_text()
        if copy.quote_line:
            artifacts["quote_line"] = copy.quote_line
        artifacts["cta"] = CTA_LINE
        if copy.visual_prompt:
            artifacts["visual_prompt"] = copy.visual_prompt
    for platform, url in media_urls.items():
        artifacts[f"{platform}_media"] = url

    review_required = bool(task.get("review_required", 1))
    return {
        "task_id": task["task_id"],
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at") or draft.get("updated_at"),
        "source": task.get("source") or "landing_page",
        "title": task["title"],
        "goal": task["goal"],
        "task_type": task.get("task_type") or "content_prep",
        "risk_tier": task["risk_tier"],
        "owner_role": task["owner_role"],
        "preferred_owner": task.get("preferred_owner") or task["owner_role"],
        "output_required": task.get("output_required"),
        "review_required": review_required,
        "status": status,
        "queue": queue,
        "inputs": task.get("inputs") or {},
        "copy": copy_payload,
        "media_urls": media_urls,
        "review_artifacts": artifacts,
        "review_artifacts_ready": bool(copy),
        "artifact_path": artifacts.get("quote_line") or artifacts.get("linkedin"),
        "audit": audit if audit is not None else (task.get("audit") or []),
        "draft_id": task.get("draft_id") or draft.get("draft_id"),
        "version": draft.get("version"),
        "draft_status": draft.get("status"),
        "block_reason": task.get("block_reason"),
        "last_error": draft.get("last_error"),
    }


def validate_submit(request: SubmitRequest) -> dict[str, Any]:
    if request.source not in VALID_SOURCES:
        raise FlowControlError(
            f"Invalid source. Expected one of: {', '.join(VALID_SOURCES)}"
        )
    expected_owner = RISK_ROUTING.get(request.risk_tier)
    if expected_owner is None:
        raise FlowControlError(
            "Invalid risk_tier. Expected one of: reputation, time_loss, downtime_security_money"
        )
    owner_role = request.owner_role or expected_owner
    if owner_role != expected_owner:
        raise FlowControlError(
            f"Invalid routing combination: risk_tier={request.risk_tier} requires owner_role={expected_owner}"
        )
    output = request.output_required or (
        "Postiz draft only (never scheduled or published). "
        "Locked CTA: Start Here → https://transformby10x.ai/"
    )
    return {
        "source": request.source,
        "title": request.title.strip(),
        "goal": request.goal.strip(),
        "task_type": "content_prep",
        "risk_tier": request.risk_tier,
        "owner_role": owner_role,
        "preferred_owner": owner_role,
        "output_required": output,
        "review_required": True,
        "inputs": request.inputs or {},
    }


def runtime_status(settings, store) -> dict[str, Any]:
    fake = settings.use_fake_clients()
    grok_ok = fake or bool(settings.grok_key() or settings.openrouter_api_key)
    venice_ok = fake or bool(settings.venice_api_key)
    postiz_ok = fake or bool(settings.postiz_api_key)
    agents = {
        "alpha": {
            "name": "Alpha (Grok copy)",
            "port": 8088,
            "port_open": True,
            "runtime_registered": grok_ok,
            "healthy": grok_ok,
        },
        "beta": {
            "name": "Beta (Venice stills)",
            "port": 8088,
            "port_open": True,
            "runtime_registered": venice_ok,
            "healthy": venice_ok,
        },
        "gamma": {
            "name": "Gamma (Postiz draft)",
            "port": 8088,
            "port_open": True,
            "runtime_registered": postiz_ok,
            "healthy": postiz_ok,
        },
    }
    counts = {name: 0 for name in QUEUE_NAMES}
    for task, draft in store.list_flow_task_rows():
        _status, queue = derive_status(task, draft)
        if queue in counts:
            counts[queue] += 1
    return {
        "timestamp": utc_now(),
        "state_root": str(settings.db_path()),
        "agents": agents,
        "queues": counts,
        "healthy": all(agent["healthy"] for agent in agents.values()),
        "engine": "social",
        "publish": False,
    }


def _hydrate(store, task: dict[str, Any]) -> dict[str, Any]:
    draft = store.get_draft(task["draft_id"])
    if not draft:
        raise FlowControlError(f"Task not found: {task['task_id']}")
    media = store.list_media(task["draft_id"])
    return serialize_task(task, draft, media, audit=task.get("audit") or [])


def build_flow_router() -> APIRouter:
    router = APIRouter(tags=["flow-control"])

    def _store(request: Request):
        return request.app.state.db

    def _pipe(request: Request):
        return request.app.state.pipeline

    def _settings(request: Request):
        return request.app.state.settings

    @router.get("/health")
    async def flow_health() -> dict[str, Any]:
        return {"status": "healthy", "engine": "social", "timestamp": utc_now()}

    @router.get("/status")
    async def flow_status(request: Request) -> dict[str, Any]:
        return runtime_status(_settings(request), _store(request))

    @router.get("/tasks")
    async def flow_tasks(request: Request, queue: Optional[str] = None) -> dict[str, Any]:
        store = _store(request)
        tasks = []
        for task, draft in store.list_flow_task_rows():
            media = store.list_media(task["draft_id"])
            item = serialize_task(task, draft, media, audit=task.get("audit") or [])
            if queue and item["queue"] != queue:
                continue
            tasks.append(item)
        return {"tasks": tasks}

    @router.get("/tasks/{task_id}")
    async def flow_task(task_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        task = store.get_flow_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        try:
            return _hydrate(store, task)
        except FlowControlError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.post("/submit")
    async def flow_submit(body: SubmitRequest, request: Request) -> dict[str, Any]:
        store = _store(request)
        pipe = _pipe(request)
        try:
            envelope = validate_submit(body)
        except FlowControlError as exc:
            raise _http_error(exc)
        task_id = str(uuid4())
        brief = _brief_from_submit(body)
        row = await pipe.create_from_brief(brief, telegram_chat_id=None, draft_id=task_id)
        now = utc_now()
        store.create_flow_task(
            task_id=task_id,
            draft_id=row["draft_id"],
            title=envelope["title"],
            goal=envelope["goal"],
            source=envelope["source"],
            task_type=envelope["task_type"],
            risk_tier=envelope["risk_tier"],
            owner_role=envelope["owner_role"],
            preferred_owner=envelope["preferred_owner"],
            output_required=envelope["output_required"],
            review_required=True,
            envelope_status="review_required",
            queue="pending",
            inputs=envelope["inputs"],
            created_at=now,
        )
        store.append_flow_audit(
            task_id,
            action="task_submitted",
            actor=body.source,
            details={"risk_tier": envelope["risk_tier"], "owner_role": envelope["owner_role"]},
        )
        task = store.get_flow_task(task_id)
        return {"status": "accepted", "task": _hydrate(store, task)}

    @router.post("/approve")
    async def flow_approve(
        body: ApprovalRequest, request: Request, background: BackgroundTasks
    ) -> dict[str, Any]:
        store = _store(request)
        pipe = _pipe(request)
        task = store.get_flow_task(body.task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task not found: {body.task_id}")
        if task.get("envelope_status") == "blocked" or task.get("block_reason"):
            raise HTTPException(status_code=400, detail="Task is blocked")
        draft = store.get_draft(task["draft_id"])
        if not draft:
            raise HTTPException(status_code=404, detail=f"Task not found: {body.task_id}")
        if draft["status"] != "awaiting_approval":
            raise HTTPException(status_code=400, detail="Task is not awaiting approval")
        claimed = store.claim_approval(task["draft_id"], draft["version"])
        if not claimed:
            raise HTTPException(
                status_code=400, detail="Task is not awaiting approval or was already claimed"
            )
        store.update_flow_task(body.task_id, envelope_status="active", queue="active")
        store.append_flow_audit(
            body.task_id,
            action="approved",
            actor=body.actor,
            details={"approval": "explicit", "version": draft["version"]},
        )
        background.add_task(pipe.process_assets, task["draft_id"])
        task = store.get_flow_task(body.task_id)
        return {"status": "approved", "task": _hydrate(store, task)}

    @router.post("/block")
    async def flow_block(body: BlockRequest, request: Request) -> dict[str, Any]:
        store = _store(request)
        task = store.get_flow_task(body.task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task not found: {body.task_id}")
        draft = store.get_draft(task["draft_id"])
        if not draft:
            raise HTTPException(status_code=404, detail=f"Task not found: {body.task_id}")
        if draft["status"] not in ("awaiting_approval", "pending_copy", "revising"):
            raise HTTPException(status_code=400, detail="Task can no longer be blocked")
        store.mark_failed(task["draft_id"], f"blocked: {body.reason}")
        store.update_flow_task(
            body.task_id,
            envelope_status="blocked",
            queue="blocked",
            block_reason=body.reason,
        )
        store.append_flow_audit(
            body.task_id,
            action="blocked",
            actor=body.actor,
            details={"reason": body.reason},
        )
        task = store.get_flow_task(body.task_id)
        return {"status": "blocked", "task": _hydrate(store, task)}

    return router


def bizbrain_stub_payloads() -> dict[str, Any]:
    now = utc_now()
    return {
        "health": {"status": "healthy", "timestamp": now, "version": "social-engine"},
        "queues": {
            "timestamp": now,
            "queues": {"openclaw": 0, "hermes": 0, "agent_zero": 0},
            "total": 0,
        },
        "performance": {
            "success_rate": 0,
            "avg_execution_time": 0,
            "total_jobs": 0,
            "performance_metrics": [],
            "recommendations": [],
        },
        "skills": {
            "total_active_skills": 0,
            "high_confidence_skills": 0,
            "frequently_used_skills": 0,
            "top_skills": [],
            "skill_distribution": {
                "high_confidence": 0,
                "medium_confidence": 0,
                "low_confidence": 0,
            },
        },
    }
