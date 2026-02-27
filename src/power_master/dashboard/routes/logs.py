"""Logs page — view application output logs."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    """Render the logs viewer page."""
    templates = request.app.state.templates
    return templates.TemplateResponse("logs.html", {"request": request})
