"""API M2M para integración con el agente inteligente de cobranza (WhatsApp)."""
import os
import io
import time
import secrets
import hashlib
import hmac
from pathlib import Path
from datetime import datetime, date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse

import liquidador
import exportaciones

router = APIRouter()

BOT_API_KEY = (os.getenv("LIQUIDADOR_API_KEY") or "").strip().strip('"').strip("'")
_TOKEN_TTL_SECONDS = int(os.getenv("BOT_PDF_URL_TTL", "900"))
_PDF_SECRET = (
    os.getenv("BOT_PDF_PUBLIC_SECRET")
    or BOT_API_KEY
    or ""
).strip().strip('"').strip("'")


def _public_base_url() -> str:
    return (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or ""
    ).rstrip("/")


def _require_api_key(request: Request) -> None:
    expected = BOT_API_KEY
    supplied = request.headers.get("X-API-Key")
    if not expected:
        raise HTTPException(status_code=503, detail="LIQUIDADOR_API_KEY no está configurada")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="No autorizado")


def _sign(filename: str, expires: int) -> str:
    payload = f"{filename}|{expires}".encode("utf-8")
    return hmac.new(_PDF_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_signed_pdf_url(filename: str, base_url: str) -> str:
    if not _PDF_SECRET:
        raise RuntimeError("BOT_PDF_PUBLIC_SECRET/LIQUIDADOR_API_KEY no está configurada")
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    token = _sign(filename, expires)
    return f"{base_url.rstrip('/')}/api/bot/pdf/{filename}?expires={expires}&token={token}"


def _parse_payload(payload: Any) -> tuple[int, date]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="El cuerpo debe ser JSON")

    try:
        inmueble_id = int(payload.get("inmueble_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="inmueble_id debe ser un entero")

    if inmueble_id <= 0:
        raise HTTPException(status_code=422, detail="inmueble_id debe ser mayor que cero")

    fecha_texto = str(payload.get("fecha_corte") or "")
    try:
        fecha_corte = datetime.strptime(fecha_texto, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="fecha_corte debe tener formato YYYY-MM-DD")

    if fecha_corte > date.today():
        raise HTTPException(status_code=422, detail="fecha_corte no puede ser futura")

    return inmueble_id, fecha_corte


def _obtener_datos_liquidacion(inmueble_id: int, fecha_corte: date) -> tuple[list[dict], dict, tuple]:
    resultados, resumen, inm_info = liquidador.motor_calculo_judicial(
        inmueble_id,
        "usura",
        2.5,
        23.8,
        0.0,
        fecha_corte,
    )
    if not inm_info:
        raise HTTPException(status_code=404, detail="No existe información del inmueble")
    if not resultados:
        raise HTTPException(status_code=404, detail="No hay movimientos para liquidar")
    return resultados, resumen, inm_info


def _resumen_tasas(resultados: list[dict]) -> tuple[list[dict], str]:
    tasas = []
    vistos = set()
    for fila in resultados:
        periodo = fila.get("periodo") or fila.get("mes")
        desde = fila.get("desde")
        tasa_ea = fila.get("tasa_ea")
        tasa_mes = fila.get("tasa_mes")
        clave = (str(periodo), str(desde), str(tasa_ea), str(tasa_mes))
        if clave in vistos:
            continue
        vistos.add(clave)
        tasas.append({
            "periodo": periodo or desde or "",
            "fecha_desde": desde or "",
            "tasa_ea": tasa_ea or "",
            "tasa_mensual": tasa_mes or "",
            "fuente": "SFC/Neon validada" if not fila.get("tasa_personalizada") else "Tasa personalizada E.A.",
        })
    fuente = "SFC/Neon validada"
    if any(f.get("tasa_personalizada") for f in resultados):
        fuente = "Tasa personalizada E.A."
    return tasas, fuente


def _generar_pdf_unificado(inmueble_id: int, fecha_corte: date, resultados: list[dict], resumen: dict, inm_info: tuple) -> str:
    base = _public_base_url()
    if not base:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL/RENDER_EXTERNAL_URL no está configurada")
    pdf_path = exportaciones.generar_pdf_liquidacion(inmueble_id, fecha_corte, resultados, resumen, inm_info)
    filename = os.path.basename(pdf_path)
    return build_signed_pdf_url(filename, base)


@router.post("/api/bot/liquidar")
async def liquidar_para_bot(request: Request):
    _require_api_key(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="El cuerpo debe ser JSON válido")

    inmueble_id, fecha_corte = _parse_payload(payload)

    try:
        resultados, resumen, inm_info = _obtener_datos_liquidacion(inmueble_id, fecha_corte)
        url_pdf = _generar_pdf_unificado(inmueble_id, fecha_corte, resultados, resumen, inm_info)
        tasas_aplicadas, fuente_tasas = _resumen_tasas(resultados)

        print(
            f"[BOT LIQUIDADOR] inmueble={inmueble_id} corte={fecha_corte} "
            f"capital={float(resumen.get('capital', 0)):.2f} "
            f"intereses={float(resumen.get('intereses', 0)):.2f} "
            f"total={float(resumen.get('gran_total', 0)):.2f} "
            f"fuente_tasas={fuente_tasas}",
            flush=True,
        )

        return JSONResponse({
            "status": "success",
            "mensaje": "Liquidación generada correctamente",
            "datos": {
                "inmueble_id": inmueble_id,
                "deudor": inm_info[2],
                "identificacion": inm_info[3],
                "capital": float(resumen["capital"]),
                "intereses": float(resumen["intereses"]),
                "honorarios_pct": float(resumen["honorarios_pct"]),
                "honorarios": float(resumen["honorarios"]),
                "gastos": float(resumen["gastos"]),
                "gran_total": float(resumen["gran_total"]),
                "total_exigible": float(resumen["gran_total"]),
                "tasas_aplicadas": tasas_aplicadas,
                "fuente_tasas": fuente_tasas,
                "detalle_mensual": resultados,
                "url_pdf": url_pdf,
            },
        })
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[BOT LIQUIDADOR] Error generando liquidación: {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="No fue posible generar la liquidación")


@router.get("/api/bot/pdf/{filename}", include_in_schema=False)
def servir_pdf_bot(filename: str, expires: int, token: str):
    if not _PDF_SECRET:
        raise HTTPException(status_code=503, detail="Servicio PDF no configurado")
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Enlace inválido")
    if expires_int < int(time.time()):
        raise HTTPException(status_code=410, detail="Enlace expirado")
    expected = _sign(filename, expires_int)
    if not token or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Enlace no autorizado")

    path = Path("static") / "pdfs" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=filename,
        headers={"Cache-Control": "private, max-age=900"},
    )
