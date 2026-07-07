"""Thin admin API — per-passport budget-cap override (Windy Admin Phase 3).

ADR-WA-001 §2: platforms grow thin admin APIs; the dashboard is the
only caller and owns RBAC + the immutable audit row. This side is
deliberately minimal: one bearer token (WINDY_SEARCH_ADMIN_TOKEN, in
the lockbox), set/clear/read one Redis value. The override changes the
CAP only — spend counters are untouched, months roll as usual.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.eii import cost_cap

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin_token(
    request: Request, settings: Settings = Depends(get_settings)
) -> None:
    if not settings.admin_api_token:
        raise HTTPException(status_code=503, detail="admin API disabled (no token configured)")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token, settings.admin_api_token
    ):
        raise HTTPException(status_code=401, detail="admin token required")


class CapOverridePut(BaseModel):
    # None clears the override (fall back to tier-scaled default).
    cap_usd: float | None = Field(default=None, ge=0, le=1000)


@router.put("/budget-cap/{passport}", dependencies=[Depends(_require_admin_token)])
async def put_cap_override(passport: str, body: CapOverridePut, request: Request) -> dict:
    redis = request.app.state.redis
    if redis is None:
        raise HTTPException(status_code=503, detail="redis unavailable")
    cap_microcents = (
        None if body.cap_usd is None else int(body.cap_usd * cost_cap.MICROCENTS_PER_USD)
    )
    await cost_cap.set_cap_override(redis, passport, cap_microcents)
    return {"passport": passport, "cap_usd": body.cap_usd, "override": cap_microcents is not None}


@router.get("/budget-cap/{passport}", dependencies=[Depends(_require_admin_token)])
async def get_cap_override(passport: str, request: Request) -> dict:
    redis = request.app.state.redis
    if redis is None:
        raise HTTPException(status_code=503, detail="redis unavailable")
    override = await cost_cap.get_cap_override(redis, passport)
    return {
        "passport": passport,
        "override": override is not None,
        "cap_usd": None if override is None else override / cost_cap.MICROCENTS_PER_USD,
    }
