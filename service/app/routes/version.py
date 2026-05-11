"""Deployment-identity endpoint.

GET /version per the MF1 contract in
~/kit-army-config/docs/marathon-foundations-program-2026-05-11.md §MF1.

Separate from /health on purpose: /health is for orchestrators,
/version is for deployment verification, and /version MUST NOT
depend on DB/Redis (it's a process-level fact).

Consumer: kit-army-config deployed-state cron polls /version every
30 minutes and writes docs/deployed-state.json.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import __version__ as _PACKAGE_VERSION

router = APIRouter(tags=["health"])


_STARTED_AT = datetime.now(UTC).isoformat()


class VersionInfo(BaseModel):
    service: str = Field(description="Canonical service name.")
    version: str = Field(description="Semver from app/__init__.py.")
    commit_sha: str | None
    commit_sha_short: str | None
    build_timestamp: str | None
    started_at: str
    environment: str


@router.get(
    "/version",
    response_model=VersionInfo,
    summary="Deployment identity",
)
async def get_version() -> VersionInfo:
    commit_sha = os.getenv("COMMIT_SHA") or None
    return VersionInfo(
        service="windy-search-api",
        version=_PACKAGE_VERSION,
        commit_sha=commit_sha,
        commit_sha_short=commit_sha[:7] if commit_sha else None,
        build_timestamp=os.getenv("BUILD_TIMESTAMP") or None,
        started_at=_STARTED_AT,
        environment=os.getenv("ENVIRONMENT") or "unknown",
    )
