"""REST API for chart annotations (trendlines, levels, indicators) per stock.

Endpoints
---------
GET  /dashboard/chart-annotations?instrument_token=TOKEN
     Returns saved trendlines, levels, and indicators for a stock.

POST /dashboard/chart-annotations
     Body: {"instrument_token": int, "trendlines": [...], "levels": [...], "indicators": [...]}
     Replaces all annotations for the token.

DELETE /dashboard/chart-annotations?instrument_token=TOKEN
     Removes all annotations for the token.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.infrastructure import chart_annotations_store
from app.presentation.http.server_auth import authorized_browser_or_api, kite_for_request

logger = logging.getLogger(__name__)

router = APIRouter()


class AnnotationSaveRequest(BaseModel):
    instrument_token: int = Field(..., ge=1)
    trendlines: list[dict[str, Any]] = Field(default_factory=list)
    levels: list[dict[str, Any]] = Field(default_factory=list)
    indicators: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/dashboard/chart-annotations")
async def get_chart_annotations(
    request: Request,
    instrument_token: int = Query(..., ge=1),
) -> JSONResponse:
    """Return saved trendlines, levels, and indicators for a stock."""
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if kite_for_request() is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        annotations = chart_annotations_store.load(instrument_token)
    except Exception as exc:
        logger.exception("chart-annotations GET failed: %s", exc)
        return JSONResponse({"error": "failed to load annotations"}, status_code=500)

    return JSONResponse({
        "instrument_token": instrument_token,
        **annotations,
    })


@router.post("/dashboard/chart-annotations")
async def save_chart_annotations(
    request: Request,
    body: AnnotationSaveRequest,
) -> JSONResponse:
    """Replace all annotations for a stock."""
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if kite_for_request() is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        chart_annotations_store.save(
            body.instrument_token,
            trendlines=body.trendlines,
            levels=body.levels,
            indicators=body.indicators,
        )
    except Exception as exc:
        logger.exception("chart-annotations POST failed: %s", exc)
        return JSONResponse({"error": "failed to save annotations"}, status_code=500)

    return JSONResponse({"ok": True, "instrument_token": body.instrument_token})


@router.delete("/dashboard/chart-annotations")
async def delete_chart_annotations(
    request: Request,
    instrument_token: int = Query(..., ge=1),
) -> JSONResponse:
    """Remove all annotations for a stock."""
    if not authorized_browser_or_api(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if kite_for_request() is None:
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        chart_annotations_store.delete(instrument_token)
    except Exception as exc:
        logger.exception("chart-annotations DELETE failed: %s", exc)
        return JSONResponse({"error": "failed to delete annotations"}, status_code=500)

    return JSONResponse({"ok": True, "instrument_token": instrument_token})
