"""Diseño final y compacto del PDF de liquidación para web y bot."""

import os
import secrets
from datetime import datetime, date

from fastapi import Form, Request
from fastapi.responses import FileResponse

import main


def _money(v):
    try:
        return f"${float(v or 0):,.0f}"
    except (TypeError, ValueError):
        return "$0"


def generar_pdf_liquidacion_final(inmueble_id, fecha_corte, resultados, resumen, inm_info):
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
    width, height = landscape(A4)
    margin_x = 10 * mm

    styles = getSampleStyleSheet()
    title = ParagraphStyle("pdt", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=17, leading=20, textColor=colors.HexColor("#0f172a"), alignment=TA_LEFT)
    subtitle = ParagraphStyle("pds", parent=styles["Normal"], fontName="Helvetica", fontSize=7.5, leading=9, textColor=colors.HexColor("#64748b"))
    label = ParagraphStyle("pdl", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=6.3, leading=7.5, textColor=colors.HexColor("#64748b"))
    value = ParagraphStyle("pdv", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=9.5, textColor=colors.HexColor("#0f172a"))
    cell = ParagraphStyle("pdc", parent=styles["Normal"], fontName="Helvetica", fontSize=5.8, leading=6.8, textColor=colors.HexColor("#0f172a"))
    cell_b = ParagraphStyle("pdb", parent=cell, fontName="Helvetica-Bold")
    right = ParagraphStyle("pdr", parent=cell, alignment=TA_RIGHT)
    center = ParagraphStyle("pdcen", parent=cell, alignment=TA_CENTER)
    note = ParagraphStyle("pdn", parent=styles["Normal"], fontSize=6.2, leading=7.5, textColor=colors.HexColor("#64748b"))

    def P(v, style=cell):
        s = "" if v is None else str(v)
        return Paragraph(s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), style)

    doc = SimpleDocTemplate(
        path, pagesize=landscape(A4), leftMargin=margin_x, rightMargin=margin_x,
        topMargin=9 * mm, bottomMargin=11 * mm,
        title="Liquidación de Crédito Judicial", author="Gestión Judicial",
    )
    story = []

    conjunto = str(inm_info[0] or "SIN CONJUNTO")
    inmueble = str(inm_info[1] or "NO INFORMADO")
    deudor = str(inm_info[2] or "")
    identificacion = str(inm_info[3] or "")
    generado = datetime.now().strftime("%Y-%m-%d %H:%M")

    header = Table([
        [[Paragraph("GESTIÓN JUDICIAL", subtitle), Paragraph("LIQUIDACIÓN DE CRÉDITO JUDICIAL", title), Paragraph("Estado de cuenta oficial generado por el motor financiero central", subtitle)],
         [P("GENERACIÓN", label), P(generado, value), P("CORTE", label), P(fecha_corte.strftime("%Y-%m-%d"), value)]]
    ], colWidths=[width - 2 * margin_x - 62 * mm, 62 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, colors.HexColor("#0f172a")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [header, Spacer(1, 5)]

    identity = Table([
        [P("DEMANDANTE / CONJUNTO", label), P("INMUEBLE", label), P("DEUDOR", label), P("CC / NIT", label)],
        [P(conjunto, value), P(inmueble, value), P(deudor, value), P(identificacion, value)],
    ], colWidths=[74 * mm, 42 * mm, 78 * mm, 38 * mm])
    identity.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.45, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, 0), 4), ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("TOPPADDING", (0, 1), (-1, 1), 3), ("BOTTOMPADDING", (0, 1), (-1, 1), 5),
    ]))
    story += [identity, Spacer(1, 5)]

    resumen = resumen or {}
    boxes = [
        ("CAPITAL", _money(resumen.get("capital"))),
        ("INTERESES", _money(resumen.get("intereses"))),
        ("HONORARIOS", _money(resumen.get("honorarios"))),
        ("GASTOS", _money(resumen.get("gastos"))),
        ("GRAN TOTAL", _money(resumen.get("gran_total"))),
    ]
    box_cells = []
    for name, amount in boxes:
        amount_style = ParagraphStyle("amt", parent=value, fontSize=10.5 if name != "GRAN TOTAL" else 12, leading=12.5, alignment=TA_CENTER, textColor=colors.HexColor("#7f1d1d" if name == "GRAN TOTAL" else "#0f172a"))
        box_cells.append([P(name, label), Paragraph(amount, amount_style)])
    summary_table = Table([box_cells], colWidths=[54 * mm] * 5)
    summary_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.55, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (4, 0), (4, 0), colors.HexColor("#fff7ed")),
    ]))
    story += [summary_table, Spacer(1, 5), Paragraph("DETALLE MENSUAL", ParagraphStyle("sec", parent=label, fontSize=8.5, leading=10, textColor=colors.HexColor("#0f172a"))), Spacer(1, 2)]

    rows = [[
        P("Periodo", cell_b), P("Ordinaria", cell_b), P("Extraord.", cell_b), P("Gastos", cell_b),
        P("Abonos", cell_b), P("Capital", cell_b), P("Días", cell_b), P("Tasa EA", cell_b),
        P("Tasa mes", cell_b), P("Interés", cell_b), P("Int. acum.", cell_b), P("Saldo", cell_b),
    ]]
    for r in resultados or []:
        rows.append([
            P(str(r.get("desde", ""))[:7], cell_b), P(_money(r.get("ordinarias")), right), P(_money(r.get("extraordinarias")), right),
            P(_money(r.get("gastos")), right), P(_money(r.get("abonos")), right), P(_money(r.get("capital_liquidable")), right),
            P(r.get("dias"), center), P(r.get("tasa_ea"), center), P(r.get("tasa_mes"), center),
            P(_money(r.get("intereses")), right), P(_money(r.get("int_acumulado")), right), P(_money(r.get("cap_int")), right),
        ])

    widths = [19 * mm, 24 * mm, 25 * mm, 22 * mm, 22 * mm, 24 * mm, 13 * mm, 18 * mm, 19 * mm, 24 * mm, 27 * mm, 27 * mm]
    detail = LongTable(rows, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 4), ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 1), (-1, -1), 2.5),
    ]
    for idx in range(1, len(rows)):
        if idx % 2 == 0:
            style.append(("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#f8fafc")))
    detail.setStyle(TableStyle(style))
    story += [detail, Spacer(1, 5), Paragraph("La información financiera y las tasas aplicadas provienen del motor de liquidación central y de las fuentes validadas por el sistema. Documento generado a la fecha de corte indicada.", note)]

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
        canvas.line(margin_x, 7 * mm, width - margin_x, 7 * mm)
        canvas.setFont("Helvetica", 6.2)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(margin_x, 3.6 * mm, "Gestión Judicial | Estado de cuenta oficial")
        canvas.drawRightString(width - margin_x, 3.6 * mm, f"Página {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


# Publicar generador único para web y bot.
main.generar_pdf_liquidacion = generar_pdf_liquidacion_final
main.app.router.routes[:] = [
    r for r in main.app.router.routes
    if not (getattr(r, "path", None) == "/liquidador/exportar/pdf" and "POST" in getattr(r, "methods", set()))
]


@main.app.post("/liquidador/exportar/pdf")
async def exportar_pdf_final(
    request: Request,
    inmueble_id: int = Form(...),
    tipo_tasa: str = Form(...),
    tasa_fija: float = Form(2.5),
    honorarios_pct: float = Form(23.8),
    gastos: float = Form(0.0),
    fecha_corte: date = Form(...),
):
    resultados, resumen, inm_info = main.motor_calculo_judicial(
        inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte
    )
    path = generar_pdf_liquidacion_final(inmueble_id, fecha_corte, resultados, resumen, inm_info)
    return FileResponse(path, media_type="application/pdf", filename=f"Liquidacion_{inmueble_id}_{fecha_corte.isoformat()}.pdf")


print("[PDF FINAL] Formato A4 apaisado estructurado activado para web y bot", flush=True)
