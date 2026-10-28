"""Rutas del cargador de facturación mensual COLON."""
from __future__ import annotations

import json
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates

from facturacion_mensual_parser import recortar_media_pagina_png
from facturacion_mensual_service import (
    FacturacionMensualError,
    MAX_PDF_BYTES,
    aplicar_lote,
    obtener_lote,
    preview_pdf,
)

router = APIRouter(tags=["Facturación mensual"])
templates = Jinja2Templates(directory="templates")


def _render(name: str, context: dict, status_code: int = 200):
    try:
        return templates.TemplateResponse(name, context, status_code=status_code)
    except TypeError:
        return templates.TemplateResponse(
            request=context.get("request"), name=name, context=context, status_code=status_code
        )


@router.get("/herramientas/facturacion-mensual")
def vista_facturacion_mensual(request: Request):
    return _render(
        "facturacion_mensual.html",
        {"request": request, "max_mb": MAX_PDF_BYTES // (1024 * 1024)},
    )


@router.post("/api/facturacion-mensual/preview")
async def preview_facturacion_mensual(archivo: UploadFile = File(...)):
    data = await archivo.read()
    try:
        preview = preview_pdf(data, filename=archivo.filename or "")
    except FacturacionMensualError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el PDF: {exc}") from exc
    return JSONResponse(preview)


@router.get("/api/facturacion-mensual/factura/{lote_id}/{page}/{half}")
def ver_media_factura(lote_id: str, page: int, half: str):
    lote = obtener_lote(lote_id)
    if not lote:
        raise HTTPException(status_code=404, detail="La pre-vista venció. Vuelva a subir el PDF.")
    try:
        png = recortar_media_pagina_png(lote["pdf_bytes"], page, half)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo recortar la factura: {exc}") from exc
    return Response(content=png, media_type="image/png")


@router.post("/api/facturacion-mensual/aplicar")
async def aplicar_facturacion_mensual(
    request: Request,
    lote_id: str = Form(...),
    confirmar_sanciones: str = Form(""),
):
    sanciones: set[int] = set()
    raw = (confirmar_sanciones or "").strip()
    if raw:
        try:
            parsed = json.loads(raw) if raw.startswith("[") else raw.split(",")
        except json.JSONDecodeError:
            parsed = raw.split(",")
        for item in parsed:
            try:
                sanciones.add(int(item))
            except (TypeError, ValueError):
                continue
    usuario = str(getattr(request.state, "user_id", "") or "")
    try:
        resultado = aplicar_lote(lote_id, usuario_id=usuario, confirmar_sanciones=sanciones)
    except FacturacionMensualError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(resultado)
