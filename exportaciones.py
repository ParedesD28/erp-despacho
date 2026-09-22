"""Generador oficial de exportaciones: PDF judicial unificado y Excel ejecutivo."""
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


def _table_exists(cur, table_name):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table_name,))
    row = cur.fetchone()
    if not row:
        return False
    return bool(row[0] if not isinstance(row, dict) else next(iter(row.values())))


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

    import pdf_storage

    filename = f"Estado_Cuenta_{int(inmueble_id)}_{secrets.token_hex(8)}.pdf"
    path = str(pdf_storage.resolve_pdf(filename))

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

    # 3. Tabla detallada
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
    "radicado_interno", "radicado_rama", "naturaleza", "juzgado", "etapa_actual",
    "demandante", "identificacion_demandante", "demandado", "identificacion_demandado",
    "inmueble_id", "estado", "pretensiones", "medidas_cautelares", "abogado_id",
    "Historial_Actuaciones", "Gestiones_CRM",
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


def _formatear_gestiones_crm(gestiones):
    """Formatea el historial extrajudicial para mostrarlo dentro de cada expediente."""
    partes = []
    for g in gestiones:
        fecha = g.get("fecha") or g.get("fecha_gestion") or g.get("created_at")
        fecha_texto = fecha.strftime("%Y-%m-%d %H:%M") if hasattr(fecha, "strftime") else str(fecha or "")[:16]
        tipo = str(g.get("tipo_contacto") or g.get("tipo") or "Gestión CRM").strip()
        resumen = str(g.get("resumen") or "").strip()
        usuario = str(g.get("usuario") or "ERP").strip()
        identificacion = str(g.get("identificacion_deudor") or "").strip()
        promesa = g.get("promesa_pago_fecha") or g.get("promesa")
        promesa_texto = promesa.strftime("%Y-%m-%d") if hasattr(promesa, "strftime") else str(promesa or "")[:10]

        contacto = f" [Contacto: {identificacion}]" if identificacion else ""
        cuerpo = " - ".join(x for x in (tipo, resumen) if x)
        cuerpo = f"{cuerpo}{contacto}" if cuerpo else contacto.strip()
        if promesa_texto:
            cuerpo = f"{cuerpo} [Promesa: {promesa_texto}]"
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
            elif ws.title == "Expedientes" and col_idx == 14:
                width = 95
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
                ws.row_dimensions[row].height = 120


def _build_executive_report(cur):
    cur.execute("""
        SELECT
            p.radicado_interno, p.radicado_rama, p.naturaleza, p.juzgado, p.etapa_actual,
            COALESCE(ppdemandante.nombres, 'SIN REGISTRO') AS demandante,
            COALESCE(ppdemandante.identificaciones, '') AS identificacion_demandante,
            COALESCE(ppdemandado.nombres, 'SIN REGISTRO') AS demandado,
            COALESCE(ppdemandado.identificaciones, '') AS identificacion_demandado,
            p.inmueble_id,
            COALESCE(p.estado, 'ACTIVO') AS estado,
            p.pretensiones, p.medidas_cautelares, p.abogado_id
        FROM procesos p
        LEFT JOIN LATERAL (
            SELECT
                STRING_AGG(DISTINCT c.identificacion, ' | ' ORDER BY c.identificacion) AS identificaciones,
                STRING_AGG(DISTINCT c.nombre, ' | ' ORDER BY c.nombre) AS nombres
            FROM proceso_partes pp
            JOIN contactos c ON c.id=pp.contacto_id
            WHERE pp.radicado_interno=p.radicado_interno
              AND UPPER(pp.rol)='DEMANDANTE'
        ) ppdemandante ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                STRING_AGG(DISTINCT c.identificacion, ' | ' ORDER BY c.identificacion) AS identificaciones,
                STRING_AGG(DISTINCT c.nombre, ' | ' ORDER BY c.nombre) AS nombres
            FROM proceso_partes pp
            JOIN contactos c ON c.id=pp.contacto_id
            WHERE pp.radicado_interno=p.radicado_interno
              AND UPPER(pp.rol)='DEMANDADO'
        ) ppdemandado ON TRUE
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

    # CRM manual / ERP
    cur.execute("""
        SELECT id, inmueble_id, identificacion_deudor, tipo_contacto, resumen,
               promesa_pago_fecha, fecha, usuario
        FROM gestiones_crm
        WHERE COALESCE(anulado, FALSE)=FALSE
        ORDER BY fecha ASC, id ASC
    """)
    gestiones_crm = _rows_as_dicts(cur)

    crm_by_inmueble = defaultdict(list)
    crm_by_identificacion = defaultdict(list)
    for gestion in gestiones_crm:
        inmueble_id = gestion.get("inmueble_id")
        identificacion = str(gestion.get("identificacion_deudor") or "").strip()
        if inmueble_id is not None:
            crm_by_inmueble[str(inmueble_id)].append(gestion)
        if identificacion:
            crm_by_identificacion[identificacion].append(gestion)

    # CRM generado desde cartera/agente, cuando la tabla existe.
    # Se incorpora de forma tolerante porque esa tabla puede no existir en
    # instalaciones antiguas y su esquema puede variar entre versiones.
    if _table_exists(cur, "gestiones_cartera"):
        cur.execute("SELECT * FROM gestiones_cartera")
        gestiones_cartera = _rows_as_dicts(cur)
        for gestion in gestiones_cartera:
            if gestion.get("anulado") is True or str(gestion.get("estado") or "").upper() == "ANULADO":
                continue
            normalized = {
                "id": gestion.get("id") or gestion.get("id_gestion") or gestion.get("gestion_id"),
                "inmueble_id": gestion.get("inmueble_id"),
                "identificacion_deudor": gestion.get("identificacion_deudor"),
                "tipo_contacto": gestion.get("tipo_contacto") or "WhatsApp IA",
                "resumen": gestion.get("resumen") or "",
                "promesa_pago_fecha": gestion.get("promesa_pago_fecha"),
                "fecha": next(
                    (
                        gestion.get(name)
                        for name in (
                            "fecha", "fecha_gestion", "created_at", "createdAt",
                            "timestamp", "fecha_registro", "created",
                        )
                        if gestion.get(name) is not None
                    ),
                    None,
                ),
                "usuario": gestion.get("usuario") or "Bot Claude",
            }
            inmueble_id = normalized.get("inmueble_id")
            identificacion = str(normalized.get("identificacion_deudor") or "").strip()
            if inmueble_id is not None:
                crm_by_inmueble[str(inmueble_id)].append(normalized)
            if identificacion:
                crm_by_identificacion[identificacion].append(normalized)

    rows = []
    for p in procesos:
        radicado = str(p.get("radicado_interno") or "")
        item = {key: p.get(key) for key in _REPORT_HEADERS[:-2]}
        actuaciones_del_expediente = history.get(radicado, [])
        item["Historial_Actuaciones"] = _formatear_historial_actuaciones(actuaciones_del_expediente)

        crm_del_expediente = []
        seen_crm = set()
        inmueble_id = p.get("inmueble_id")
        if inmueble_id is not None:
            crm_del_expediente.extend(crm_by_inmueble.get(str(inmueble_id), []))

        identificaciones = []
        for value in (p.get("identificacion_demandado"), p.get("identificacion_demandante")):
            identificaciones.extend(
                x.strip() for x in str(value or "").split("|") if x.strip()
            )
        for identificacion in identificaciones:
            crm_del_expediente.extend(crm_by_identificacion.get(identificacion, []))

        crm_unicas = []
        for gestion in sorted(
            crm_del_expediente,
            key=lambda x: str(x.get("fecha") or x.get("fecha_gestion") or x.get("created_at") or ""),
        ):
            firma = (
                gestion.get("id"),
                str(gestion.get("fecha") or gestion.get("fecha_gestion") or gestion.get("created_at") or ""),
                str(gestion.get("resumen") or ""),
                str(gestion.get("identificacion_deudor") or ""),
            )
            if firma in seen_crm:
                continue
            seen_crm.add(firma)
            crm_unicas.append(gestion)

        item["Gestiones_CRM"] = _formatear_gestiones_crm(crm_unicas)
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
        wb.properties.subject = "Resumen integral de expedientes con historial completo de actuaciones y gestiones CRM"
        wb.properties.creator = "Gestión Judicial"
        wb.properties.description = "Incluye expediente, actuaciones, gestiones CRM, contactos e inmuebles."
    output.seek(0)
    return output
