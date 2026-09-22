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


def _cargar_plantillas(tipo_cartera: str) -> tuple[list[dict], list[dict]]:
    todas = cartas_cobro_service.listar_plantillas(solo_activas=True)
    filtradas = [
        p for p in todas
        if str(p.get("tipo_cartera") or "").upper() == (tipo_cartera or "PREJURIDICO").upper()
    ]
    return todas, filtradas


@router.get("/cartas-cobro")
def vista_cartas_cobro(
    request: Request,
    conjunto_id: Optional[int] = None,
    tipo_cartera: str = "PREJURIDICO",
    plantilla_id: Optional[int] = None,
    fecha_corte: Optional[str] = None,
    fecha_limite: Optional[str] = None,
    saldo_minimo: float = 0,
    error: Optional[str] = None,
    mensaje: Optional[str] = None,
):
    conjuntos = _listar_conjuntos()
    cartera = (tipo_cartera or "PREJURIDICO").upper()
    hoy = date.today()
    try:
        plantillas_todas, plantillas = _cargar_plantillas(cartera)
    except Exception:
        plantillas_todas, plantillas = [], []
        error = error or "No fue posible cargar las plantillas de cartas."

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
                "tipo_cartera": cartera,
                "fecha_corte": hoy.isoformat(),
                "fecha_limite": cartas_cobro_service.fecha_limite_default(hoy).isoformat(),
                "saldo_minimo": saldo_minimo or 0,
                "candidatos": [],
                "plantillas": plantillas,
                "plantillas_todas": plantillas_todas,
                "plantilla_id": plantilla_id,
                "plantilla_editar": None,
                "variables": cartas_cobro_service.PLANTILLA_VARIABLES,
                "cuerpo_nuevo": cartas_cobro_service.DEFAULT_CUERPO_PREJURIDICO,
                "error": str(exc),
                "mensaje": mensaje,
                "max_lote": cartas_cobro_service.MAX_LOTE,
            },
        )

    candidatos: list[dict] = []
    err = error
    if conjunto_id:
        try:
            candidatos = cartas_cobro_service.listar_candidatos(
                conjunto_id=int(conjunto_id),
                tipo_cartera=cartera,
                fecha_corte=corte,
                saldo_minimo=float(saldo_minimo or 0),
            )
        except ValueError as exc:
            err = str(exc)
        except Exception:
            err = "No fue posible cargar los candidatos del conjunto."

    if plantilla_id and not any(int(p["id"]) == int(plantilla_id) for p in plantillas):
        plantilla_id = None
    if not plantilla_id and plantillas:
        plantilla_id = int(plantillas[0]["id"])

    return _render(
        "cartas_cobro.html",
        {
            "request": request,
            "conjuntos": conjuntos,
            "conjunto_id": int(conjunto_id) if conjunto_id else None,
            "tipo_cartera": cartera,
            "fecha_corte": corte.isoformat(),
            "fecha_limite": limite.isoformat(),
            "saldo_minimo": saldo_minimo or 0,
            "candidatos": candidatos,
            "plantillas": plantillas,
            "plantillas_todas": plantillas_todas,
            "plantilla_id": plantilla_id,
            "plantilla_editar": None,
            "variables": cartas_cobro_service.PLANTILLA_VARIABLES,
            "cuerpo_nuevo": (
                cartas_cobro_service.DEFAULT_CUERPO_JURIDICO
                if cartera == "JURIDICO"
                else cartas_cobro_service.DEFAULT_CUERPO_PREJURIDICO
            ),
            "error": err,
            "mensaje": mensaje,
            "max_lote": cartas_cobro_service.MAX_LOTE,
        },
    )


@router.post("/cartas-cobro/plantillas/guardar")
async def guardar_plantilla_carta(
    request: Request,
    nombre: str = Form(...),
    tipo_cartera: str = Form("PREJURIDICO"),
    cuerpo: str = Form(...),
    plantilla_id: Optional[int] = Form(None),
    conjunto_id: Optional[int] = Form(None),
    fecha_corte: str = Form(""),
    fecha_limite: str = Form(""),
    saldo_minimo: float = Form(0),
):
    try:
        guardada = cartas_cobro_service.guardar_plantilla(
            nombre=nombre,
            tipo_cartera=tipo_cartera,
            cuerpo=cuerpo,
            plantilla_id=int(plantilla_id) if plantilla_id else None,
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
    except Exception:
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            fecha_corte=fecha_corte,
            fecha_limite=fecha_limite,
            saldo_minimo=saldo_minimo,
            error="No fue posible guardar la plantilla.",
        )

    return _redirect(
        conjunto_id=conjunto_id,
        tipo_cartera=guardada["tipo_cartera"],
        plantilla_id=guardada["id"],
        fecha_corte=fecha_corte,
        fecha_limite=fecha_limite,
        saldo_minimo=saldo_minimo,
        mensaje="Plantilla+guardada",
    )


@router.post("/cartas-cobro/plantillas/desactivar")
async def desactivar_plantilla_carta(
    plantilla_id: int = Form(...),
    tipo_cartera: str = Form("PREJURIDICO"),
    conjunto_id: Optional[int] = Form(None),
    fecha_corte: str = Form(""),
    fecha_limite: str = Form(""),
    saldo_minimo: float = Form(0),
):
    try:
        cartas_cobro_service.desactivar_plantilla(int(plantilla_id))
    except ValueError as exc:
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            fecha_corte=fecha_corte,
            fecha_limite=fecha_limite,
            saldo_minimo=saldo_minimo,
            error=str(exc),
        )
    except Exception:
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            fecha_corte=fecha_corte,
            fecha_limite=fecha_limite,
            saldo_minimo=saldo_minimo,
            error="No fue posible desactivar la plantilla.",
        )
    return _redirect(
        conjunto_id=conjunto_id,
        tipo_cartera=tipo_cartera,
        fecha_corte=fecha_corte,
        fecha_limite=fecha_limite,
        saldo_minimo=saldo_minimo,
        mensaje="Plantilla+desactivada",
    )


@router.post("/cartas-cobro/generar")
async def generar_cartas_cobro(
    request: Request,
    conjunto_id: int = Form(...),
    tipo_cartera: str = Form("PREJURIDICO"),
    plantilla_id: Optional[int] = Form(None),
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
            plantilla_id=plantilla_id,
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
            plantilla_id=plantilla_id,
            fecha_corte=corte.isoformat(),
            fecha_limite=limite.isoformat(),
            saldo_minimo=saldo_minimo,
            error="Selección de inmuebles inválida.",
        )

    try:
        cuerpo = None
        if plantilla_id:
            plantilla = cartas_cobro_service.obtener_plantilla(int(plantilla_id))
            if not plantilla or not plantilla.get("activo"):
                raise ValueError("Plantilla no disponible.")
            if str(plantilla.get("tipo_cartera") or "").upper() != (
                tipo_cartera or "PREJURIDICO"
            ).upper():
                raise ValueError("La plantilla no coincide con el tipo de cartera.")
            cuerpo = plantilla["cuerpo"]
        else:
            _, filtradas = _cargar_plantillas(tipo_cartera or "PREJURIDICO")
            if filtradas:
                cuerpo = filtradas[0]["cuerpo"]
            else:
                cuerpo = (
                    cartas_cobro_service.DEFAULT_CUERPO_JURIDICO
                    if (tipo_cartera or "").upper() == "JURIDICO"
                    else cartas_cobro_service.DEFAULT_CUERPO_PREJURIDICO
                )

        candidatos = cartas_cobro_service.listar_candidatos(
            conjunto_id=int(conjunto_id),
            tipo_cartera=tipo_cartera or "PREJURIDICO",
            fecha_corte=corte,
            saldo_minimo=float(saldo_minimo or 0),
        )
        seleccionados = cartas_cobro_service.resolver_seleccion(candidatos, inmueble_ids)
        contenido, filename, media_type = cartas_cobro_service.generar_paquete_docx(
            seleccionados,
            fecha_limite=limite,
            fecha_carta=hoy,
            cuerpo_plantilla=cuerpo,
        )
    except ValueError as exc:
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            plantilla_id=plantilla_id,
            fecha_corte=corte.isoformat(),
            fecha_limite=limite.isoformat(),
            saldo_minimo=saldo_minimo,
            error=str(exc),
        )
    except Exception:
        return _redirect(
            conjunto_id=conjunto_id,
            tipo_cartera=tipo_cartera,
            plantilla_id=plantilla_id,
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
