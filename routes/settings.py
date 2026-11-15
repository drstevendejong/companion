"""
routes/settings.py - Gemini API key status, validation, and local JSON persistence endpoints.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from services import key_store

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ApiKeySaveRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    validate_key: bool = True


@router.get("/api-key")
def get_api_key_status(
    request: Request,
    verify: bool = Query(default=False),
) -> Dict[str, Any]:
    gemini = request.app.state.gemini
    key_path = gemini.key_path
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
