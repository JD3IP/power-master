"""Dedicated graphing page routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/graphs", response_class=HTMLResponse)
async def graphs_page(request: Request) -> HTMLResponse:
    """Render graphs page."""
    templates = request.app.state.templates

    return templates.TemplateResponse(
        "graphs.html",
        {"request": request},
    )
