"""Versioned JSON API for external clients."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.application.dashboard_view_model import build_dashboard_view_model

router = APIRouter()


@router.get("/api/v1/health")
async def api_v1_health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/v1/portfolio/snapshot")
async def api_v1_portfolio_snapshot(request: Request) -> JSONResponse:
    """Same dashboard data as HTML page; use ``Authorization: Bearer <access_token>`` from CLI."""
    context, redirect = await build_dashboard_view_model(request, allow_bearer=True)
    if redirect is not None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    assert context is not None
    snap = {k: v for k, v in context.items() if k not in ("request", "dashboard_bootstrap_json")}
    raw_boot = context.get("dashboard_bootstrap_json")
    if isinstance(raw_boot, str):
        try:
            snap["dashboard_bootstrap"] = json.loads(raw_boot)
        except json.JSONDecodeError:
            snap["dashboard_bootstrap"] = {}
    return JSONResponse(jsonable_encoder(snap))
