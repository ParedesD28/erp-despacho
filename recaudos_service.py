"""
Servicio central de Recaudos, Conciliación Bancaria, Informes Contables
y Emisión de Paz y Salvo Oficial.
"""
import os
import secrets
import hashlib
from datetime import date, datetime
from typing import Dict, Any, Tuple, Optional
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib import colors

DATABASE_URL = os.getenv("DATABASE_URL")

def calcular_imputacion_abono(saldo_intereses: float, saldo_capital: float, honorarios_pct: float, valor_abono: float) -> Dict[str, float]:
    """
    Imputación matemática conforme al Art. 1653 del Código Civil Colombiano:
    1. Si existen intereses moratorios devengados, se extingue primero el interés.
    2. El excedente amortiza el capital.
    3. Se calculan los honorarios proporcionales pactados sobre el recaudo.
    """
    abono_restante = float(valor_abono)
    
    # 1. Imputación a intereses
    if abono_restante <= saldo_intereses:
        pago_intereses = abono_restante
        abono_restante = 0.0
    else:
        pago_intereses = saldo_intereses
        abono_restante -= saldo_intereses
        
    # 2. Imputación a capital
    if abono_restante <= saldo_capital:
        pago_capital = abono_restante
        abono_restante = 0.0
    else:
        pago_capital = saldo_capital
        abono_restante -= saldo_capital

    # 3. Honorarios de cobranza (proporcionales al valor recuperado según acuerdo)
    pct = max(float(honorarios_pct), 0.0) / 100.0
    honorarios = round(float(valor_abono) * pct, 2)
    
    return {
        "abono_intereses": round(pago_intereses, 2),
        "abono_capital": round(pago_capital, 2),
        "honorarios_cobro": honorarios,
        "sobrante_a_favor": round(abono_restante, 2)
    }

def generar_pdf_paz_y_salvo(
    inmueble_id: int, 
    conjunto: str, 
    unidad: str, 
    nombre_deudor: str, 
    identificacion: str, 
    fecha_emision: Optional[date] = None,
    abogado_firmante: str = "Diego Alejandro Paredes García"
) -> Tuple[str, str]:
    """
    Genera el PDF formal de Paz y Salvo con firma y hash de verificación.
    Retorna la ruta del archivo y el código de verificación único.
    """
    if not fecha_emision:
        fecha_emision = date.today()
        
    os.makedirs("static/pdfs", exist_ok=True)
    token_aleatorio = secrets.token_hex(6).upper()
    codigo_verif = hashlib.sha256(f"{inmueble_id}|{identificacion}|{fecha_emision}|{token_aleatorio}".encode()).hexdigest()[:16].upper()
    
    filename = f"Paz_Y_Salvo_{inmueble_id}_{token_aleatorio}.pdf"
    filepath = os.path.join("static", "pdfs", filename)
    
    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
        title=f"Certificado de Paz y Salvo - {unidad}",
        author="Departamento Jurídico y Cobranzas"
    )
    
    styles = getSampleStyleSheet()
    
    kicker = ParagraphStyle('KickerPYS', fontName='Helvetica-Bold', fontSize=9, leading=11, textColor=colors.HexColor('#2563eb'), alignment=1)
    titulo = ParagraphStyle('TituloPYS', fontName='Helvetica-Bold', fontSize=20, leading=24, textColor=colors.HexColor('#0f172a'), alignment=1, spaceAfter=8)
    subtitulo = ParagraphStyle('SubPYS', fontName='Helvetica', fontSize=10, leading=13, textColor=colors.HexColor('#64748b'), alignment=1, spaceAfter=20)
    cuerpo = ParagraphStyle('CuerpoPYS', fontName='Helvetica', fontSize=11, leading=16, textColor=colors.HexColor('#1e293b'), alignment=4, spaceAfter=14)
    resaltado = ParagraphStyle('ResaltadoPYS', fontName='Helvetica-Bold', fontSize=12, leading=17, textColor=colors.HexColor('#0f172a'), alignment=1)
    meta_box = ParagraphStyle('MetaBoxPYS', fontName='Helvetica', fontSize=9.5, leading=13, textColor=colors.HexColor('#334155'))
    meta_label = ParagraphStyle('MetaLabelPYS', fontName='Helvetica-Bold', fontSize=9.5, leading=13, textColor=colors.HexColor('#0f172a'))
    pie = ParagraphStyle('PiePYS', fontName='Helvetica', fontSize=8, leading=10, textColor=colors.HexColor('#94a3b8'), alignment=1)
    
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    fecha_texto = f"{fecha_emision.day} de {meses[fecha_emision.month - 1]} de {fecha_emision.year}"
    
    story = [
        Paragraph("DEPARTAMENTO DE GESTIÓN JURÍDICA Y RECUPERACIÓN DE CARTERA", kicker),
        Spacer(1, 4),
        Paragraph("CERTIFICADO DE PAZ Y SALVO", titulo),
        Paragraph("Régimen de Propiedad Horizontal - Ley 675 de 2001", subtitulo),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor('#cbd5e1'), spaceAfter=18),
        Spacer(1, 5)
    ]
    
    tabla_datos = [
        [Paragraph("Copropiedad:", meta_label), Paragraph(conjunto, meta_box)],
        [Paragraph("Unidad / Inmueble:", meta_label), Paragraph(unidad, meta_box)],
        [Paragraph("Titular / Deudor:", meta_label), Paragraph(nombre_deudor, meta_box)],
        [Paragraph("Identificación:", meta_label), Paragraph(f"C.C. {identificacion}", meta_box)],
        [Paragraph("Fecha de expedición:", meta_label), Paragraph(fecha_texto, meta_box)],
        [Paragraph("Código de verificación:", meta_label), Paragraph(f"<b>{codigo_verif}</b>", meta_box)],
    ]
    
    t = Table(tabla_datos, colWidths=[140, 360])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#edf2f7')),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(t)
    story.append(Spacer(1, 20))
    
    p1 = (
        f"El suscrito apoderado especial y encargado del cobro y recuperación de cartera de la copropiedad "
        f"<b>{conjunto}</b>, hace constar que a la fecha de expedición del presente documento, el inmueble "
        f"distinguido como <b>{unidad}</b>, de propiedad o a cargo de <b>{nombre_deudor}</b>, "
        f"identificado(a) con C.C. No. <b>{identificacion}</b>, se encuentra a:"
    )
    story.append(Paragraph(p1, cuerpo))
    
    box_paz = Table([[Paragraph("PAZ Y SALVO", resaltado)]], colWidths=[500])
    box_paz.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#ecfdf5')),
        ('BOX', (0, 0), (-1, -1), 1.5, colors.HexColor('#10b981')),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(box_paz)
    story.append(Spacer(1, 14))
    
    p2 = (
        "Por concepto de cuotas de administración ordinarias, cuotas extraordinarias, intereses moratorios "
        "y gastos de cobranza pactados dentro de la gestión adelantada. No adeuda suma alguna por las obligaciones "
        "causadas hasta la fecha."
    )
    story.append(Paragraph(p2, cuerpo))
    
    p3 = (
        f"Se expide en la ciudad de Pereira, Risaralda, a los {fecha_texto}, con destino al interesado para "
        f"los trámites pertinentes."
    )
    story.append(Paragraph(p3, cuerpo))
    story.append(Spacer(1, 40))
    
    firma = Table([
        [Paragraph(f"<b>{abogado_firmante}</b><br/>Abogado Titulado<br/>Representante de Cartera y Cobranzas", meta_box)]
    ], colWidths=[260])
    firma.setStyle(TableStyle([
        ('LINEABOVE', (0, 0), (-1, -1), 1, colors.HexColor('#0f172a')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(firma)
    story.append(Spacer(1, 30))
    
    story.append(Paragraph(f"Certificado generado y validado electrónicamente en ERP • Hash: {codigo_verif}", pie))
    doc.build(story)
    
    return filepath, codigo_verif

def exportar_informe_contable_excel(conn, conjunto_residencial: Optional[str] = None, fecha_inicio: Optional[date] = None, fecha_fin: Optional[date] = None) -> str:
    """
    Genera un archivo Excel profesional con la rendición de cuentas discriminada por cuenta:
    - Total recaudado
    - Transferencia Neta a la Copropiedad (Capital + Intereses)
    - Honorarios de Cobranza (Abogado)
    - Gastos Procesales
    """
    if not fecha_inicio:
        fecha_inicio = date(date.today().year, date.today().month, 1)
    if not fecha_fin:
        fecha_fin = date.today()
        
    query = """
        SELECT 
            r.id AS recaudo_id,
            r.fecha_pago,
            i.conjunto_residencial,
            i.torre_apto,
            r.identificacion_deudor,
            r.nombre_deudor,
            r.valor_total,
            r.abono_capital,
            r.abono_intereses,
            r.honorarios_cobro,
            r.gastos_reembolso,
            r.banco_origen,
            r.referencia_transaccion,
            r.estado_conciliacion,
            r.aprobado_por
        FROM recaudos_contabilidad r
        JOIN inmuebles_ph i ON r.inmueble_id = i.id
        WHERE r.fecha_pago BETWEEN %s AND %s
    """
    params = [fecha_inicio, fecha_fin]
    if conjunto_residencial:
        query += " AND i.conjunto_residencial = %s"
        params.append(conjunto_residencial)
        
    query += " ORDER BY r.fecha_pago ASC, i.conjunto_residencial ASC;"
    
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, tuple(params))
        filas = cur.fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rendición de Cuentas"
    ws.views.sheetView[0].showGridLines = True
    
    # Encabezado Ejecutivo
    ws.merge_cells("A1:K1")
    ws["A1"] = "INFORME OFICIAL DE RECAUDO Y RENDICIÓN DE CUENTAS POR CUENTA"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="1E3A8A")
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28
    
    ws.merge_cells("A2:K2")
    filtro_txt = f"Copropiedad: {conjunto_residencial or 'TODAS'} | Periodo: {fecha_inicio} a {fecha_fin}"
    ws["A2"] = filtro_txt
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color="475569")
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 18
    
    headers = [
        "Fecha Pago", "Copropiedad", "Unidad", "Identificación", "Deudor",
        "Total Recaudado", "Abono Capital (PH)", "Abono Interés (PH)",
        "Honorarios (Jurídico)", "Banco / Medio", "Referencia", "Estado"
    ]
    
    header_fill = PatternFill("solid", fgColor="F1F5F9")
    header_font = Font(name="Calibri", size=10, bold=True, color="0F172A")
    border_thin = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )
    
    ws.append([])
    ws.append(headers)
    ws.row_dimensions[4].height = 24
    
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=4, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border_thin

    fila_num = 5
    for r in filas:
        ws.append([
            str(r["fecha_pago"]),
            r["conjunto_residencial"],
            r["torre_apto"],
            r["identificacion_deudor"],
            r["nombre_deudor"],
            float(r["valor_total"]),
            float(r["abono_capital"]),
            float(r["abono_intereses"]),
            float(r["honorarios_cobro"]),
            r["banco_origen"] or "-",
            r["referencia_transaccion"] or "-",
            r["estado_conciliacion"]
        ])
        
        for c in range(6, 10):
            ws.cell(row=fila_num, column=c).number_format = "$#,##0"
            
        for c in range(1, len(headers) + 1):
            ws.cell(row=fila_num, column=c).border = border_thin
            ws.cell(row=fila_num, column=c).font = Font(name="Calibri", size=9.5)
        fila_num += 1
        
    if filas:
        ws.append([
            "TOTALES", "", "", "", "",
            f"=SUM(F5:F{fila_num-1})",
            f"=SUM(G5:G{fila_num-1})",
            f"=SUM(H5:H{fila_num-1})",
            f"=SUM(I5:I{fila_num-1})",
            "", "", ""
        ])
        ws.row_dimensions[fila_num].height = 22
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=fila_num, column=c)
            cell.font = Font(name="Calibri", size=10, bold=True, color="0F172A")
            cell.fill = PatternFill("solid", fgColor="E2E8F0")
            cell.border = border_thin
            if c in (6, 7, 8, 9):
                cell.number_format = "$#,##0"

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)
        
    os.makedirs("static/informes", exist_ok=True)
    nombre_archivo = f"Informe_Recaudos_{fecha_fin.strftime('%Y%m%d')}_{secrets.token_hex(4)}.xlsx"
    ruta_salida = os.path.join("static", "informes", nombre_archivo)
    wb.save(ruta_salida)
    return ruta_salida

