"""
storage/db.py - SQLite persistence for projects, sessions, messages, gates, and patch backups.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from models import (
    AttachmentItem,
    MessageRecord,
    PatchRecord,
    PresetCommand,
    ProjectCreate,
    ProjectProfile,
    ProjectUpdate,
    SessionCreate,
    SessionInfo,
    SessionUpdate,
    TokenStats,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CompanionDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    python_path TEXT,
                    directives_path TEXT,
                    backup_dir TEXT NOT NULL DEFAULT 'backup',
                    post_patch_command TEXT,
                    cdp_port INTEGER NOT NULL DEFAULT 9222,
                    ignore_patterns_json TEXT NOT NULL DEFAULT '[]',
                    preset_commands_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    model TEXT NOT NULL,
                    thinking_level TEXT NOT NULL DEFAULT 'HIGH',
                    auto_approve_read_tokens INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    token_usage_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_gates (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS patch_history (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    file_path TEXT NOT NULL,
                    backup_path TEXT,
                    existed_before INTEGER NOT NULL DEFAULT 1,
                    summary TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    reverted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )

    # --- Projects ---

    def _row_to_project(self, row: sqlite3.Row) -> ProjectProfile:
        presets_raw = json.loads(row["preset_commands_json"] or "[]")
        presets = [PresetCommand(**p) for p in presets_raw]
        return ProjectProfile(
            id=row["id"],
            name=row["name"],
            root_path=row["root_path"],
            python_path=row["python_path"],
            directives_path=row["directives_path"],
            backup_dir=row["backup_dir"],
            post_patch_command=row["post_patch_command"],
            cdp_port=row["cdp_port"],
            ignore_patterns=json.loads(row["ignore_patterns_json"] or "[]"),
            preset_commands=presets,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_projects(self) -> List[ProjectProfile]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, name ASC"
            ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def get_project(self, project_id: str) -> Optional[ProjectProfile]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._row_to_project(row) if row else None

    def create_project(self, data: ProjectCreate) -> ProjectProfile:
        now = _utc_now()
        project_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO projects (
                    id, name, root_path, python_path, directives_path,
                    backup_dir, post_patch_command, cdp_port,
                    ignore_patterns_json, preset_commands_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    data.name,
                    data.root_path,
                    data.python_path,
                    data.directives_path,
                    data.backup_dir,
                    data.post_patch_command,
                    data.cdp_port,
                    json.dumps(data.ignore_patterns),
                    json.dumps([p.model_dump() for p in data.preset_commands]),
                    now,
                    now,
                ),
            )
        created = self.get_project(project_id)
        if created is None:
            raise RuntimeError("Failed to read newly created project.")
        return created

    def update_project(self, project_id: str, data: ProjectUpdate) -> Optional[ProjectProfile]:
        existing = self.get_project(project_id)
        if existing is None:
            return None
        updated = existing.model_copy(update=data.model_dump(exclude_unset=True))
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE projects SET
                    name = ?, root_path = ?, python_path = ?, directives_path = ?,
                    backup_dir = ?, post_patch_command = ?, cdp_port = ?,
                    ignore_patterns_json = ?, preset_commands_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.name,
                    updated.root_path,
                    updated.python_path,
                    updated.directives_path,
                    updated.backup_dir,
                    updated.post_patch_command,
                    updated.cdp_port,
                    json.dumps(updated.ignore_patterns),
                    json.dumps([p.model_dump() for p in updated.preset_commands]),
                    now,
                    project_id,
                ),
            )
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0

    # --- Sessions ---

    def _row_to_session(self, row: sqlite3.Row) -> SessionInfo:
        return SessionInfo(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            model=row["model"],
            thinking_level=row["thinking_level"],
            auto_approve_read_tokens=row["auto_approve_read_tokens"],
            token_stats=TokenStats(
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                thinking_tokens=row["thinking_tokens"],
                total_tokens=row["total_tokens"],
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_sessions(self, project_id: Optional[str] = None) -> List[SessionInfo]:
        with self._connect() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE project_id = ? ORDER BY updated_at DESC",
                    (project_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC"
                ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def get_session(self, session_id: str) -> Optional[SessionInfo]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def create_session(self, data: SessionCreate) -> SessionInfo:
        now = _utc_now()
        session_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, project_id, title, model, thinking_level,
                    auto_approve_read_tokens, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    data.project_id,
                    data.title,
                    data.model,
                    data.thinking_level,
                    data.auto_approve_read_tokens,
                    now,
                    now,
                ),
            )
        created = self.get_session(session_id)
        if created is None:
            raise RuntimeError("Failed to read newly created session.")
        return created

    def update_session(self, session_id: str, data: SessionUpdate) -> Optional[SessionInfo]:
        existing = self.get_session(session_id)
        if existing is None:
            return None
        changes = data.model_dump(exclude_unset=True)
        now = _utc_now()
        title = changes.get("title", existing.title)
        model = changes.get("model", existing.model)
        thinking_level = changes.get("thinking_level", existing.thinking_level)
        auto_read = changes.get("auto_approve_read_tokens", existing.auto_approve_read_tokens)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, model = ?, thinking_level = ?,
                    auto_approve_read_tokens = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, model, thinking_level, auto_read, now, session_id),
            )
        return self.get_session(session_id)

    def add_session_tokens(self, session_id: str, usage: TokenStats) -> Optional[SessionInfo]:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    thinking_tokens = thinking_tokens + ?,
                    total_tokens = total_tokens + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.thinking_tokens,
                    usage.total_tokens,
                    now,
                    session_id,
                ),
            )
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cur.rowcount > 0

    # --- Messages ---

    def list_messages(self, session_id: str) -> List[MessageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
                (session_id,),
            ).fetchall()
        results: List[MessageRecord] = []
        for r in rows:
            attachments = [AttachmentItem(**a) for a in json.loads(r["attachments_json"] or "[]")]
            tool_calls = json.loads(r["tool_calls_json"] or "[]")
            usage_raw = r["token_usage_json"]
            usage = TokenStats(**json.loads(usage_raw)) if usage_raw else None
            results.append(
                MessageRecord(
                    id=r["id"],
                    session_id=r["session_id"],
                    role=r["role"],
                    text=r["text"],
                    attachments=attachments,
                    tool_calls=tool_calls,
                    token_usage=usage,
                    created_at=r["created_at"],
                )
            )
        return results

    def add_message(
        self,
        session_id: str,
        role: str,
        text: str,
        attachments: Optional[List[AttachmentItem]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        token_usage: Optional[TokenStats] = None,
    ) -> MessageRecord:
        msg_id = str(uuid.uuid4())
        now = _utc_now()
        att_list = attachments or []
        tc_list = tool_calls or []
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    id, session_id, role, text, attachments_json,
                    tool_calls_json, token_usage_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    session_id,
                    role,
                    text,
                    json.dumps([a.model_dump() for a in att_list]),
                    json.dumps(tc_list),
                    json.dumps(token_usage.model_dump()) if token_usage else None,
                    now,
                ),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        return MessageRecord(
            id=msg_id,
            session_id=session_id,
            role=role,  # type: ignore[arg-type]
            text=text,
            attachments=att_list,
            tool_calls=tc_list,
            token_usage=token_usage,
            created_at=now,
        )

    # --- Pending Tool Gates ---

    def create_gate(
        self,
        session_id: str,
        tool_name: str,
        args: Dict[str, Any],
        preview: Dict[str, Any],
    ) -> Dict[str, Any]:
        gate_id = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_gates (
                    id, session_id, tool_name, args_json, preview_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (gate_id, session_id, tool_name, json.dumps(args), json.dumps(preview), now),
            )
        return {
            "id": gate_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "args": args,
            "preview": preview,
            "status": "pending",
            "created_at": now,
        }

    def get_gate(self, gate_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_gates WHERE id = ?", (gate_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "tool_name": row["tool_name"],
            "args": json.loads(row["args_json"]),
            "preview": json.loads(row["preview_json"]),
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
        }

    def resolve_gate(
        self, gate_id: str, status: str, result: Optional[Dict[str, Any]] = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_gates SET status = ?, result_json = ? WHERE id = ?",
                (status, json.dumps(result) if result is not None else None, gate_id),
            )

    def list_pending_gates(self, session_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_gates WHERE session_id = ? AND status = 'pending' ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "tool_name": r["tool_name"],
                "args": json.loads(r["args_json"]),
                "preview": json.loads(r["preview_json"]),
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # --- Patch History / Backups ---

    def add_patch_record(
        self,
        project_id: str,
        file_path: str,
        backup_path: Optional[str],
        existed_before: bool,
        summary: str,
        diff: str,
        session_id: Optional[str] = None,
    ) -> PatchRecord:
        patch_id = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO patch_history (
                    id, session_id, project_id, file_path, backup_path,
                    existed_before, summary, diff, reverted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    patch_id,
                    session_id,
                    project_id,
                    file_path,
                    backup_path,
                    1 if existed_before else 0,
                    summary,
                    diff,
                    now,
                ),
            )
        return PatchRecord(
            id=patch_id,
            session_id=session_id,
            project_id=project_id,
            file_path=file_path,
            backup_path=backup_path,
            existed_before=existed_before,
            summary=summary,
            diff=diff,
            created_at=now,
            reverted=False,
        )

    def get_patch_record(self, patch_id: str) -> Optional[PatchRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM patch_history WHERE id = ?", (patch_id,)
            ).fetchone()
        if not row:
            return None
        return PatchRecord(
            id=row["id"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            file_path=row["file_path"],
            backup_path=row["backup_path"],
            existed_before=bool(row["existed_before"]),
            summary=row["summary"],
            diff=row["diff"],
            created_at=row["created_at"],
            reverted=bool(row["reverted"]),
        )

    def list_patch_records(self, project_id: str, limit: int = 30) -> List[PatchRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM patch_history WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [
            PatchRecord(
                id=r["id"],
                session_id=r["session_id"],
                project_id=r["project_id"],
                file_path=r["file_path"],
                backup_path=r["backup_path"],
                existed_before=bool(r["existed_before"]),
                summary=r["summary"],
                diff=r["diff"],
                created_at=r["created_at"],
                reverted=bool(r["reverted"]),
            )
            for r in rows
        ]

    def mark_patch_reverted(self, patch_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE patch_history SET reverted = 1 WHERE id = ?", (patch_id,))
