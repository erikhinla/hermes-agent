"""Required pipeline guarantees: lock, idempotency, verify, e2e draft, secret."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import SECRET_HEADERS, approve_payload, reject_payload


def test_health(harness):
    response = harness.client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


def test_webhook_secret_mismatch_403(harness):
    created = harness.client.post("/briefs", json={"brief": "handoffs still need a chaperone"}).json()
    response = harness.client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        json=approve_payload(1, created["draft_id"], created["version"]),
    )
    assert response.status_code == 403
    draft = harness.client.get(f"/drafts/{created['draft_id']}").json()
    assert draft["status"] == "awaiting_approval"


def test_webhook_idempotency_duplicate_update_id(harness):
    created = harness.client.post(
        "/briefs", json={"brief": "more tools, same pile", "telegram_chat_id": "42"}
    ).json()
    payload = approve_payload(777, created["draft_id"], created["version"])
    first = harness.client.post("/telegram/webhook", headers=SECRET_HEADERS, json=payload)
    second = harness.client.post("/telegram/webhook", headers=SECRET_HEADERS, json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json().get("idempotent") is True
    assert first.json().get("claimed") is True
    assert len(harness.postiz.creates) == 1


def test_mechanical_failure_skips_postiz(harness):
    harness.venice.set_fail_mode("bad_bytes")
    created = harness.client.post("/briefs", json={"brief": "which version is real"}).json()
    response = harness.client.post(
        "/telegram/webhook",
        headers=SECRET_HEADERS,
        json=approve_payload(12, created["draft_id"], created["version"]),
    )
    assert response.status_code == 200
    draft = harness.client.get(f"/drafts/{created['draft_id']}").json()
    assert draft["status"] == "failed"
    assert draft["last_error"]
    assert harness.postiz.called is False
    assert harness.postiz.creates == []
    assert harness.postiz.uploads == []


def test_mechanical_failure_wrong_size_skips_postiz(harness):
    harness.venice.set_fail_mode("bad_size")
    created = harness.client.post("/briefs", json={"brief": "tools faster, work foggier"}).json()
    harness.client.post(
        "/telegram/webhook",
        headers=SECRET_HEADERS,
        json=approve_payload(13, created["draft_id"], created["version"]),
    )
    draft = harness.client.get(f"/drafts/{created['draft_id']}").json()
    assert draft["status"] == "failed"
    assert "below required" in (draft["last_error"] or "")
    assert harness.postiz.called is False


def test_e2e_brief_feedback_revise_approve_postiz_draft(harness):
    created = harness.client.post(
        "/briefs",
        json={"brief": "agents ping you when the tools don't agree", "telegram_chat_id": "42"},
    )
    assert created.status_code == 200
    body = created.json()
    draft_id = body["draft_id"]
    assert body["status"] == "awaiting_approval"
    assert body["copy"]["linkedin"]
    assert "transformby10x.ai" in body["copy"]["linkedin"]
    assert harness.telegram.messages

    reject = harness.client.post(
        "/telegram/webhook",
        headers=SECRET_HEADERS,
        json=reject_payload(20, draft_id, body["version"], "tighter open, keep the CTA"),
    )
    assert reject.status_code == 200
    revised = harness.client.get(f"/drafts/{draft_id}").json()
    assert revised["status"] == "awaiting_approval"
    assert revised["version"] == body["version"] + 1
    assert any(call.mode == "revise" for call in harness.grok.calls)
    revise_call = [call for call in harness.grok.calls if call.mode == "revise"][-1]
    assert revise_call.brief == "agents ping you when the tools don't agree"
    assert revise_call.rejected_copy is not None
    assert "tighter open" in (revise_call.feedback_text or "")
    assert "After your note" in revised["current_copy"]["linkedin"]

    approve = harness.client.post(
        "/telegram/webhook",
        headers=SECRET_HEADERS,
        json=approve_payload(21, draft_id, revised["version"]),
    )
    assert approve.status_code == 200
    staged = harness.client.get(f"/drafts/{draft_id}").json()
    assert staged["status"] == "staged"
    assert harness.postiz.called is True
    last = harness.postiz.last_create
    assert last is not None
    assert last.status == "draft"
    assert last.type == "draft"
    wire = last.wire_payload()
    assert wire["type"] == "draft"
    assert "schedule" not in wire
    assert "publish" not in wire
    assert wire.get("status") in (None, "draft")


def test_optimistic_lock_two_concurrent_approves(harness):
    created = harness.client.post("/briefs", json={"brief": "the handoff still needs a chaperone"}).json()
    draft_id = created["draft_id"]
    version = created["version"]

    def approve(update_id: int):
        return harness.client.post(
            "/telegram/webhook",
            headers=SECRET_HEADERS,
            json=approve_payload(update_id, draft_id, version),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve, 301), pool.submit(approve, 302)]
        results = [f.result() for f in futures]

    assert all(r.status_code == 200 for r in results)
    claimed = [r.json().get("claimed") for r in results]
    assert claimed.count(True) == 1
    assert claimed.count(False) == 1
    draft = harness.client.get(f"/drafts/{draft_id}").json()
    assert draft["status"] in ("processing_assets", "staged")
    assert len(harness.postiz.creates) == 1


@pytest.mark.asyncio
async def test_optimistic_lock_async_gather(harness):
    created = harness.client.post("/briefs", json={"brief": "one person is out, work waits"}).json()
    draft_id = created["draft_id"]
    version = created["version"]
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1, r2 = await asyncio.gather(
            client.post(
                "/telegram/webhook",
                headers=SECRET_HEADERS,
                json=approve_payload(401, draft_id, version),
            ),
            client.post(
                "/telegram/webhook",
                headers=SECRET_HEADERS,
                json=approve_payload(402, draft_id, version),
            ),
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    claimed = [r1.json().get("claimed"), r2.json().get("claimed")]
    assert claimed.count(True) == 1
    assert claimed.count(False) == 1
    draft = harness.client.get(f"/drafts/{draft_id}").json()
    assert draft["status"] in ("processing_assets", "staged")
