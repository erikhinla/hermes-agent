"""FLOW dashboard envelope: submit, review copy, approve draft, block, SPA."""

from __future__ import annotations

from uuid import UUID


def _submit(harness, title="Agents still ping you", goal="Name the extra job the tools assigned"):
    return harness.client.post(
        "/api/flow/submit",
        json={"title": title, "goal": goal, "risk_tier": "reputation", "source": "landing_page"},
    )


def test_v1_health(harness):
    response = harness.client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_submit_envelope_lists_as_review_required_with_copy(harness):
    created = _submit(harness)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "accepted"
    task = body["task"]
    UUID(task["task_id"])
    assert task["status"] == "review_required"
    assert task["review_required"] is True
    assert task["task_type"] == "content_prep"
    assert task["risk_tier"] == "reputation"
    assert task["owner_role"] == "alpha"
    assert task["source"] == "landing_page"
    assert task["copy"]
    assert "transformby10x.ai" in task["copy"]["linkedin"]
    assert task["review_artifacts_ready"] is True
    assert "linkedin" in task["review_artifacts"]

    listed = harness.client.get("/api/flow/tasks")
    assert listed.status_code == 200
    tasks = listed.json()["tasks"]
    match = next(item for item in tasks if item["task_id"] == task["task_id"])
    assert match["status"] == "review_required"
    assert match["copy"]["linkedin"]

    v1 = harness.client.get(f"/v1/flow/tasks/{task['task_id']}")
    assert v1.status_code == 200
    assert v1.json()["copy"]["quote_line"]
    assert v1.json()["status"] == "review_required"

    pending = harness.client.get("/api/flow/tasks?queue=pending")
    assert any(item["task_id"] == task["task_id"] for item in pending.json()["tasks"])


def test_approve_stages_postiz_draft(harness):
    created = _submit(harness, title="Handoffs still need a chaperone", goal="Keep one person from babysitting the tools")
    task_id = created.json()["task"]["task_id"]
    draft_id = created.json()["task"]["draft_id"]
    approved = harness.client.post(
        "/v1/flow/approve", json={"task_id": task_id, "actor": "landing_page"}
    )
    assert approved.status_code == 200, approved.text
    draft = harness.client.get(f"/drafts/{draft_id}").json()
    assert draft["status"] == "staged"
    assert harness.postiz.called is True
    last = harness.postiz.last_create
    assert last is not None
    assert last.status == "draft"
    assert last.type == "draft"
    wire = last.wire_payload()
    assert wire["type"] == "draft"
    assert "schedule" not in wire
    detail = harness.client.get(f"/api/flow/tasks/{task_id}").json()
    assert detail["status"] == "completed"
    assert detail["media_urls"]


def test_block_does_not_call_postiz(harness):
    created = _submit(harness, title="Tools faster work foggier", goal="Do not invent a new offer in this brief")
    task_id = created.json()["task"]["task_id"]
    blocked = harness.client.post(
        "/api/flow/block",
        json={"task_id": task_id, "reason": "off brand, no invented offer", "actor": "landing_page"},
    )
    assert blocked.status_code == 200, blocked.text
    task = blocked.json()["task"]
    assert task["status"] == "blocked"
    assert "invented offer" in (task.get("block_reason") or "")
    assert harness.postiz.called is False
    assert harness.postiz.creates == []
    assert harness.postiz.uploads == []
    listed = harness.client.get("/api/flow/tasks?queue=blocked").json()["tasks"]
    assert any(item["task_id"] == task_id for item in listed)


def test_dashboard_flow_control_returns_html(harness):
    response = harness.client.get("/flow-control")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert "FLOW Agent AS Dashboard" in body
    assert "/assets/index-9dd493e3.js" in body
    assets = harness.client.get("/assets/index-9dd493e3.js")
    assert assets.status_code == 200
    assert "api/flow" in assets.text


def test_submit_validation(harness):
    short = harness.client.post(
        "/api/flow/submit",
        json={"title": "Hey", "goal": "too short", "risk_tier": "reputation"},
    )
    assert short.status_code == 422
    bad_tier = harness.client.post(
        "/v1/flow/submit",
        json={
            "title": "A real enough title",
            "goal": "Observable goal that is long enough",
            "risk_tier": "money",
        },
    )
    assert bad_tier.status_code == 400
