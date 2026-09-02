"""SQLite WAL store for drafts, revisions, webhook idempotency, and media."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from app.models import VALID_STATUSES, CopyBundle

DDL = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    brief TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending_copy',
            'awaiting_approval',
            'revising',
            'approved',
            'processing_assets',
            'staged',
            'failed'
        )
    ),
    version INTEGER NOT NULL DEFAULT 1,
    current_copy_json TEXT,
    approved_copy_json TEXT,
    telegram_chat_id TEXT,
    telegram_last_message_id TEXT,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    video_first INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    version_number INTEGER NOT NULL,
    copy_json TEXT NOT NULL,
    feedback_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS webhook_idempotency (
    update_id TEXT PRIMARY KEY,
    draft_id TEXT,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS media_assets (
    asset_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    platform TEXT NOT NULL,
    image_url TEXT NOT NULL,
    postiz_media_id TEXT,
    width INTEGER,
    height INTEGER,
    verified BOOLEAN NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS flow_tasks (
    task_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'landing_page',
    task_type TEXT NOT NULL DEFAULT 'content_prep',
    risk_tier TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    preferred_owner TEXT NOT NULL,
    output_required TEXT,
    review_required INTEGER NOT NULL DEFAULT 1,
    envelope_status TEXT NOT NULL,
    queue TEXT NOT NULL,
    inputs_json TEXT,
    block_reason TEXT,
    audit_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_local = threading.local()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = str(db_path)
    uri = path.startswith("file:")
    conn = sqlite3.connect(
        path,
        timeout=5.0,
        isolation_level=None,
        check_same_thread=False,
        uri=uri,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: str | Path) -> None:
    parent = Path(str(db_path)).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(DDL)
    finally:
        conn.close()


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        init_db(self.db_path)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self.db_path)
        try:
            yield connection
        finally:
            connection.close()

    def create_draft(
        self,
        draft_id: str,
        brief: str,
        telegram_chat_id: Optional[str] = None,
        video_first: int = 1,
    ) -> dict[str, Any]:
        with self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO drafts (draft_id, brief, status, version, telegram_chat_id, video_first)
                VALUES (?, ?, 'pending_copy', 1, ?, ?)
                """,
                (draft_id, brief, telegram_chat_id, video_first),
            )
            conn.execute("COMMIT")
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> Optional[dict[str, Any]]:
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
            return dict(row) if row else None

    def latest_awaiting_for_chat(self, chat_id: str) -> Optional[dict[str, Any]]:
        with self.conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM drafts
                WHERE telegram_chat_id = ? AND status = 'awaiting_approval'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            return dict(row) if row else None

    def save_copy(
        self,
        draft_id: str,
        copy: CopyBundle,
        status: str,
        bump_version: bool = False,
    ) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status}")
        payload = copy.model_dump_json()
        with self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if bump_version:
                conn.execute(
                    """
                    UPDATE drafts
                    SET current_copy_json = ?, status = ?, version = version + 1,
                        last_error = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE draft_id = ?
                    """,
                    (payload, status, draft_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE drafts
                    SET current_copy_json = ?, status = ?, last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE draft_id = ?
                    """,
                    (payload, status, draft_id),
                )
            conn.execute("COMMIT")
        return self.get_draft(draft_id)

    def set_status(
        self,
        draft_id: str,
        status: str,
        last_error: Optional[str] = None,
        increment_retry: bool = False,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status}")
        with self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if increment_retry:
                conn.execute(
                    """
                    UPDATE drafts
                    SET status = ?, last_error = ?, retry_count = retry_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE draft_id = ?
                    """,
                    (status, last_error, draft_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE drafts
                    SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE draft_id = ?
                    """,
                    (status, last_error, draft_id),
                )
            conn.execute("COMMIT")

    def set_telegram_message(self, draft_id: str, chat_id: str, message_id: str) -> None:
        with self.conn() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET telegram_chat_id = ?, telegram_last_message_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE draft_id = ?
                """,
                (chat_id, message_id, draft_id),
            )

    def claim_approval(self, draft_id: str, expected_version: int) -> bool:
        """Atomic approve. Returns True only for the winner."""
        with self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE drafts
                SET status = 'processing_assets',
                    approved_copy_json = current_copy_json,
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE draft_id = ? AND status = 'awaiting_approval' AND version = ?
                """,
                (draft_id, expected_version),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1

    def mark_failed(self, draft_id: str, error: str) -> None:
        self.set_status(draft_id, "failed", last_error=error, increment_retry=True)

    def mark_staged(self, draft_id: str, copy: Optional[CopyBundle] = None) -> None:
        with self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if copy is not None:
                conn.execute(
                    """
                    UPDATE drafts
                    SET status = 'staged', current_copy_json = ?, approved_copy_json = ?,
                        last_error = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE draft_id = ?
                    """,
                    (copy.model_dump_json(), copy.model_dump_json(), draft_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE drafts
                    SET status = 'staged', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE draft_id = ?
                    """,
                    (draft_id,),
                )
            conn.execute("COMMIT")

    def list_processing(self) -> list[str]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT draft_id FROM drafts WHERE status = 'processing_assets'"
            ).fetchall()
            return [r["draft_id"] for r in rows]

    def insert_revision(
        self,
        revision_id: str,
        draft_id: str,
        version_number: int,
        copy: CopyBundle,
        feedback_text: Optional[str] = None,
    ) -> None:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO revisions (revision_id, draft_id, version_number, copy_json, feedback_text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (revision_id, draft_id, version_number, copy.model_dump_json(), feedback_text),
            )

    def claim_update_id(self, update_id: str, draft_id: Optional[str] = None) -> bool:
        """Insert webhook update_id. False means already processed."""
        try:
            with self.conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO webhook_idempotency (update_id, draft_id) VALUES (?, ?)",
                    (str(update_id), draft_id),
                )
                conn.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            return False

    def upsert_media(
        self,
        asset_id: str,
        draft_id: str,
        platform: str,
        image_url: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
        verified: bool = False,
        postiz_media_id: Optional[str] = None,
    ) -> None:
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO media_assets (
                    asset_id, draft_id, platform, image_url, width, height, verified, postiz_media_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    image_url = excluded.image_url,
                    width = excluded.width,
                    height = excluded.height,
                    verified = excluded.verified,
                    postiz_media_id = COALESCE(excluded.postiz_media_id, media_assets.postiz_media_id)
                """,
                (
                    asset_id,
                    draft_id,
                    platform,
                    image_url,
                    width,
                    height,
                    1 if verified else 0,
                    postiz_media_id,
                ),
            )

    def list_media(self, draft_id: str) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM media_assets WHERE draft_id = ? ORDER BY created_at ASC",
                (draft_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_media_verified(
        self, asset_id: str, width: int, height: int, verified: bool = True
    ) -> None:
        with self.conn() as conn:
            conn.execute(
                """
                UPDATE media_assets
                SET verified = ?, width = ?, height = ?
                WHERE asset_id = ?
                """,
                (1 if verified else 0, width, height, asset_id),
            )

    def set_postiz_media_id(self, asset_id: str, postiz_media_id: str) -> None:
        with self.conn() as conn:
            conn.execute(
                "UPDATE media_assets SET postiz_media_id = ? WHERE asset_id = ?",
                (postiz_media_id, asset_id),
            )



    def _decode_flow_row(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        raw_inputs = item.pop("inputs_json", None)
        raw_audit = item.pop("audit_json", None)
        try:
            item["inputs"] = json.loads(raw_inputs) if raw_inputs else {}
        except json.JSONDecodeError:
            item["inputs"] = {}
        try:
            item["audit"] = json.loads(raw_audit) if raw_audit else []
        except json.JSONDecodeError:
            item["audit"] = []
        item["review_required"] = bool(item.get("review_required", 1))
        return item

    def create_flow_task(
        self,
        task_id: str,
        draft_id: str,
        title: str,
        goal: str,
        source: str,
        task_type: str,
        risk_tier: str,
        owner_role: str,
        preferred_owner: str,
        output_required: Optional[str],
        review_required: bool,
        envelope_status: str,
        queue: str,
        inputs: Optional[dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> dict[str, Any]:
        from datetime import datetime, timezone

        now = created_at or datetime.now(timezone.utc).isoformat()
        with self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO flow_tasks (
                    task_id, draft_id, title, goal, source, task_type, risk_tier,
                    owner_role, preferred_owner, output_required, review_required,
                    envelope_status, queue, inputs_json, audit_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
                """,
                (
                    task_id,
                    draft_id,
                    title,
                    goal,
                    source,
                    task_type,
                    risk_tier,
                    owner_role,
                    preferred_owner,
                    output_required,
                    1 if review_required else 0,
                    envelope_status,
                    queue,
                    json.dumps(inputs or {}),
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
        return self.get_flow_task(task_id)

    def get_flow_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM flow_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return self._decode_flow_row(row) if row else None

    def list_flow_task_rows(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        with self.conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    f.task_id, f.draft_id, f.title, f.goal, f.source, f.task_type,
                    f.risk_tier, f.owner_role, f.preferred_owner, f.output_required,
                    f.review_required, f.envelope_status, f.queue, f.inputs_json,
                    f.block_reason, f.audit_json, f.created_at, f.updated_at,
                    d.brief, d.status, d.version, d.current_copy_json, d.approved_copy_json,
                    d.last_error, d.retry_count, d.created_at AS draft_created_at,
                    d.updated_at AS draft_updated_at
                FROM flow_tasks f
                JOIN drafts d ON d.draft_id = f.draft_id
                ORDER BY f.updated_at DESC
                """
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            task = self._decode_flow_row(
                {
                    "task_id": item["task_id"],
                    "draft_id": item["draft_id"],
                    "title": item["title"],
                    "goal": item["goal"],
                    "source": item["source"],
                    "task_type": item["task_type"],
                    "risk_tier": item["risk_tier"],
                    "owner_role": item["owner_role"],
                    "preferred_owner": item["preferred_owner"],
                    "output_required": item["output_required"],
                    "review_required": item["review_required"],
                    "envelope_status": item["envelope_status"],
                    "queue": item["queue"],
                    "inputs_json": item["inputs_json"],
                    "block_reason": item["block_reason"],
                    "audit_json": item["audit_json"],
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                }
            )
            draft = {
                "draft_id": item["draft_id"],
                "brief": item["brief"],
                "status": item["status"],
                "version": item["version"],
                "current_copy_json": item["current_copy_json"],
                "approved_copy_json": item["approved_copy_json"],
                "last_error": item["last_error"],
                "retry_count": item["retry_count"],
                "created_at": item["draft_created_at"],
                "updated_at": item["draft_updated_at"],
            }
            out.append((task, draft))
        return out

    def update_flow_task(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "envelope_status",
            "queue",
            "block_reason",
            "output_required",
        }
        sets = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update flow_task field {key}")
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return
        from datetime import datetime, timezone

        sets.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(task_id)
        with self.conn() as conn:
            conn.execute(
                f"UPDATE flow_tasks SET {', '.join(sets)} WHERE task_id = ?",
                tuple(values),
            )

    def append_flow_audit(
        self,
        task_id: str,
        action: str,
        actor: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        from datetime import datetime, timezone

        event = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "actor": actor,
            "details": details or {},
        }
        with self.conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT audit_json FROM flow_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return
            try:
                audit = json.loads(row["audit_json"] or "[]")
            except json.JSONDecodeError:
                audit = []
            audit.append(event)
            conn.execute(
                """
                UPDATE flow_tasks
                SET audit_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    json.dumps(audit),
                    datetime.now(timezone.utc).isoformat(),
                    task_id,
                ),
            )
            conn.execute("COMMIT")


def parse_copy(raw: Optional[str]) -> Optional[CopyBundle]:
    if not raw:
        return None
    data = json.loads(raw)
    return CopyBundle.model_validate(data)
