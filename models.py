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
    cdp_port: int = 9222
    ignore_patterns: List[str] = Field(default_factory=list)
    preset_commands: List[PresetCommand] = Field(default_factory=list)
    created_at: str
    updated_at: str


class SessionCreate(BaseModel):
    project_id: str
    title: str = "New Session"
    model: str = "gemini-4-hydrogen"
    thinking_level: str = "HIGH"
    auto_approve_read_tokens: int = 0


class SessionUpdate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None
    thinking_level: Optional[str] = None
    auto_approve_read_tokens: Optional[int] = None


class TokenStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0


class SessionInfo(BaseModel):
    id: str
    project_id: str
    title: str
    model: str
    thinking_level: str
    auto_approve_read_tokens: int = 0
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


class ToolGateDecision(BaseModel):
    action: Literal["approve", "deny", "edit_range", "write_only", "write_and_run"]
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    reason: Optional[str] = None


class RunCommandRequest(BaseModel):
    project_id: str
    command: str


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


class MessageRecord(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "model", "tool_event"]
    text: str
    attachments: List[AttachmentItem] = Field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    token_usage: Optional[TokenStats] = None
    created_at: str
