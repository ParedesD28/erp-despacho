import os
import io
from datetime import datetime, date
from typing import Any

import psycopg2
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

BOT_API_KEY = os.getenv("LIQUIDADOR_API_KEY")
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")


def _require_api_key(request: Request) -> None:
    expected = BOT_API_KEY
    supplied = request.headers.get("X-API-Key")
    if not expected:
        raise HTTPException(status_code=503, detail="LIQUIDADOR_API_KEY no esta configurada")
    if not supplied or supplied != expected:
        raise HTTPException(status_code=401, detail="No autorizado")


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

    return inmueble_id, fecha_corte


def _obtener_datos_liquidacion(inmueble_id: int, fecha_corte: date) -> tuple[dict, dict, tuple]:
    # Import local para evitar dependencias circulares durante el arranque de main.py.
    import main

    resultados, resumen, inm_info = main.motor_calculo_judicial(
        inmueble_id=inmueble_id,
        tipo_tasa="usura",
        tasa_fija=2.5,
        honorarios_pct=23.8,
        gastos_globales=0.0,
        fecha_corte=fecha_corte,
    )

    if not inm_info:
        raise HTTPException(status_code=404, detail="No existe informacion del inmueble")
    if not resultados:
        raise HTTPException(status_code=404, detail="No hay movimientos para liquidar")

    return resultados, resumen, inm_info


def _generar_pdf(inmueble_id: int, fecha_corte: date, resultados: list[dict], resumen: dict, inm_info: tuple) -> str:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors

    if not PUBLIC_BASE_URL:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL/RENDER_EXTERNAL_URL no esta configurada")

    os.makedirs("static/pdfs", exist_ok=True)
    nombre_pdf = f"Estado_Cuenta_{inmueble_id}_{fecha_corte.isoformat()}.pdf"
    ruta = os.path.join("static", "pdfs", nombre_pdf)

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(ruta, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    elementos = [
        Paragraph("ESTADO DE CUENTA OFICIAL", styles["Title"]),
        Spacer(1, 10),
        Paragraph(f"Conjunto / Inmueble: {inm_info[0]} - {inm_info[1]}", styles["BodyText"]),
        Paragraph(f"Deudor: {inm_info[2]}", styles["BodyText"]),
        Paragraph(f"Identificacion: {inm_info[3]}", styles["BodyText"]),
        Paragraph(f"Inmueble ID: {inmueble_id}", styles["BodyText"]),
        Paragraph(f"Fecha de corte: {fecha_corte.isoformat()}", styles["BodyText"]),
        Spacer(1, 14),
    ]

    def dinero(valor):
        return f"${float(valor or 0):,.0f}"

    resumen_data = [
        ["Concepto", "Valor"],
        ["Capital", dinero(resumen.get("capital"))],
        ["Intereses de mora", dinero(resumen.get("intereses"))],
        [f"Honorarios ({float(resumen.get('honorarios_pct', 0)):g}%)", dinero(resumen.get("honorarios"))],
        ["Gastos procesales", dinero(resumen.get("gastos"))],
        ["GRAN TOTAL", dinero(resumen.get("gran_total"))],
    ]
    tabla = Table(resumen_data, colWidths=[330, 130])
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (1, 1), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elementos.extend([tabla, Spacer(1, 16), Paragraph("Detalle mensual", styles["Heading2"])])

    detalle = [["Periodo", "Capital", "Interes", "Capital + Interes"]]
    for fila in resultados:
        detalle.append([
            str(fila.get("periodo", fila.get("mes", ""))),
            dinero(fila.get("capital", fila.get("cap_mes", 0))),
            dinero(fila.get("interes", fila.get("intereses", 0))),
            dinero(fila.get("cap_int", 0)),
        ])

    tabla_detalle = Table(detalle, repeatRows=1, colWidths=[130, 110, 110, 110])
    tabla_detalle.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elementos.append(tabla_detalle)
    doc.build(elementos)

    return f"{PUBLIC_BASE_URL}/static/pdfs/{nombre_pdf}"


async def liquidar_para_bot(request: Request):
    _require_api_key(request)
    payload = await request.json()
    inmueble_id, fecha_corte = _parse_payload(payload)

    try:
        resultados, resumen, inm_info = _obtener_datos_liquidacion(inmueble_id, fecha_corte)
        url_pdf = _generar_pdf(inmueble_id, fecha_corte, resultados, resumen, inm_info)

        return JSONResponse({
            "status": "success",
            "mensaje": "Liquidacion generada correctamente",
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
                "url_pdf": url_pdf,
            },
        })
    except HTTPException:
        raise
    except (psycopg2.Error, ValueError, KeyError, IndexError) as exc:
        print(f"[BOT LIQUIDADOR] Error de datos: {repr(exc)}", flush=True)
        raise HTTPException(status_code=500, detail="No fue posible generar la liquidacion")
    except Exception as exc:
        print(f"[BOT LIQUIDADOR] Error inesperado: {repr(exc)}", flush=True)
        raise HTTPException(status_code=500, detail="No fue posible generar la liquidacion")
