"""Cron protegido: sincronización diaria de actuaciones Rama (BackgroundTasks → 202)."""
from __future__ import annotations

import os
import secrets
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg2.extras import RealDictCursor

import db
import sync_procesos_service
from observability import log_msg

router = APIRouter(tags=["Sync Rama"])
templates = Jinja2Templates(directory="templates")

CRON_HEADER = "X-Cron-Secret"


def _cron_secret() -> str:
    return (os.getenv("CRON_SECRET_KEY") or "").strip().strip('"').strip("'")


def _require_cron_secret(request: Request) -> Optional[JSONResponse]:
    """Valida X-Cron-Secret. None = OK; JSONResponse = error."""
    expected = _cron_secret()
    if not expected:
        return JSONResponse(
            {"error": "CRON_SECRET_KEY no configurada"},
            status_code=503,
        )
    supplied = (request.headers.get(CRON_HEADER) or "").strip()
    if not supplied:
        return JSONResponse(
            {"error": "Falta encabezado X-Cron-Secret"},
            status_code=401,
        )
    if not secrets.compare_digest(supplied, expected):
        return JSONResponse(
            {"error": "X-Cron-Secret inválido"},
            status_code=403,
        )
    return None


async def _run_sync_background() -> None:
    try:
        await sync_procesos_service.sincronizar_expedientes_activos()
    except Exception as exc:
        log_msg("💥 [SYNC RAMA CRON]", f"Fallo en background: {type(exc).__name__}: {exc}")


@router.post("/api/v1/cron/sync-rama")
async def cron_sync_rama(request: Request, background_tasks: BackgroundTasks):
    """Trigger Render Cron → acepta y procesa en background (HTTP 202)."""
    denied = _require_cron_secret(request)
    if denied is not None:
        return denied

    background_tasks.add_task(_run_sync_background)
    log_msg("📥 [SYNC RAMA CRON]", "Job aceptado (202)")
    return JSONResponse(
        {
            "accepted": True,
            "job": "sync-rama",
            "message": "Sincronización encolada en background",
        },
        status_code=202,
    )


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


@router.get("/alertas-seguimiento")
def vista_alertas_seguimiento(request: Request, solo_no_leidas: str = "1"):
    """Listado mínimo de novedades / alertas in-ERP."""
    solo = str(solo_no_leidas or "1").strip() not in ("0", "false", "no")
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            alertas = sync_procesos_service.listar_alertas_recientes(
                cur, limit=80, solo_no_leidas=solo
            )
            unread = sync_procesos_service.contar_alertas_no_leidas(cur)
        return _render(
            "alertas_seguimiento.html",
            {
                "request": request,
                "alertas": alertas,
                "unread": unread,
                "solo_no_leidas": solo,
            },
        )
    finally:
        conn.release()


@router.post("/alertas-seguimiento/{alerta_id}/leer")
def marcar_alerta_leida(request: Request, alerta_id: int):
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                sync_procesos_service.marcar_alerta_leida(cur, alerta_id)
    finally:
        conn.release()
    q = urlencode({"solo_no_leidas": "1", "mensaje": "Alerta+marcada+como+leída"})
    return RedirectResponse(url=f"/alertas-seguimiento?{q}", status_code=303)
