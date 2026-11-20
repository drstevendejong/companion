"""
storage/db.py - SQLite persistence for projects, sessions, messages, gates, and patch backups.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from models import (
    AttachmentItem,
    CheckpointRecord,
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


def _pacific_midnight_utc(now_ts: float) -> tuple[float, str]:
    """
    Returns (utc_epoch_seconds_of_pacific_midnight, pacific_date_yyyy_mm_dd).
    Uses America/Los_Angeles via zoneinfo when available, with DST-aware fallback.
    """
    dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    try:
        tz = ZoneInfo("America/Los_Angeles")
        dt_pt = dt_utc.astimezone(tz)
        mid_pt = dt_pt.replace(hour=0, minute=0, second=0, microsecond=0)
        return mid_pt.astimezone(timezone.utc).timestamp(), mid_pt.strftime("%Y-%m-%d")
    except ZoneInfoNotFoundError:
        # Pure-Python US Pacific DST fallback (second Sunday of March .. first Sunday of Nov)
        def _nth_sunday(year: int, month: int, nth: int) -> datetime:
            d = datetime(year, month, 1, 2, 0, tzinfo=timezone.utc)
            days_to_sun = (6 - d.weekday()) % 7
            return d + timedelta(days=days_to_sun + (nth - 1) * 7)

        approx_std = dt_utc - timedelta(hours=8)
        y = approx_std.year
        dst_start = _nth_sunday(y, 3, 2) + timedelta(hours=8)  # 02:00 PST -> 10:00 UTC
        dst_end = _nth_sunday(y, 11, 1) + timedelta(hours=7)   # 02:00 PDT -> 09:00 UTC
        offset_hours = -7 if dst_start <= dt_utc < dst_end else -8
        pt_tz = timezone(timedelta(hours=offset_hours))
        dt_pt = dt_utc.astimezone(pt_tz)
        mid_pt = dt_pt.replace(hour=0, minute=0, second=0, microsecond=0)
        return mid_pt.astimezone(timezone.utc).timestamp(), mid_pt.strftime("%Y-%m-%d")


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
                    fast_test_command TEXT,
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
                    api_enabled INTEGER NOT NULL DEFAULT 0,
                    model TEXT NOT NULL,
                    thinking_level TEXT NOT NULL DEFAULT 'HIGH',
                    auto_approve_read_tokens INTEGER NOT NULL DEFAULT 0,
                    directives_sent INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    last_context_tokens INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    created_ts REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_api_usage_ts
                    ON api_usage_events(created_ts);

                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'active',
                    snapshot_dir TEXT NOT NULL,
                    files_count INTEGER NOT NULL DEFAULT 0,
                    modified_files_json TEXT NOT NULL DEFAULT '[]',
                    created_files_json TEXT NOT NULL DEFAULT '[]',
                    after_msg_index INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT 'Current state of the project is saved.',
                    resolution_note TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                    ON checkpoints(session_id, created_at);
                """
            )
            project_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "fast_test_command" not in project_cols:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN fast_test_command TEXT"
                )
            session_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "last_context_tokens" not in session_cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN last_context_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "directives_sent" not in session_cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN directives_sent INTEGER NOT NULL DEFAULT 0"
                )
            if "api_enabled" not in session_cols:
                # Existing legacy sessions predate Bridge mode and are API sessions
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN api_enabled INTEGER NOT NULL DEFAULT 1"
                )

    # --- Projects ---

    def _row_to_project(self, row: sqlite3.Row) -> ProjectProfile:
        row_keys = set(row.keys())
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
            fast_test_command=row["fast_test_command"] if "fast_test_command" in row_keys else None,
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
                    backup_dir, post_patch_command, fast_test_command, cdp_port,
                    ignore_patterns_json, preset_commands_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    data.name,
                    data.root_path,
                    data.python_path,
                    data.directives_path,
                    data.backup_dir,
                    data.post_patch_command,
                    data.fast_test_command,
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
                    backup_dir = ?, post_patch_command = ?, fast_test_command = ?, cdp_port = ?,
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
                    updated.fast_test_command,
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
        row_keys = set(row.keys())
        last_ctx = int(row["last_context_tokens"] or 0) if "last_context_tokens" in row_keys else 0
        dir_sent = bool(row["directives_sent"]) if "directives_sent" in row_keys else False
        api_enabled = bool(row["api_enabled"]) if "api_enabled" in row_keys else True
        return SessionInfo(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            api_enabled=api_enabled,
            model=row["model"],
            thinking_level=row["thinking_level"],
            auto_approve_read_tokens=row["auto_approve_read_tokens"],
            directives_sent=dir_sent,
            token_stats=TokenStats(
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                thinking_tokens=row["thinking_tokens"],
                total_tokens=row["total_tokens"],
                last_context_tokens=last_ctx,
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
                    id, project_id, title, api_enabled, model, thinking_level,
                    auto_approve_read_tokens, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    data.project_id,
                    data.title,
                    1 if data.api_enabled else 0,
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
        directives_sent = changes.get("directives_sent", existing.directives_sent)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, model = ?, thinking_level = ?,
                    auto_approve_read_tokens = ?, directives_sent = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, model, thinking_level, auto_read, 1 if directives_sent else 0, now, session_id),
            )
        return self.get_session(session_id)

    def add_session_tokens(self, session_id: str, usage: TokenStats) -> Optional[SessionInfo]:
        now = _utc_now()
        latest_ctx = usage.total_tokens or (
            usage.input_tokens + usage.output_tokens + usage.thinking_tokens
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    thinking_tokens = thinking_tokens + ?,
                    total_tokens = total_tokens + ?,
                    last_context_tokens = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.thinking_tokens,
                    usage.total_tokens,
                    latest_ctx,
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

    # --- App Settings & API Mode ---

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_api_enabled(self, has_key: bool = False) -> bool:
        if not has_key:
            return False
        raw = self.get_setting("api_enabled")
        if raw is None:
            return True
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def set_api_enabled(self, enabled: bool) -> None:
        self.set_setting("api_enabled", "true" if enabled else "false")

    # --- Rate Telemetry (RPM, TPM, RPD since 00:00 Pacific) ---

    def record_api_usage(
        self,
        model: str,
        usage: TokenStats,
        created_ts: Optional[float] = None,
    ) -> None:
        ts = float(created_ts if created_ts is not None else time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO api_usage_events (
                    model, input_tokens, output_tokens, thinking_tokens, created_ts
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    model,
                    int(usage.input_tokens or 0),
                    int(usage.output_tokens or 0),
                    int(usage.thinking_tokens or 0),
                    ts,
                ),
            )
            # Prune events older than 14 days so DB stays lean
            conn.execute("DELETE FROM api_usage_events WHERE created_ts < ?", (ts - 14 * 86400,))

    def get_rate_telemetry(
        self,
        model: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        ts = float(now_ts if now_ts is not None else time.time())
        cutoff_min = ts - 60.0
        midnight_ts, pacific_date = _pacific_midnight_utc(ts)

        with self._connect() as conn:
            all_min = conn.execute(
                """
                SELECT COUNT(*) AS rpm,
                       COALESCE(SUM(input_tokens), 0) AS tpm,
                       MIN(created_ts) AS oldest_ts
                FROM api_usage_events
                WHERE created_ts > ? AND created_ts <= ?
                """,
                (cutoff_min, ts),
            ).fetchone()
            all_day = conn.execute(
                """
                SELECT COUNT(*) AS rpd
                FROM api_usage_events
                WHERE created_ts >= ? AND created_ts <= ?
                """,
                (midnight_ts, ts),
            ).fetchone()

            if model:
                mod_min = conn.execute(
                    """
                    SELECT COUNT(*) AS rpm,
                           COALESCE(SUM(input_tokens), 0) AS tpm,
                           MIN(created_ts) AS oldest_ts
                    FROM api_usage_events
                    WHERE model = ? AND created_ts > ? AND created_ts <= ?
                    """,
                    (model, cutoff_min, ts),
                ).fetchone()
                mod_day = conn.execute(
                    """
                    SELECT COUNT(*) AS rpd
                    FROM api_usage_events
                    WHERE model = ? AND created_ts >= ? AND created_ts <= ?
                    """,
                    (model, midnight_ts, ts),
                ).fetchone()
            else:
                mod_min = all_min
                mod_day = all_day

        oldest_ts = mod_min["oldest_ts"] if mod_min else None
        next_decay = max(0, int((float(oldest_ts) + 60.0 - ts) + 0.999)) if oldest_ts is not None else 0

        return {
            "model": model,
            "rpm": int(mod_min["rpm"] or 0),
            "tpm": int(mod_min["tpm"] or 0),
            "rpd": int(mod_day["rpd"] or 0),
            "all_rpm": int(all_min["rpm"] or 0),
            "all_tpm": int(all_min["tpm"] or 0),
            "all_rpd": int(all_day["rpd"] or 0),
            "next_minute_reset_sec": next_decay,
            "pacific_date": pacific_date,
            "rpd_reset_label": "00:00 PT",
        }

    # --- Project Checkpoints (Transactional Savepoints) ---

    def _row_to_checkpoint(self, row: sqlite3.Row) -> CheckpointRecord:
        return CheckpointRecord(
            id=row["id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            status=row["status"],
            snapshot_dir=row["snapshot_dir"],
            files_count=int(row["files_count"] or 0),
            modified_files=json.loads(row["modified_files_json"] or "[]"),
            created_files=json.loads(row["created_files_json"] or "[]"),
            after_msg_index=int(row["after_msg_index"] or 0),
            summary=row["summary"],
            resolution_note=row["resolution_note"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )

    def create_checkpoint(
        self,
        checkpoint_id: str,
        project_id: str,
        session_id: str,
        snapshot_dir: str,
        files_count: int,
        after_msg_index: int,
        summary: str = "Current state of the project is saved.",
    ) -> CheckpointRecord:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (
                    id, project_id, session_id, status, snapshot_dir, files_count,
                    modified_files_json, created_files_json, after_msg_index, summary,
                    resolution_note, created_at, resolved_at
                ) VALUES (?, ?, ?, 'active', ?, ?, '[]', '[]', ?, ?, NULL, ?, NULL)
                """,
                (
                    checkpoint_id,
                    project_id,
                    session_id,
                    snapshot_dir,
                    int(files_count),
                    int(after_msg_index),
                    summary,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        return self._row_to_checkpoint(row)

    def get_checkpoint(self, checkpoint_id: str) -> Optional[CheckpointRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        return self._row_to_checkpoint(row) if row else None

    def get_active_checkpoint(
        self, session_id: Optional[str] = None, project_id: Optional[str] = None
    ) -> Optional[CheckpointRecord]:
        with self._connect() as conn:
            if session_id:
                row = conn.execute(
                    "SELECT * FROM checkpoints WHERE session_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            elif project_id:
                row = conn.execute(
                    "SELECT * FROM checkpoints WHERE project_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
            else:
                row = None
        return self._row_to_checkpoint(row) if row else None

    def list_checkpoints(self, session_id: str) -> List[CheckpointRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [self._row_to_checkpoint(r) for r in rows]

    def list_active_checkpoint_dirs(self, project_id: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_dir FROM checkpoints WHERE project_id = ? AND status = 'active'",
                (project_id,),
            ).fetchall()
        return [str(r["snapshot_dir"]) for r in rows if r["snapshot_dir"]]

    def record_checkpoint_file(
        self, checkpoint_id: str, rel_path: str, existed_before: bool
    ) -> Optional[CheckpointRecord]:
        norm = rel_path.replace("\\", "/").lstrip("./")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
            if not row:
                return None
            mods: List[str] = json.loads(row["modified_files_json"] or "[]")
            created: List[str] = json.loads(row["created_files_json"] or "[]")
            if existed_before:
                if norm not in mods:
                    mods.append(norm)
            else:
                if norm not in created:
                    created.append(norm)
            conn.execute(
                "UPDATE checkpoints SET modified_files_json = ?, created_files_json = ? WHERE id = ?",
                (json.dumps(mods), json.dumps(created), checkpoint_id),
            )
            updated = conn.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        return self._row_to_checkpoint(updated) if updated else None

    def resolve_checkpoint(
        self, checkpoint_id: str, status: str, resolution_note: Optional[str] = None
    ) -> Optional[CheckpointRecord]:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE checkpoints SET status = ?, resolution_note = ?, resolved_at = ? WHERE id = ?",
                (status, resolution_note, now, checkpoint_id),
            )
            row = conn.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        return self._row_to_checkpoint(row) if row else None

    def cancel_pending_gates(self, session_id: str, reason: str = "Checkpoint restored") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pending_gates SET status = 'cancelled', result_json = ? WHERE session_id = ? AND status = 'pending'",
                (json.dumps({"cancelled": True, "reason": reason}), session_id),
            )
            return int(cur.rowcount or 0)


