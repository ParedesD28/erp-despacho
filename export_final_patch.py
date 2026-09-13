"""Parche final de exportaciones: PDF compacto judicial + Excel ejecutivo robusto."""

import io
import os
import secrets
from collections import defaultdict
from datetime import datetime

import pandas as pd
from fastapi import Form, Request
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import main


def _fmt_money(value):
    try:
        return f"${float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return "$0"


def _fmt_pct(value, decimals=2):
    try:
        return f"{float(value or 0):.{decimals}f}%"
    except (TypeError, ValueError):
        return "0%"


def _rows_as_dicts(cur):
    """Convierte cualquier cursor psycopg2 en diccionarios sin depender del tipo de cursor."""
    rows = cur.fetchall()
    description = getattr(cur, "description", None) or []
    names = [d[0] for d in description]
    return [dict(zip(names, row)) for row in rows]


def _esc(text):
    return (
        "" if text is None else str(text)
    ).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generar_pdf_liquidacion(inmueble_id, fecha_corte, resultados, resumen, inm_info):
    """Formato oficial compacto: identidad, resumen, detalle financiero y cierre."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    if not inm_info:
        raise ValueError("No existe informacion del inmueble")

    os.makedirs("static/pdfs", exist_ok=True)
    filename = f"Estado_Cuenta_{int(inmueble_id)}_{secrets.token_hex(8)}.pdf"
    path = os.path.join("static", "pdfs", filename)

    page_width, page_height = landscape(A4)
    left = right = 12 * mm
    top = 11 * mm
    bottom = 13 * mm
    usable = page_width - left - right

    styles = getSampleStyleSheet()
    title = ParagraphStyle("TitleERP", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=17, leading=19, textColor=colors.HexColor("#0f172a"), alignment=TA_LEFT, spaceAfter=1)
    kicker = ParagraphStyle("KickerERP", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7, leading=8, textColor=colors.HexColor("#475569"))
    label = ParagraphStyle("LabelERP", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=6.8, leading=8, textColor=colors.HexColor("#64748b"))
    value = ParagraphStyle("ValueERP", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.2, leading=9.5, textColor=colors.HexColor("#0f172a"))
    section = ParagraphStyle("SectionERP", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=9.5, leading=11, textColor=colors.HexColor("#0f172a"), spaceBefore=3, spaceAfter=4)
    cell = ParagraphStyle("CellERP", parent=styles["Normal"], fontName="Helvetica", fontSize=5.7, leading=6.5, textColor=colors.HexColor("#0f172a"))
    cell_bold = ParagraphStyle("CellBoldERP", parent=cell, fontName="Helvetica-Bold")
    cell_right = ParagraphStyle("CellRightERP", parent=cell, alignment=TA_RIGHT)
    cell_center = ParagraphStyle("CellCenterERP", parent=cell, alignment=TA_CENTER)
    note = ParagraphStyle("NoteERP", parent=styles["Normal"], fontSize=6.3, leading=7.5, textColor=colors.HexColor("#64748b"))
    signature = ParagraphStyle("SignatureERP", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=9, alignment=TA_CENTER, textColor=colors.HexColor("#0f172a"))

    def P(text, style=cell):
        return Paragraph(_esc(text), style)

    generado = datetime.now().strftime("%Y-%m-%d %H:%M")
    corte = fecha_corte.strftime("%Y-%m-%d")
    conjunto = str(inm_info[0] or "SIN INFORMAR")
    inmueble = str(inm_info[1] or "SIN INFORMAR")
    deudor = str(inm_info[2] or "SIN INFORMAR")
    identificacion = str(inm_info[3] or "SIN INFORMAR")

    doc = SimpleDocTemplate(
        path,
        pagesize=landscape(A4),
        leftMargin=left,
        rightMargin=right,
        topMargin=top,
        bottomMargin=bottom,
        title="Estado de Cuenta y Liquidación de Crédito",
        author="Gestión Judicial",
    )

    story = []

    # 1. Encabezado principal
    head = Table(
        [[
            [Paragraph("GESTIÓN JUDICIAL", kicker), Paragraph("ESTADO DE CUENTA Y LIQUIDACIÓN DE CRÉDITO", title), Paragraph("Documento generado por el sistema de liquidación financiera", note)],
            [P("FECHA DE GENERACIÓN", label), P(generado, value), Spacer(1, 2), P("FECHA DE CORTE", label), P(corte, value)],
        ]],
        colWidths=[usable - 50 * mm, 50 * mm],
    )
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, colors.HexColor("#0f172a")),
    ]))
    story.extend([head, Spacer(1, 6)])

    identity = Table([
        [P("DEMANDANTE / CONJUNTO", label), P("INMUEBLE", label), P("DEUDOR", label), P("CC / NIT", label)],
        [P(conjunto, value), P(inmueble, value), P(deudor, value), P(identificacion, value)],
    ], colWidths=[usable * 0.30, usable * 0.18, usable * 0.34, usable * 0.18])
    identity.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, 0), 4), ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 4), ("BOTTOMPADDING", (0, 1), (-1, 1), 5),
    ]))
    story.extend([identity, Spacer(1, 6)])

    # 2. Resumen de cartera
    resumen = resumen or {}
    summary_data = [[
        [P("CAPITAL", label), Paragraph(_esc(_fmt_money(resumen.get("capital"))), value)],
        [P("INTERESES", label), Paragraph(_esc(_fmt_money(resumen.get("intereses"))), value)],
        [P(f"HONORARIOS ({_fmt_pct(resumen.get('honorarios_pct', 0), 1)})", label), Paragraph(_esc(_fmt_money(resumen.get("honorarios"))), value)],
        [P("GASTOS", label), Paragraph(_esc(_fmt_money(resumen.get("gastos"))), value)],
        [P("TOTAL DE LA DEUDA", label), Paragraph(_esc(_fmt_money(resumen.get("gran_total"))), ParagraphStyle("TotalERP", parent=value, fontSize=11, alignment=TA_CENTER, textColor=colors.HexColor("#7f1d1d")))],
    ]]
    summary = Table(summary_data, colWidths=[usable / 5] * 5)
    summary.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (4, 0), (4, 0), colors.HexColor("#fff7ed")),
    ]))
    story.extend([summary, Spacer(1, 6), Paragraph("LIQUIDACIÓN DETALLADA", section)])

    # 3. Tabla detallada — 12 columnas, pero dimensionadas al ancho útil real.
    headers = ["PERÍODO", "ORD.", "EXTRA.", "GASTOS", "ABONOS", "CAP. LIQUIDABLE", "DÍAS", "TASA E.A.", "TASA MES", "INTERÉS MES", "INT. ACUM.", "SALDO FINAL"]
    detalle = [[P(h, cell_bold) for h in headers]]
    for fila in resultados or []:
        detalle.append([
            P(str(fila.get("desde", ""))[:7], cell_bold),
            P(_fmt_money(fila.get("ordinarias")), cell_right),
            P(_fmt_money(fila.get("extraordinarias")), cell_right),
            P(_fmt_money(fila.get("gastos")), cell_right),
            P(_fmt_money(fila.get("abonos")), cell_right),
            P(_fmt_money(fila.get("capital_liquidable")), cell_right),
            P(str(fila.get("dias", "")), cell_center),
            P(fila.get("tasa_ea", ""), cell_center),
            P(fila.get("tasa_mes", ""), cell_center),
            P(_fmt_money(fila.get("intereses")), cell_right),
            P(_fmt_money(fila.get("int_acumulado")), cell_right),
            P(_fmt_money(fila.get("cap_int")), cell_right),
        ])

    # 273 mm de ancho útil aproximado; evita que se corten columnas.
    widths = [17, 23, 23, 19, 21, 27, 12, 18, 19, 24, 26, 44]
    widths_mm = [w * mm for w in widths]
    table = LongTable(detalle, colWidths=widths_mm, repeatRows=1, splitByRow=1)
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.30, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, 0), 4), ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 1), (-1, -1), 2.5),
    ]
    for idx in range(1, len(detalle)):
        if idx % 2 == 0:
            table_style.append(("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#f8fafc")))
    table.setStyle(TableStyle(table_style))
    story.append(table)
    story.append(Spacer(1, 8))

    # 4. Cierre y firma
    close = Table([[Paragraph("FIRMA AUTORIZADA", signature), Paragraph("Liquidación elaborada por Sistema LegalTech", note)]], colWidths=[usable * 0.45, usable * 0.55])
    close.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (0, 0), 0.5, colors.HexColor("#64748b")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
    ]))
    story.append(close)

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
        canvas.line(left, 8 * mm, page_width - right, 8 * mm)
        canvas.setFont("Helvetica", 6.2)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(left, 4.5 * mm, "Gestión Judicial | Estado de cuenta y liquidación de crédito")
        canvas.drawRightString(page_width - right, 4.5 * mm, f"Página {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


_REPORT_HEADERS = [
    "radicado_interno", "radicado_rama", "naturaleza", "juzgado", "etapa_actual", "id_cliente",
    "demandado", "id_demandado", "estado", "pretensiones", "medidas_cautelares", "abogado_id", "Historial_Actuaciones",
]


def _formatear_historial_actuaciones(actuaciones):
    partes = []
    for a in actuaciones:
        fecha = a.get("fecha")
        fecha_texto = fecha.strftime("%Y-%m-%d") if hasattr(fecha, "strftime") else str(fecha or "")[:10]
        etapa = str(a.get("etapa") or "Actuación").strip()
        descripcion = str(a.get("descripcion") or a.get("tipificacion_sugerida") or "").strip()
        usuario = str(a.get("usuario") or "Sistema").strip()
        cuerpo = " - ".join(x for x in (etapa, descripcion) if x)
        if usuario:
            cuerpo = f"{cuerpo} (Por: {usuario})"
        partes.append(f"[{fecha_texto}] {cuerpo}")
    return "\n".join(partes)


def _estilizar_workbook(wb):
    dark = "0F172A"
    border_color = "CBD5E1"
    header_fill = PatternFill("solid", fgColor=dark)
    header_font = Font(color="FFFFFF", bold=True, size=10)
    thin = Side(style="thin", color=border_color)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for ws in wb.worksheets:
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for row in ws.iter_rows():
            for cell in row:
                cell.border = border
                if cell.row > 1:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
        ws.row_dimensions[1].height = 30
        for col_idx in range(1, ws.max_column + 1):
            letter = get_column_letter(col_idx)
            if ws.title == "Expedientes" and col_idx == 13:
                width = 90
            elif ws.title == "Expedientes" and col_idx in (7, 11):
                width = 40
            elif ws.title == "Actuaciones" and col_idx == 4:
                width = 70
            else:
                max_len = max((len(str(cell.value or "")) for cell in ws[letter]), default=12)
                width = min(max(max_len + 2, 12), 36)
            ws.column_dimensions[letter].width = width
        if ws.title == "Expedientes":
            for row in range(2, ws.max_row + 1):
                ws.row_dimensions[row].height = 90


def _build_executive_report(cur):
    cur.execute("""
        SELECT
            p.radicado_interno, p.radicado_rama, p.naturaleza, p.juzgado, p.etapa_actual,
            p.id_cliente,
            CASE WHEN NULLIF(TRIM(COALESCE(p.demandado, '')), '') IS NOT NULL THEN p.demandado
                 ELSE STRING_AGG(DISTINCT c_ddo.nombre, ' | ' ORDER BY c_ddo.nombre) END AS demandado,
            CASE WHEN NULLIF(TRIM(COALESCE(p.id_demandado, '')), '') IS NOT NULL THEN p.id_demandado
                 ELSE STRING_AGG(DISTINCT pl.identificacion_demandado, ' | ' ORDER BY pl.identificacion_demandado) END AS id_demandado,
            p.estado, p.pretensiones, p.medidas_cautelares, p.abogado_id
        FROM procesos p
        LEFT JOIN procesos_litisconsorcio pl ON pl.radicado_interno = p.radicado_interno
        LEFT JOIN contactos c_ddo ON c_ddo.identificacion = pl.identificacion_demandado
        GROUP BY p.radicado_interno, p.radicado_rama, p.naturaleza, p.juzgado, p.etapa_actual,
                 p.id_cliente, p.demandado, p.id_demandado, p.estado, p.pretensiones, p.medidas_cautelares, p.abogado_id
        ORDER BY p.radicado_interno DESC
    """)
    procesos = _rows_as_dicts(cur)

    cur.execute("""
        SELECT id, radicado_interno, fecha, etapa, descripcion, usuario, tipificacion_sugerida
        FROM actuaciones
        ORDER BY radicado_interno DESC, fecha ASC, id ASC
    """)
    actuaciones = _rows_as_dicts(cur)

    history = defaultdict(list)
    for act in actuaciones:
        history[str(act.get("radicado_interno") or "")].append(act)

    rows = []
    for p in procesos:
        radicado = str(p.get("radicado_interno") or "")
        item = {key: p.get(key) for key in _REPORT_HEADERS[:-1]}
        item["Historial_Actuaciones"] = _formatear_historial_actuaciones(history.get(radicado, []))
        rows.append(item)
    return rows, actuaciones


def generar_informe_ejecutivo_excel(conn):
    with conn.cursor() as cur:
        expedientes, actuaciones = _build_executive_report(cur)
        cur.execute("SELECT * FROM contactos ORDER BY nombre ASC")
        contactos = _rows_as_dicts(cur)
        cur.execute("SELECT * FROM inmuebles_ph ORDER BY id")
        inmuebles = _rows_as_dicts(cur)
        cur.execute("SELECT * FROM gestiones_crm ORDER BY fecha DESC")
        gestiones = _rows_as_dicts(cur)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(expedientes, columns=_REPORT_HEADERS).to_excel(writer, index=False, sheet_name="Expedientes")
        pd.DataFrame(actuaciones).to_excel(writer, index=False, sheet_name="Actuaciones")
        pd.DataFrame(contactos).to_excel(writer, index=False, sheet_name="Contactos")
        pd.DataFrame(inmuebles).to_excel(writer, index=False, sheet_name="Inmuebles")
        pd.DataFrame(gestiones).to_excel(writer, index=False, sheet_name="CRM")
        wb = writer.book
        _estilizar_workbook(wb)
        for ws_name in ("Actuaciones", "CRM"):
            ws = wb[ws_name]
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if hasattr(cell.value, "strftime"):
                        cell.number_format = "yyyy-mm-dd"
        wb.properties.title = "Reporte Ejecutivo de Expedientes y Actuaciones"
        wb.properties.subject = "Resumen integral de expedientes con historial completo de actuaciones"
        wb.properties.creator = "Gestión Judicial"
        wb.properties.description = "Incluye expediente, actuaciones, contactos, inmuebles y CRM."
    output.seek(0)
    return output


# Reemplazo determinista de las rutas de exportación anteriores.
main.app.router.routes[:] = [
    r for r in main.app.router.routes
    if getattr(r, "path", None) not in {"/liquidador/exportar/pdf", "/descargar-excel"}
]


@main.app.post("/liquidador/exportar/pdf")
async def exportar_pdf_final(
    request: Request,
    inmueble_id: int = Form(...),
    tipo_tasa: str = Form(...),
    tasa_fija: float = Form(2.5),
    honorarios_pct: float = Form(23.8),
    gastos: float = Form(0.0),
    fecha_corte=None,
):
    if isinstance(fecha_corte, str):
        from datetime import datetime as _dt
        fecha_corte = _dt.strptime(fecha_corte, "%Y-%m-%d").date()
    if fecha_corte is None:
        from datetime import date as _date
        fecha_corte = _date.today()
    resultados, resumen, inm_info = main.motor_calculo_judicial(
        inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte
    )
    path = generar_pdf_liquidacion(inmueble_id, fecha_corte, resultados, resumen, inm_info)
    return FileResponse(path, media_type="application/pdf", filename=f"Liquidacion_{inmueble_id}_{fecha_corte.isoformat()}.pdf")


@main.app.get("/descargar-excel")
def descargar_excel_final():
    c = main.db_pool.getconn()
    try:
        output = generar_informe_ejecutivo_excel(c)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=reporte_ejecutivo_expedientes.xlsx"},
        )
    finally:
        main.db_pool.putconn(c)


main.generar_pdf_liquidacion = generar_pdf_liquidacion
print("[EXPORT_FINAL_PATCH] PDF estructurado + Excel ejecutivo robusto activados", flush=True)
