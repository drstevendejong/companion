"""
models.py - Pydantic request/response and internal domain models.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class PresetCommand(BaseModel):
    id: str
    label: str
    command: str
    is_post_patch_default: bool = False


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    root_path: str = Field(..., min_length=1)
    python_path: Optional[str] = None
    directives_path: Optional[str] = None
    backup_dir: str = "backup"
    post_patch_command: Optional[str] = None
    fast_test_command: Optional[str] = None
    cdp_port: int = 9222
    ignore_patterns: List[str] = Field(default_factory=list)
    preset_commands: List[PresetCommand] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    root_path: Optional[str] = None
    python_path: Optional[str] = None
    directives_path: Optional[str] = None
    backup_dir: Optional[str] = None
    post_patch_command: Optional[str] = None
    fast_test_command: Optional[str] = None
    cdp_port: Optional[int] = None
    ignore_patterns: Optional[List[str]] = None
    preset_commands: Optional[List[PresetCommand]] = None


class ProjectProfile(BaseModel):
    id: str
    name: str
    root_path: str
    python_path: Optional[str] = None
    directives_path: Optional[str] = None
    backup_dir: str = "backup"
    post_patch_command: Optional[str] = None
    fast_test_command: Optional[str] = None
    cdp_port: int = 9222
    ignore_patterns: List[str] = Field(default_factory=list)
    preset_commands: List[PresetCommand] = Field(default_factory=list)
    created_at: str
    updated_at: str


class SessionCreate(BaseModel):
    project_id: str
    title: str = "New Session"
    api_enabled: bool = False
    model: str = "gemini-4-hydrogen"
    thinking_level: str = "HIGH"
    auto_approve_read_tokens: int = 0


class SessionUpdate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None
    thinking_level: Optional[str] = None
    auto_approve_read_tokens: Optional[int] = None
    directives_sent: Optional[bool] = None


class TokenStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0
    last_context_tokens: int = 0


class SessionInfo(BaseModel):
    id: str
    project_id: str
    title: str
    api_enabled: bool = True
    model: str
    thinking_level: str
    auto_approve_read_tokens: int = 0
    directives_sent: bool = False
    token_stats: TokenStats = Field(default_factory=TokenStats)
    created_at: str
    updated_at: str


class AttachmentItem(BaseModel):
    kind: Literal["file", "image", "console", "terminal", "snippet"]
    label: str
    content: str
    mime_type: Optional[str] = None
    path: Optional[str] = None
    estimated_tokens: int = 0


class ChatSendRequest(BaseModel):
    session_id: str
    prompt: str = ""
    attachments: List[AttachmentItem] = Field(default_factory=list)


class ChatRetryRequest(BaseModel):
    session_id: str


class BridgeBundleRequest(BaseModel):
    session_id: str
    prompt: str = ""
    attachments: List[AttachmentItem] = Field(default_factory=list)
    include_directives: Optional[bool] = None
    record_turn: bool = True


class BridgeParseRequest(BaseModel):
    session_id: str
    raw_markdown: str = Field(..., min_length=1)
    auto_approve_reads: bool = False


class BridgePackFilesRequest(BaseModel):
    project_id: str
    paths: List[str] = Field(default_factory=list)
    filter_text: str = ""
    max_files: int = 25


class ToolGateDecision(BaseModel):
    action: Literal["approve", "deny", "edit_range", "write_only", "write_and_run"]
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    reason: Optional[str] = None


class BatchGateItem(BaseModel):
    gate_id: str
    decision: ToolGateDecision


class BatchGateDecisionRequest(BaseModel):
    session_id: str
    decisions: List[BatchGateItem] = Field(default_factory=list)


class RunCommandRequest(BaseModel):
    project_id: str
    command: str
    session_id: Optional[str] = None
    is_test_run: bool = False


class GitCommitRequest(BaseModel):
    project_id: str
    message: str = Field(..., min_length=1)
    push: bool = False
    stage_all: bool = True


class GitDraftMessageRequest(BaseModel):
    project_id: str
    model: str = "gemini-4-hydrogen"


class CdpTargetRequest(BaseModel):
    ws_url: str
    cdp_port: int = 9222
    full_page: bool = False


class PatchRecord(BaseModel):
    id: str
    session_id: Optional[str] = None
    project_id: str
    file_path: str
    backup_path: Optional[str] = None
    existed_before: bool = True
    summary: str
    diff: str
    created_at: str
    reverted: bool = False
    backup_exists: bool = True


class CheckpointRecord(BaseModel):
    id: str
    project_id: str
    session_id: str
    status: Literal["active", "committed", "restored"] = "active"
    snapshot_dir: str
    files_count: int = 0
    modified_files: List[str] = Field(default_factory=list)
    created_files: List[str] = Field(default_factory=list)
    after_msg_index: int = 0
    summary: str = "Current state of the project is saved."
    resolution_note: Optional[str] = None
    created_at: str
    resolved_at: Optional[str] = None


class MessageRecord(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "model", "tool_event"]
    text: str
    attachments: List[AttachmentItem] = Field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    token_usage: Optional[TokenStats] = None
    created_at: str
