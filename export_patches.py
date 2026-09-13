"""Exportaciones de producción: PDF judicial unificado e informe ejecutivo completo."""

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


# -----------------------------------------------------------------------------
# PDF UNIFICADO
# -----------------------------------------------------------------------------
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


def generar_pdf_liquidacion(inmueble_id, fecha_corte, resultados, resumen, inm_info):
    """Genera el PDF oficial que usan tanto la web como el bot."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase import pdfmetrics
    from reportlab.platypus import (
        KeepTogether,
        LongTable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    if not inm_info:
        raise ValueError("No existe informacion del inmueble")

    os.makedirs("static/pdfs", exist_ok=True)
    filename = f"Estado_Cuenta_{int(inmueble_id)}_{secrets.token_hex(8)}.pdf"
    path = os.path.join("static", "pdfs", filename)

    page_width, page_height = landscape(A4)
    left = right = 12 * mm
    top = 11 * mm
    bottom = 12 * mm

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ERPTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=18, leading=21, alignment=TA_LEFT, textColor=colors.HexColor("#0f172a"),
        spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "ERPSubtitle", parent=styles["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=colors.HexColor("#475569"),
    )
    label_style = ParagraphStyle(
        "ERPLabel", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=7, leading=8.5, textColor=colors.HexColor("#64748b"),
    )
    value_style = ParagraphStyle(
        "ERPValue", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=8.5, leading=10.5, textColor=colors.HexColor("#0f172a"),
    )
    section_style = ParagraphStyle(
        "ERPSection", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=10, leading=12, textColor=colors.HexColor("#0f172a"), spaceBefore=5, spaceAfter=6,
    )
    cell_style = ParagraphStyle(
        "ERPCell", parent=styles["Normal"], fontName="Helvetica",
        fontSize=6.2, leading=7.2, textColor=colors.HexColor("#0f172a"),
    )
    cell_bold_style = ParagraphStyle(
        "ERPCellBold", parent=cell_style, fontName="Helvetica-Bold",
    )
    cell_right_style = ParagraphStyle(
        "ERPCellRight", parent=cell_style, alignment=TA_RIGHT,
    )
    cell_center_style = ParagraphStyle(
        "ERPCellCenter", parent=cell_style, alignment=TA_CENTER,
    )
    small_style = ParagraphStyle(
        "ERPSmall", parent=styles["Normal"], fontSize=6.5, leading=8,
        textColor=colors.HexColor("#64748b"),
    )

    def P(text, style=cell_style):
        safe = "" if text is None else str(text)
        return Paragraph(safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), style)

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    conjunto = str(inm_info[0] or "SIN CONJUNTO")
    torre_apto = str(inm_info[1] or "")
    deudor = str(inm_info[2] or "")
    identificacion = str(inm_info[3] or "")

    doc = SimpleDocTemplate(
        path, pagesize=landscape(A4), leftMargin=left, rightMargin=right,
        topMargin=top, bottomMargin=bottom,
        title="Liquidación de Crédito Judicial",
        author="Gestión Judicial",
    )

    story = []

    header_left = [
        Paragraph("GESTIÓN JUDICIAL", subtitle_style),
        Paragraph("LIQUIDACIÓN DE CRÉDITO JUDICIAL", title_style),
        Paragraph("Estado de cuenta oficial generado por el motor financiero central", subtitle_style),
    ]
    header_right = [
        P("FECHA DE GENERACIÓN", label_style),
        P(generated, value_style),
        Spacer(1, 3),
        P("FECHA DE CORTE", label_style),
        P(fecha_corte.strftime("%Y-%m-%d"), value_style),
    ]
    header = Table([[header_left, header_right]], colWidths=[page_width - left - right - 55 * mm, 55 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, -1), 1.4, colors.HexColor("#0f172a")),
    ]))
    story.append(header)
    story.append(Spacer(1, 7))

    identity_data = [
        [P("DEMANDANTE / CONJUNTO", label_style), P("INMUEBLE", label_style), P("DEUDOR", label_style), P("CC / NIT", label_style)],
        [P(conjunto, value_style), P(torre_apto or "No informado", value_style), P(deudor, value_style), P(identificacion, value_style)],
    ]
    identity = Table(identity_data, colWidths=[page_width * 0.29, page_width * 0.17, page_width * 0.34, page_width * 0.20])
    identity.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
    ]))
    story.append(identity)
    story.append(Spacer(1, 8))

    resumen = resumen or {}
    summary_values = [
        ("CAPITAL", _fmt_money(resumen.get("capital"))),
        ("INTERESES DE MORA", _fmt_money(resumen.get("intereses"))),
        (f"HONORARIOS ({_fmt_pct(resumen.get('honorarios_pct', 0), 1)})", _fmt_money(resumen.get("honorarios"))),
        ("GASTOS PROCESALES", _fmt_money(resumen.get("gastos"))),
        ("GRAN TOTAL", _fmt_money(resumen.get("gran_total"))),
    ]
    summary_cells = []
    for label, value in summary_values:
        accent = label == "GRAN TOTAL"
        block_style = ParagraphStyle(
            f"Summary_{label}", parent=value_style, fontSize=12 if accent else 10,
            leading=14 if accent else 12, alignment=TA_CENTER,
            textColor=colors.HexColor("#7f1d1d" if accent else "#0f172a"),
        )
        summary_cells.append([Paragraph(label, label_style), Paragraph(value, block_style)])
    summary = Table([summary_cells], colWidths=[(page_width - left - right) / 5.0] * 5)
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (4, 0), (4, 0), colors.HexColor("#fff7ed")),
        ("BOX", (4, 0), (4, 0), 0.8, colors.HexColor("#f59e0b")),
    ]))
    story.append(summary)
    story.append(Spacer(1, 8))
    story.append(Paragraph("DETALLE DE LIQUIDACIÓN", section_style))

    header = [
        P("Período", cell_bold_style), P("Ordinaria", cell_bold_style), P("Extraordinaria", cell_bold_style),
        P("Gastos", cell_bold_style), P("Abonos", cell_bold_style), P("Capital liquidable", cell_bold_style),
        P("Días", cell_bold_style), P("Tasa E.A.", cell_bold_style), P("Tasa mes", cell_bold_style),
        P("Interés mes", cell_bold_style), P("Interés acumulado", cell_bold_style), P("Saldo final", cell_bold_style),
    ]
    detalle = [header]
    for fila in resultados or []:
        detalle.append([
            P(str(fila.get("desde", ""))[:7], cell_bold_style),
            P(_fmt_money(fila.get("ordinarias")), cell_right_style),
            P(_fmt_money(fila.get("extraordinarias")), cell_right_style),
            P(_fmt_money(fila.get("gastos")), cell_right_style),
            P(_fmt_money(fila.get("abonos")), cell_right_style),
            P(_fmt_money(fila.get("capital_liquidable")), cell_right_style),
            P(str(fila.get("dias", "")), cell_center_style),
            P(str(fila.get("tasa_ea", "")), cell_center_style),
            P(str(fila.get("tasa_mes", "")), cell_center_style),
            P(_fmt_money(fila.get("intereses")), cell_right_style),
            P(_fmt_money(fila.get("int_acumulado")), cell_right_style),
            P(_fmt_money(fila.get("cap_int")), cell_right_style),
        ])

    col_widths = [
        21 * mm, 29 * mm, 31 * mm, 25 * mm, 25 * mm, 31 * mm,
        13 * mm, 20 * mm, 21 * mm, 29 * mm, 31 * mm, 31 * mm,
    ]
    table = LongTable(detalle, colWidths=col_widths, repeatRows=1)
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
    ]
    for idx in range(1, len(detalle)):
        if idx % 2 == 0:
            table_style.append(("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#f8fafc")))
    table.setStyle(TableStyle(table_style))
    story.append(table)
    story.append(Spacer(1, 7))
    story.append(Paragraph(
        "La información financiera y las tasas aplicadas provienen del motor de liquidación central y de las fuentes validadas configuradas por el sistema. Este documento corresponde a la fecha de corte indicada.",
        small_style,
    ))

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
        canvas.line(left, 8 * mm, page_width - right, 8 * mm)
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(left, 4.5 * mm, "Gestión Judicial | Estado de cuenta oficial")
        canvas.drawRightString(page_width - right, 4.5 * mm, f"Página {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


# -----------------------------------------------------------------------------
# INFORME EJECUTIVO COMPLETO DE EXPEDIENTES + ACTUACIONES
# -----------------------------------------------------------------------------
_REPORT_HEADERS = [
    "radicado_interno", "radicado_rama", "naturaleza", "juzgado", "etapa_actual",
    "id_cliente", "demandado", "id_demandado", "estado", "pretensiones",
    "medidas_cautelares", "abogado_id", "Historial_Actuaciones",
]


def _formatear_historial_actuaciones(actuaciones):
    partes = []
    for a in actuaciones:
        fecha = a.get("fecha")
        if hasattr(fecha, "strftime"):
            fecha_texto = fecha.strftime("%Y-%m-%d")
        else:
            fecha_texto = str(fecha or "")[:10]
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
        if ws.max_row >= 1:
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
        ws.row_dimensions[1].height = 28

        for col_idx in range(1, ws.max_column + 1):
            letter = get_column_letter(col_idx)
            max_len = 0
            for cell in ws[letter]:
                value = "" if cell.value is None else str(cell.value)
                max_len = max(max_len, max((len(line) for line in value.splitlines()), default=0))
            if ws.title == "Expedientes" and col_idx == 13:
                width = 72
            elif ws.title == "Expedientes" and col_idx in (7, 11):
                width = 38
            elif ws.title == "Actuaciones" and col_idx == 4:
                width = 65
            else:
                width = min(max(max_len + 2, 12), 36)
            ws.column_dimensions[letter].width = width

        if ws.title == "Expedientes":
            for row in range(2, ws.max_row + 1):
                ws.row_dimensions[row].height = 64


def _build_executive_report(cur):
    cur.execute("""
        SELECT
            p.radicado_interno,
            p.radicado_rama,
            p.naturaleza,
            p.juzgado,
            p.etapa_actual,
            p.id_cliente,
            CASE
                WHEN NULLIF(TRIM(COALESCE(p.demandado, '')), '') IS NOT NULL THEN p.demandado
                ELSE STRING_AGG(DISTINCT c_ddo.nombre, ' | ' ORDER BY c_ddo.nombre)
            END AS demandado,
            CASE
                WHEN NULLIF(TRIM(COALESCE(p.id_demandado, '')), '') IS NOT NULL THEN p.id_demandado
                ELSE STRING_AGG(DISTINCT pl.identificacion_demandado, ' | ' ORDER BY pl.identificacion_demandado)
            END AS id_demandado,
            p.estado,
            p.pretensiones,
            p.medidas_cautelares,
            p.abogado_id
        FROM procesos p
        LEFT JOIN procesos_litisconsorcio pl ON pl.radicado_interno = p.radicado_interno
        LEFT JOIN contactos c_ddo ON c_ddo.identificacion = pl.identificacion_demandado
        GROUP BY
            p.radicado_interno, p.radicado_rama, p.naturaleza, p.juzgado,
            p.etapa_actual, p.id_cliente, p.demandado, p.id_demandado,
            p.estado, p.pretensiones, p.medidas_cautelares, p.abogado_id
        ORDER BY p.radicado_interno DESC
    """)
    procesos = [dict(row) for row in cur.fetchall()]

    cur.execute("""
        SELECT id, radicado_interno, fecha, etapa, descripcion, usuario, tipificacion_sugerida
        FROM actuaciones
        ORDER BY radicado_interno DESC, fecha DESC, id DESC
    """)
    actuaciones = [dict(row) for row in cur.fetchall()]

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
        contactos = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT * FROM inmuebles_ph ORDER BY id")
        inmuebles = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT * FROM gestiones_crm ORDER BY fecha DESC")
        gestiones = [dict(row) for row in cur.fetchall()]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(expedientes, columns=_REPORT_HEADERS).to_excel(writer, index=False, sheet_name="Expedientes")
        pd.DataFrame(actuaciones).to_excel(writer, index=False, sheet_name="Actuaciones")
        pd.DataFrame(contactos).to_excel(writer, index=False, sheet_name="Contactos")
        pd.DataFrame(inmuebles).to_excel(writer, index=False, sheet_name="Inmuebles")
        pd.DataFrame(gestiones).to_excel(writer, index=False, sheet_name="CRM")

        wb = writer.book
        _estilizar_workbook(wb)

        # Formatos de fecha donde corresponda.
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


# -----------------------------------------------------------------------------
# Registro de rutas: reemplaza exclusivamente las exportaciones afectadas.
# -----------------------------------------------------------------------------
main.app.router.routes[:] = [
    r for r in main.app.router.routes
    if not (
        getattr(r, "path", None) in {"/liquidador/exportar/pdf", "/descargar-excel"}
        and "POST" in getattr(r, "methods", set())
        or getattr(r, "path", None) == "/descargar-excel"
    )
]


@main.app.post("/liquidador/exportar/pdf")
async def exportar_pdf_unificado(
    request: Request,
    inmueble_id: int = Form(...),
    tipo_tasa: str = Form(...),
    tasa_fija: float = Form(2.5),
    honorarios_pct: float = Form(23.8),
    gastos: float = Form(0.0),
    fecha_corte=None,
):
    # FastAPI parsea Form(date) de forma transparente; usamos texto aquí para
    # tolerar tanto YYYY-MM-DD como la fecha ya convertida por otros clientes.
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
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"Liquidacion_{inmueble_id}_{fecha_corte.isoformat()}.pdf",
    )


# Ruta utilizada por la tarjeta Informes.
@main.app.get("/descargar-excel")
def descargar_excel_ejecutivo():
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


# Exponemos el generador para que bot_api.py utilice exactamente el mismo formato.
main.generar_pdf_liquidacion = generar_pdf_liquidacion

print("[EXPORT_PATCHES] PDF judicial unificado + Excel ejecutivo con historial completo de actuaciones", flush=True)
