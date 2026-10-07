"""Rutas del portal Cliente PH (solo lectura)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import portal_ph_service

router = APIRouter(tags=["Portal PH"])
templates = Jinja2Templates(directory="templates")


def _render(name: str, context: dict, status_code: int = 200):
    try:
        return templates.TemplateResponse(name, context, status_code=status_code)
    except TypeError:
        return templates.TemplateResponse(
            request=context.get("request"),
            name=name,
            context=context,
            status_code=status_code,
        )


@router.get("/portal-ph", include_in_schema=False)
def portal_ph(request: Request, conjunto_id: str = ""):
    user_id = getattr(request.state, "user_id", None)
    data = portal_ph_service.listar_unidades_portal(
        user_id,
        conjunto_id=conjunto_id or None,
    )
    return _render(
        "portal_ph.html",
        {
            "request": request,
            "conjuntos": data["conjuntos"],
            "conjunto_id": data["conjunto_id"],
            "unidades": data["unidades"],
        },
    )
