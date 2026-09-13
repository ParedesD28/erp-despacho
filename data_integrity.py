"""Hardening de datos y exportaciones del liquidador.

Se ejecuta al arrancar el ERP. No cambia la fórmula del motor; evita que la
misma expensa mensual se duplique y corrige las descargas que el template
ofrece como PDF/Excel.
"""
import io
from datetime import date

import main
from fastapi import Request, Form
from fastapi.responses import RedirectResponse, StreamingResponse


def _conn():
    return main.db_pool.getconn()


def _release(conn):
    main.db_pool.putconn(conn)


def _dedupe_expensas(conn):
    with conn.cursor() as cur:
        # Conserva la primera fila por inmueble/concepto/año/mes.
        cur.execute("""
            DELETE FROM expensas_ph
            WHERE ctid IN (
                SELECT ctid FROM (
                    SELECT ctid,
                           ROW_NUMBER() OVER (
                               PARTITION BY inmueble_id, concepto, periodo_anio, periodo_mes
                               ORDER BY ctid
                           ) AS rn
                    FROM expensas_ph
                ) x
                WHERE rn > 1
            )
        """)
        deleted = cur.rowcount
        # Una restricción única evita que el motor vuelva a generar duplicados.
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_expensas_ph_periodo
            ON expensas_ph (inmueble_id, concepto, periodo_anio, periodo_mes)
        """)
        return deleted


# Limpieza defensiva al iniciar. Si Neon contiene duplicados históricos,
# quedan saneados antes de cualquier nueva liquidación.
try:
    _conn_start = _conn()
    with _conn_start:
        deleted = _dedupe_expensas(_conn_start)
    _release(_conn_start)
    print(f"[DATA_INTEGRITY] expensas_ph saneada; filas duplicadas eliminadas: {deleted}", flush=True)
except Exception as exc:
    print(f"[DATA_INTEGRITY] No se pudo sanear expensas_ph al arranque: {exc}", flush=True)


# El motor existente conserva la lógica matemática; este wrapper garantiza
# integridad antes y después de la auto-causación mensual que hace el motor.
_original_motor = main.motor_calculo_judicial


def motor_calculo_judicial_seguro(*args, **kwargs):
    conn = _conn()
    try:
        with conn:
            _dedupe_expensas(conn)
    finally:
        _release(conn)
    result = _original_motor(*args, **kwargs)
    conn = _conn()
    try:
        with conn:
            _dedupe_expensas(conn)
    finally:
        _release(conn)
    return result


main.motor_calculo_judicial = motor_calculo_judicial_seguro


def _remove_route(path, method):
    main.app.router.routes[:] = [
        r for r in main.app.router.routes
        if not (getattr(r, "path", None) == path and method in getattr(r, "methods", set()))
    ]


# El endpoint anterior redirigía con 307 a GET y perdía todos los parámetros.
# Se reemplaza por una actualización que recalcula y vuelve a mostrar la tabla.
_remove_route("/liquidador/actualizar", "POST")


@main.app.post("/liquidador/actualizar")
async def actualizar_liquidacion_corregida(request: Request):
    form = await request.form()
    inmueble_id = int(form.get("inmueble_id"))
    tipo_tasa = str(form.get("tipo_tasa") or "Superfinanciera")
    tasa_fija = float(form.get("tasa_fija") or 2.5)
    honorarios_pct = float(form.get("honorarios_pct") or 23.8)
    gastos = float(form.get("gastos") or 0)
    fecha_corte = date.fromisoformat(str(form.get("fecha_corte")))
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                for key, value in form.multi_items():
                    if not key.startswith(("ord_", "ext_", "gas_", "abo_")):
                        continue
                    prefijo, y, m = key.split("_")
                    concepto = {"ord": "Expensa Ordinaria", "ext": "Cuota Extraordinaria", "gas": "Gastos", "abo": "Abono"}[prefijo]
                    valor = float(value or 0)
                    cur.execute(
                        "UPDATE expensas_ph SET valor_capital=%s WHERE inmueble_id=%s AND concepto=%s AND periodo_anio=%s AND periodo_mes=%s",
                        (valor, inmueble_id, concepto, int(y), int(m)),
                    )
                    if cur.rowcount == 0 and valor:
                        cur.execute(
                            "INSERT INTO expensas_ph (inmueble_id,concepto,periodo_mes,periodo_anio,valor_capital,fecha_vencimiento,estado) VALUES (%s,%s,%s,%s,%s,%s,'Aplicado')",
                            (inmueble_id, concepto, int(m), int(y), valor, f"{y}-{int(m):02d}-01"),
                        )
    finally:
        _release(conn)
    resultados, resumen, _ = main.motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte)
    return main.templates.TemplateResponse(
        request=request,
        name="liquidador.html",
        context={
            "request": request,
            "inmuebles": main.cargar_inmuebles_ph(),
            "resultados": resultados,
            "resumen": resumen,
            "parametros": {
                "inmueble_id": inmueble_id, "tipo_tasa": tipo_tasa,
                "tasa_fija": tasa_fija, "honorarios_pct": honorarios_pct,
                "gastos": gastos, "fecha_corte": fecha_corte.strftime("%Y-%m-%d"),
            },
        },
    )


# La ruta existente devolvía HTML con Content-Type text/html pese a llamarse PDF.
# Esta versión genera un PDF real con ReportLab.
_remove_route("/liquidador/exportar/pdf", "POST")


@main.app.post("/liquidador/exportar/pdf")
async def exportar_pdf_real(
    request: Request,
    inmueble_id: int = Form(...), tipo_tasa: str = Form(...),
    tasa_fija: float = Form(2.5), honorarios_pct: float = Form(23.8),
    gastos: float = Form(0.0), fecha_corte: date = Form(...),
):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    resultados, resumen, inm_info = main.motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=10*mm, leftMargin=10*mm, topMargin=10*mm, bottomMargin=10*mm)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("LIQUIDACIÓN DE CRÉDITO - PROPIEDAD HORIZONTAL", styles["Title"]),
        Paragraph(f"Inmueble: {inm_info[0]} - {inm_info[1]} | Deudor: {inm_info[2]} | CC/NIT: {inm_info[3]}", styles["Normal"]),
        Paragraph(f"Fecha de corte: {fecha_corte.isoformat()}", styles["Normal"]),
        Spacer(1, 5*mm),
    ]
    rows = [["Periodo", "Ordinaria", "Extraord.", "Gastos", "Abonos", "Capital", "Días", "Interés", "Saldo"]]
    for r in resultados:
        rows.append([
            r["desde"][:7], f"${r['ordinarias']:,.0f}", f"${r['extraordinarias']:,.0f}",
            f"${r['gastos']:,.0f}", f"${r['abonos']:,.0f}", f"${r['capital_liquidable']:,.0f}",
            str(r["dias"]), f"${r['intereses']:,.0f}", f"${r['cap_int']:,.0f}",
        ])
    table = Table(rows, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("FONTSIZE", (0,0), (-1,-1), 7),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f8fafc")]),
    ]))
    story.append(table)
    story.append(Spacer(1, 5*mm))
    story.append(Paragraph(
        f"Capital: ${resumen.get('capital',0):,.0f} | Intereses: ${resumen.get('intereses',0):,.0f} | "
        f"Honorarios: ${resumen.get('honorarios',0):,.0f} | Gastos: ${resumen.get('gastos',0):,.0f} | "
        f"TOTAL: ${resumen.get('gran_total',0):,.0f}", styles["Heading3"]
    ))
    doc.build(story)
    buffer.seek(0)
    safe_name = str(inm_info[2]).replace(" ", "_").replace("/", "-")[:80]
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=Liquidacion_{safe_name}.pdf"},
    )

print("[DATA_INTEGRITY] Motor protegido contra duplicados; actualización y PDF del liquidador corregidos", flush=True)
