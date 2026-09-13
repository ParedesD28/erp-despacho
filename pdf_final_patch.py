"""Diseño definitivo y legible del PDF de liquidación para web y bot."""

import os
import secrets
from datetime import datetime, date

from fastapi import Form, Request
from fastapi.responses import FileResponse

import main


def _money(value):
    try:
        return f"${float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return "$0"


def _pct(value, decimals=4):
    try:
        return f"{float(value or 0):.{decimals}f}%"
    except (TypeError, ValueError):
        return "0.0000%"


def generar_pdf_liquidacion_final(inmueble_id, fecha_corte, resultados, resumen, inm_info):
    """Genera el formato oficial de liquidación usado por la web y por el bot."""
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

    page_width, _ = landscape(A4)
    margin_x = 11 * mm
    usable_width = page_width - (2 * margin_x)

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "PDFTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=17, leading=19, alignment=TA_LEFT,
        textColor=colors.HexColor("#0f172a"), spaceAfter=1,
    )
    subtitle = ParagraphStyle(
        "PDFSubtitle", parent=styles["Normal"], fontName="Helvetica",
        fontSize=7.4, leading=9, textColor=colors.HexColor("#64748b"),
    )
    label = ParagraphStyle(
        "PDFLabel", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=6.5, leading=7.5, textColor=colors.HexColor("#475569"),
    )
    value = ParagraphStyle(
        "PDFValue", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=8.3, leading=9.6, textColor=colors.HexColor("#0f172a"),
    )
    section = ParagraphStyle(
        "PDFSection", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=8.8, leading=10.5, textColor=colors.HexColor("#0f172a"),
        spaceBefore=4, spaceAfter=4,
    )
    cell = ParagraphStyle(
        "PDFCell", parent=styles["Normal"], fontName="Helvetica",
        fontSize=5.55, leading=6.5, textColor=colors.HexColor("#0f172a"),
    )
    cell_b = ParagraphStyle("PDFCellBold", parent=cell, fontName="Helvetica-Bold")
    right = ParagraphStyle("PDFRight", parent=cell, alignment=TA_RIGHT)
    center = ParagraphStyle("PDFCenter", parent=cell, alignment=TA_CENTER)
    note = ParagraphStyle(
        "PDFNote", parent=styles["Normal"], fontSize=6.2, leading=7.5,
        textColor=colors.HexColor("#64748b"),
    )
    signature = ParagraphStyle(
        "PDFSignature", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=7.2, leading=8.5, alignment=TA_CENTER, textColor=colors.HexColor("#0f172a"),
    )

    def P(value_, style=cell):
        safe = "" if value_ is None else str(value_)
        return Paragraph(
            safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
            style,
        )

    conjunto = str(inm_info[0] or "SIN CONJUNTO")
    inmueble = str(inm_info[1] or "NO INFORMADO")
    deudor = str(inm_info[2] or "")
    identificacion = str(inm_info[3] or "")
    generado = datetime.now().strftime("%Y-%m-%d %H:%M")
    corte = fecha_corte.strftime("%Y-%m-%d")
    resumen = resumen or {}

    doc = SimpleDocTemplate(
        path,
        pagesize=landscape(A4),
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=9 * mm,
        bottomMargin=12 * mm,
        title="Liquidación de Crédito Judicial",
        author="Gestión Judicial",
    )

    story = []

    # 1. ENCABEZADO PRINCIPAL
    title_block = Table(
        [[
            [
                Paragraph("GESTIÓN JUDICIAL", subtitle),
                Paragraph("LIQUIDACIÓN DE CRÉDITO JUDICIAL", title),
                Paragraph("Estado de cuenta e informe financiero judicial", subtitle),
            ],
            [
                P("RADICADO / INMUEBLE", label),
                P(inmueble, value),
            ],
        ]],
        colWidths=[usable_width * 0.72, usable_width * 0.28],
    )
    title_block.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 1.3, colors.HexColor("#0f172a")),
    ]))
    story += [title_block, Spacer(1, 5)]

    identity = Table([
        [
            P("DEMANDANTE / CONJUNTO", label),
            P("DEUDOR", label),
            P("CC / NIT", label),
        ],
        [
            P(conjunto, value),
            P(deudor, value),
            P(identificacion, value),
        ],
    ], colWidths=[usable_width * 0.48, usable_width * 0.34, usable_width * 0.18])
    identity.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.45, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("TOPPADDING", (0, 1), (-1, 1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 5),
    ]))
    story += [identity, Spacer(1, 5)]

    # 2. RESUMEN DE CARTERA
    story.append(Paragraph("RESUMEN DE CARTERA", section))
    summary_boxes = [
        ("CAPITAL", _money(resumen.get("capital"))),
        ("INTERESES", _money(resumen.get("intereses"))),
        ("HONORARIOS", _money(resumen.get("honorarios"))),
        ("GASTOS", _money(resumen.get("gastos"))),
        ("TOTAL DE LA DEUDA", _money(resumen.get("gran_total"))),
    ]
    cells = []
    for name, amount in summary_boxes:
        amount_style = ParagraphStyle(
            f"Summary_{name}", parent=value, fontSize=9.6 if name != "TOTAL DE LA DEUDA" else 11.2,
            leading=11 if name != "TOTAL DE LA DEUDA" else 13, alignment=TA_CENTER,
            textColor=colors.HexColor("#7f1d1d" if name == "TOTAL DE LA DEUDA" else "#0f172a"),
        )
        cells.append([P(name, label), Paragraph(amount, amount_style)])
    summary = Table([cells], colWidths=[usable_width / 5.0] * 5)
    summary.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.55, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (4, 0), (4, 0), colors.HexColor("#fff7ed")),
        ("BOX", (4, 0), (4, 0), 0.7, colors.HexColor("#f59e0b")),
    ]))
    story += [summary, Spacer(1, 5)]

    # 3. METADATOS DE CONTROL
    control = Table([
        [P("FECHA DE GENERACIÓN:", label), P(generado, value), P("FECHA DE CORTE:", label), P(corte, value)]
    ], colWidths=[38 * mm, 52 * mm, 34 * mm, 52 * mm])
    control.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [control, Spacer(1, 5)]

    # 4. TABLA DETALLADA DE LIQUIDACIÓN
    story.append(Paragraph("DETALLE MENSUAL DE AMORTIZACIÓN / LIQUIDACIÓN", section))
    headers = [
        "PERÍODO", "ORD.", "EXTRA.", "GASTOS", "ABONOS", "CAP. LIQUIDABLE",
        "DÍAS", "TASA E.A.", "TASA MES", "INTERÉS MES", "INT. ACUM.", "SALDO FINAL",
    ]
    rows = [[P(label_, cell_b) for label_ in headers]]
    for result in resultados or []:
        rows.append([
            P(str(result.get("desde", ""))[:7], cell_b),
            P(_money(result.get("ordinarias")), right),
            P(_money(result.get("extraordinarias")), right),
            P(_money(result.get("gastos")), right),
            P(_money(result.get("abonos")), right),
            P(_money(result.get("capital_liquidable")), right),
            P(result.get("dias"), center),
            P(str(result.get("tasa_ea", "")), center),
            P(_pct(result.get("tasa_mes"), 4), center),
            P(_money(result.get("intereses")), right),
            P(_money(result.get("int_acumulado")), right),
            P(_money(result.get("cap_int")), right),
        ])

    # 264 mm: dentro del ancho útil de A4 apaisado y con margen para bordes.
    widths = [
        18 * mm, 23 * mm, 24 * mm, 21 * mm, 21 * mm, 27 * mm,
        11 * mm, 18 * mm, 18 * mm, 24 * mm, 27 * mm, 31 * mm,
    ]
    detail = LongTable(rows, colWidths=widths, repeatRows=1, splitByRow=1)
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 2.5),
    ]
    for row_index in range(1, len(rows)):
        if row_index % 2 == 0:
            table_style.append(("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#f8fafc")))
    detail.setStyle(TableStyle(table_style))
    story += [detail, Spacer(1, 6)]

    # 5. CIERRE Y FIRMAS
    closing = Table([
        [
            Paragraph("Firma Autorizada", signature),
            Paragraph("Liquidación elaborada por Sistema LegalTech", note),
        ],
        [
            Paragraph("__________________________________", signature),
            Paragraph("Documento generado automáticamente por el sistema.", note),
        ],
    ], colWidths=[usable_width * 0.45, usable_width * 0.55])
    closing.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEABOVE", (0, 0), (-1, 0), 0.4, colors.HexColor("#cbd5e1")),
    ]))
    story += [closing]

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
        canvas.line(margin_x, 7 * mm, page_width - margin_x, 7 * mm)
        canvas.setFont("Helvetica", 6.2)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(margin_x, 3.5 * mm, "Gestión Judicial | Estado de cuenta oficial")
        canvas.drawRightString(page_width - margin_x, 3.5 * mm, f"Página {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


# Publicar un único generador para web y bot.
main.generar_pdf_liquidacion = generar_pdf_liquidacion_final

# Sustituir únicamente la ruta PDF, manteniendo el resto del ERP intacto.
main.app.router.routes[:] = [
    route for route in main.app.router.routes
    if not (
        getattr(route, "path", None) == "/liquidador/exportar/pdf"
        and "POST" in getattr(route, "methods", set())
    )
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
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"Liquidacion_{inmueble_id}_{fecha_corte.isoformat()}.pdf",
    )


print("[PDF FINAL] Formato A4 apaisado estructurado y legible activado para web y bot", flush=True)
