"""
routes/settings.py - Gemini API key status, validation, and local JSON persistence endpoints.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from models import ScoutSettingsUpdate
from services import key_store, local_scout, model_catalog
from services.key_store import has_configured_api_key

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _scout_status_payload(db: Any, gemini: Any) -> Dict[str, Any]:
    has_key = has_configured_api_key(gemini)
    cfg = db.get_scout_settings(has_key=has_key)
    try:
        candidates = local_scout._get_candidate_models(gemini) if has_key else []
    except Exception:
        candidates = model_catalog.rank_light_scout_models(
            model_catalog.get_fallback_catalog(), allow_non_lite_fallback=False
        )
    telemetry = local_scout.get_telemetry_snapshot(db, gemini)
    return {
        **cfg,
        "has_key": has_key,
        "active_model": candidates[0] if candidates else None,
        "available_lite_models": candidates,
        "telemetry": telemetry,
    }


class ApiKeySaveRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    validate_key: bool = True


@router.get("/telemetry")
def get_telemetry(
    request: Request,
    model: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    return local_scout.get_telemetry_snapshot(
        request.app.state.db,
        getattr(request.app.state, "gemini", None),
        model=model,
    )


@router.get("/scout")
def get_scout_settings_endpoint(request: Request) -> Dict[str, Any]:
    return _scout_status_payload(request.app.state.db, request.app.state.gemini)


@router.post("/scout")
def update_scout_settings_endpoint(
    payload: ScoutSettingsUpdate, request: Request
) -> Dict[str, Any]:
    db = request.app.state.db
    gemini = request.app.state.gemini
    has_key = has_configured_api_key(gemini)
    if payload.enabled is True and not has_key:
        raise HTTPException(
            status_code=400,
            detail="Configure a Gemini API key first before enabling Local AI Scout.",
        )
    db.set_scout_settings(
        enabled=payload.enabled,
        auto_approve=payload.auto_approve,
        max_tokens=payload.max_tokens,
        summarize_threshold_chars=payload.summarize_threshold_chars,
    )
    return _scout_status_payload(db, gemini)


@router.get("/api-key")
def get_api_key_status(
    request: Request,
    verify: bool = Query(default=False),
) -> Dict[str, Any]:
    gemini = request.app.state.gemini
    key_path = gemini.key_path
    if getattr(gemini, "_client_factory", None) is not None:
        return {
            "configured": True,
            "valid": True,
            "source": "test",
            "masked_preview": "test-key",
            "key_file": str(key_path),
            "error": "",
        }
    try:
        api_key, source = key_store.load_api_key(key_path)
    except RuntimeError as exc:
        return {
            "configured": False,
            "valid": False,
            "source": "none",
            "masked_preview": "",
            "key_file": str(key_path),
            "error": str(exc),
        }

    if not api_key:
        return {
            "configured": False,
            "valid": False,
            "source": "none",
            "masked_preview": "",
            "key_file": str(key_path),
            "error": "No Gemini API key configured.",
        }

    valid: bool | None = None
    error = ""
    if verify:
        valid, error = gemini.verify_key(api_key)

    return {
        "configured": True,
        "valid": valid,
        "source": source,
        "masked_preview": key_store.mask_api_key(api_key),
        "key_file": str(key_path),
        "error": error,
    }


@router.post("/api-key")
def save_api_key_endpoint(payload: ApiKeySaveRequest, request: Request) -> Dict[str, Any]:
    gemini = request.app.state.gemini
    clean = payload.api_key.strip()
    if not clean:
        raise HTTPException(status_code=400, detail="API key cannot be empty.")

    if payload.validate_key:
        ok, err = gemini.verify_key(clean)
        if not ok:
            raise HTTPException(status_code=400, detail=f"API key validation failed: {err}")

    saved_path = key_store.save_api_key(clean, gemini.key_path)
    gemini.reset_client()
    return {
        "saved": True,
        "valid": True if payload.validate_key else None,
        "source": "file",
        "masked_preview": key_store.mask_api_key(clean),
        "key_file": str(saved_path),
    }


@router.delete("/api-key")
def delete_api_key_endpoint(request: Request) -> Dict[str, Any]:
    gemini = request.app.state.gemini
    removed = key_store.clear_api_key(gemini.key_path)
    gemini.reset_client()
    return {"removed": removed}
