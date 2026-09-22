"""Rutas web para cartas de cobro prejurídico en Word."""
from __future__ import annotations

import io
from datetime import date
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from psycopg2.extras import RealDictCursor

import catalogos_service
import cartas_cobro_service
import db

router = APIRouter(tags=["Cartas cobro"])
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


def _redirect(path: str = "/cartas-cobro", **params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    url = f"{path}?{query}" if query else path
    return RedirectResponse(url=url, status_code=303)


def _listar_conjuntos() -> list[dict]:
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            return catalogos_service.listar_conjuntos(cur, activos=True)
    finally:
        conn.release()


@router.get("/cartas-cobro")
def vista_cartas_cobro(
    request: Request,
    conjunto_id: Optional[int] = None,
    tipo_cartera: str = "PREJURIDICO",
    fecha_corte: Optional[str] = None,
    fecha_limite: Optional[str] = None,
    saldo_minimo: float = 0,
    error: Optional[str] = None,
    mensaje: Optional[str] = None,
):
    conjuntos = _listar_conjuntos()
    hoy = date.today()
    try:
        corte = cartas_cobro_service.parse_fecha(fecha_corte, default=hoy)
        limite = cartas_cobro_service.parse_fecha(
            fecha_limite, default=cartas_cobro_service.fecha_limite_default(hoy)
        )
    except ValueError as exc:
        return _render(
            "cartas_cobro.html",
            {
                "request": request,
                "conjuntos": conjuntos,
                "conjunto_id": conjunto_id,
                "tipo_cartera": (tipo_cartera or "PREJURIDICO").upper(),
                "fecha_corte": hoy.isoformat(),
                "fecha_limite": cartas_cobro_service.fecha_limite_default(hoy).isoformat(),
                "saldo_minimo": saldo_minimo or 0,
                "candidatos": [],
                "error": str(exc),
                "mensaje": mensaje,
            },
        )

    candidatos: list[dict] = []
    err = error
    if conjunto_id:
        try:
            candidatos = cartas_cobro_service.listar_candidatos(
                conjunto_id=int(conjunto_id),
                tipo_cartera=tipo_cartera or "PREJURIDICO",
                fecha_corte=corte,
                saldo_minimo=float(saldo_minimo or 0),
            )
        except ValueError as exc:
            err = str(exc)
        except Exception:
            err = "No fue posible cargar los candidatos del conjunto."

    return _render(
        "cartas_cobro.html",
        {
            "request": request,
            "conjuntos": conjuntos,
            "conjunto_id": int(conjunto_id) if conjunto_id else None,
            "tipo_cartera": (tipo_cartera or "PREJURIDICO").upper(),
            "fecha_corte": corte.isoformat(),
            "fecha_limite": limite.isoformat(),
            "saldo_minimo": saldo_minimo or 0,
            "candidatos": candidatos,
            "error": err,
            "mensaje": mensaje,
            "max_lote": cartas_cobro_service.MAX_LOTE,
        },
    )


@router.post("/cartas-cobro/generar")
async def generar_cartas_cobro(
    request: Request,
    conjunto_id: int = Form(...),
    tipo_cartera: str = Form("PREJURIDICO"),
    fecha_corte: str = Form(""),
    fecha_limite: str = Form(""),
    saldo_minimo: float = Form(0),
):
    hoy = date.today()
    try:
        corte = cartas_cobro_service.parse_fecha(fecha_corte, default=hoy)
        limite = cartas_cobro_service.parse_fecha(
            fecha_limite, default=cartas_cobro_service.fecha_limite_default(hoy)
        )
    except ValueError as exc:
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            fecha_corte=fecha_corte,
            fecha_limite=fecha_limite,
            saldo_minimo=saldo_minimo,
            error=str(exc),
        )

    form = await request.form()
    raw_ids = form.getlist("inmueble_ids")
    try:
        inmueble_ids = [int(x) for x in raw_ids if str(x).strip()]
    except (TypeError, ValueError):
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            fecha_corte=corte.isoformat(),
            fecha_limite=limite.isoformat(),
            saldo_minimo=saldo_minimo,
            error="Selección de inmuebles inválida.",
        )

    try:
        candidatos = cartas_cobro_service.listar_candidatos(
            conjunto_id=int(conjunto_id),
            tipo_cartera=tipo_cartera or "PREJURIDICO",
            fecha_corte=corte,
            saldo_minimo=float(saldo_minimo or 0),
        )
        seleccionados = cartas_cobro_service.resolver_seleccion(candidatos, inmueble_ids)
        contenido, filename, media_type = cartas_cobro_service.generar_paquete_docx(
            seleccionados, fecha_limite=limite, fecha_carta=hoy
        )
    except ValueError as exc:
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            fecha_corte=corte.isoformat(),
            fecha_limite=limite.isoformat(),
            saldo_minimo=saldo_minimo,
            error=str(exc),
        )
    except Exception:
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            fecha_corte=corte.isoformat(),
            fecha_limite=limite.isoformat(),
            saldo_minimo=saldo_minimo,
            error="No fue posible generar las cartas.",
        )

    return StreamingResponse(
        io.BytesIO(contenido),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
