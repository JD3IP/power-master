"""Billing and financial views routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/accounting", response_class=HTMLResponse)
async def accounting_page(request: Request) -> HTMLResponse:
    """Render accounting page."""
    templates = request.app.state.templates

    # Use the in-memory accounting engine (billing cycles are not persisted to DB)
    accounting_engine = getattr(request.app.state, "accounting", None)
    billing_cycle = None
    if accounting_engine:
        summary = accounting_engine.get_summary()
        billing_cycle = summary.cycle

    return templates.TemplateResponse(
        "accounting.html",
        {
            "request": request,
            "billing_cycle": billing_cycle,
        },
    )
