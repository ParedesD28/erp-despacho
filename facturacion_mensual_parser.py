"""Parser determinista del Recibo de Cobro Mensual COLON (`COBRO EXPENSA COMUN`).

El PDF es texto nativo (Haru / COLON). Hay 2 facturas por página Letter:
mitad superior Y >= 395, mitad inferior Y < 395. Solo se usa la columna
VALOR MES para proponer cargos; SALDO ANT. e INTERESES sirven para
cuadrar la fila, nunca para insertar en expensas_ph.
"""
from __future__ import annotations

import io
import re
from typing import Any, BinaryIO, Union

from pypdf import PdfReader

Source = Union[str, bytes, bytearray, BinaryIO]

Y_MITAD = 395.0
MESES_ES = {
    "ENERO": 1,
    "FEBRERO": 2,
    "MARZO": 3,
    "ABRIL": 4,
    "MAYO": 5,
    "JUNIO": 6,
    "JULIO": 7,
    "AGOSTO": 8,
    "SEPTIEMBRE": 9,
    "OCTUBRE": 10,
    "NOVIEMBRE": 11,
    "DICIEMBRE": 12,
}


def parse_money(val: str) -> float:
    if not val:
        return 0.0
    clean = re.sub(r"[^0-9.\-]", "", str(val).replace(",", ""))
    return float(clean) if clean and clean not in {"-", "."} else 0.0


def _as_reader_bytes(source: Source) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if hasattr(source, "read"):
        data = source.read()
        return bytes(data)
    with open(str(source), "rb") as fh:
        return fh.read()


def extract_page_items(page) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text and text.strip():
            items.append(
                {
                    "text": text.strip(),
                    "raw": text,
                    "x": round(float(tm[4]), 2),
                    "y": round(float(tm[5]), 2),
                    "sz": round(float(font_size), 2) if font_size else None,
                }
            )

    page.extract_text(visitor_text=visitor)
    return items


def split_halves(items: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    top = [it for it in items if it["y"] >= Y_MITAD]
    bottom = [it for it in items if it["y"] < Y_MITAD]
    halves: list[tuple[str, list[dict[str, Any]]]] = []
    if top:
        halves.append(("top", top))
    if bottom:
        halves.append(("bottom", bottom))
    return halves


def parse_invoice_half(items: list[dict[str, Any]], page_num: int, half: str) -> dict[str, Any]:
    items_sorted = sorted(items, key=lambda it: (-round(it["y"], 1), it["x"]))
    lines: list[dict[str, Any]] = []
    for it in items_sorted:
        if not lines or abs(lines[-1]["y"] - it["y"]) > 2.2:
            lines.append({"y": it["y"], "items": [it]})
        else:
            lines[-1]["items"].append(it)
            lines[-1]["items"].sort(key=lambda x: x["x"])

    inv: dict[str, Any] = {
        "page": page_num,
        "half": half,
        "copropiedad": None,
        "tipo_doc": None,
        "copropiedad_nit": None,
        "factura_no": None,
        "direccion_copropiedad": None,
        "telefono_copropiedad": None,
        "fecha_comprobante": None,
        "fecha_vencimiento": None,
        "propietario_nombre": None,
        "propietario_identificacion": None,
        "unidad_dir": None,
        "ref_pago": None,
        "encabezado_conceptos_mes": None,
        "conceptos": [],
        "saldo_anterior_pie": None,
        "base_calculo_intereses": None,
        "subtotal": None,
        "descuento": None,
        "iva": None,
        "total_a_pagar": None,
        "convenio": None,
    }

    y_header_conceptos = None
    y_footer_subtotal = None

    for ln in lines:
        texts = [i["text"] for i in ln["items"]]
        for i in ln["items"]:
            t = i["text"]
            x = i["x"]
            if "P.H." in t or "URBANIZACION" in t or "CONJUNTO" in t or "EDIFICIO" in t:
                if inv["copropiedad"] is None and x < 300:
                    inv["copropiedad"] = t
            if t == "COBRO EXPENSA COMUN":
                inv["tipo_doc"] = t
            if t.startswith("Nit.:"):
                inv["copropiedad_nit"] = t.replace("Nit.:", "").strip()
            if t.startswith("Dirección.:"):
                inv["direccion_copropiedad"] = t.replace("Dirección.:", "").strip()
            if t.startswith("Tel:"):
                inv["telefono_copropiedad"] = t.replace("Tel:", "").strip()
            if t.startswith("REF.PAGO:"):
                inv["ref_pago"] = t.replace("REF.PAGO:", "").strip()
            if t.startswith("CONCEPTOS MES DE"):
                inv["encabezado_conceptos_mes"] = t
                y_header_conceptos = ln["y"]
            if t.startswith("Saldo Anterior :"):
                y_footer_subtotal = ln["y"]
                m_sa = re.search(r"Saldo Anterior\s*:\s*([\d,.\-]+)", t)
                m_bi = re.search(r"Base calculo de intereses\s*\$\s*([\d,.\-]+)", t)
                if m_sa:
                    inv["saldo_anterior_pie"] = parse_money(m_sa.group(1))
                if m_bi:
                    inv["base_calculo_intereses"] = parse_money(m_bi.group(1))
            if t.startswith("Consigne en"):
                inv["convenio"] = t

        if any(i["text"] == "N°" for i in ln["items"]):
            for i in ln["items"]:
                if i["text"].isdigit() and i["x"] > 480:
                    inv["factura_no"] = i["text"]
        if any(i["text"].startswith("Fecha compte:") for i in ln["items"]):
            for i in ln["items"]:
                if re.match(r"^\d{4}\.\d{2}\.\d{2}$", i["text"]):
                    inv["fecha_comprobante"] = i["text"]
        if any(i["text"].startswith("NOMBRE:") for i in ln["items"]):
            for i in ln["items"]:
                if 60 <= i["x"] < 310 and i["text"] != "NOMBRE:":
                    inv["propietario_nombre"] = i["text"]
                elif 330 <= i["x"] < 450 and i["text"] != "Nit :":
                    inv["propietario_identificacion"] = i["text"]
        if any(i["text"].startswith("DIR :") for i in ln["items"]):
            for i in ln["items"]:
                if 60 <= i["x"] < 310 and i["text"] != "DIR :":
                    inv["unidad_dir"] = i["text"]
                elif i["x"] >= 470 and re.match(r"^\d{4}\.\d{2}\.\d{2}$", i["text"]):
                    inv["fecha_vencimiento"] = i["text"]
        for i in ln["items"]:
            if i["x"] >= 470 and re.match(r"^\d{4}\.\d{2}\.\d{2}$", i["text"]):
                if inv["fecha_comprobante"] != i["text"]:
                    inv["fecha_vencimiento"] = i["text"]

        if any(i["text"] == "Sub-Total" for i in ln["items"]):
            for i in ln["items"]:
                if i["x"] > 500:
                    inv["subtotal"] = parse_money(i["text"])
        if any(i["text"] == "Descuento" for i in ln["items"]):
            for i in ln["items"]:
                if i["x"] > 500:
                    inv["descuento"] = parse_money(i["text"])
        if any(i["text"].startswith("IVA") for i in ln["items"]):
            for i in ln["items"]:
                if i["x"] > 500:
                    inv["iva"] = parse_money(i["text"])
        if any(i["text"] == "Total a Pagar" for i in ln["items"]):
            for i in ln["items"]:
                if i["x"] > 500:
                    inv["total_a_pagar"] = parse_money(i["text"])

    if y_header_conceptos is not None and y_footer_subtotal is not None:
        for ln in lines:
            if y_footer_subtotal + 2.0 < ln["y"] < y_header_conceptos - 2.0:
                row = {
                    "concepto": None,
                    "saldo_ant": 0.0,
                    "valor_mes": 0.0,
                    "intereses_mes": 0.0,
                    "tasa_pct": None,
                    "total_concepto": 0.0,
                }
                for i in ln["items"]:
                    x = i["x"]
                    t = i["text"]
                    if x < 250:
                        row["concepto"] = (row["concepto"] + " " + t).strip() if row["concepto"] else t
                    elif 250 <= x < 340:
                        row["saldo_ant"] = parse_money(t)
                    elif 340 <= x < 405:
                        # B5: VALOR MES vive en X ∈ [340, 405]
                        row["valor_mes"] = parse_money(t)
                    elif 405 <= x < 465:
                        row["intereses_mes"] = parse_money(t)
                    elif 465 <= x < 510:
                        row["tasa_pct"] = parse_money(t)
                    elif x >= 510:
                        row["total_concepto"] = parse_money(t)
                if row["concepto"] or row["total_concepto"] or row["valor_mes"] or row["saldo_ant"]:
                    inv["conceptos"].append(row)

    return inv


def parse_facturacion_pdf(source: Source) -> list[dict[str, Any]]:
    data = _as_reader_bytes(source)
    reader = PdfReader(io.BytesIO(data))
    invoices: list[dict[str, Any]] = []
    for p_idx, page in enumerate(reader.pages):
        items = extract_page_items(page)
        for half, h_items in split_halves(items):
            invoices.append(parse_invoice_half(h_items, p_idx + 1, half))
    return invoices


def periodo_desde_facturas(invoices: list[dict[str, Any]]) -> tuple[int, int]:
    """Año-mes del lote: Fecha compte o 'CONCEPTOS MES DE <MES>'."""
    anios: list[int] = []
    meses: list[int] = []
    for inv in invoices:
        fc = str(inv.get("fecha_comprobante") or "")
        m = re.match(r"^(\d{4})\.(\d{2})\.(\d{2})$", fc)
        if m:
            anios.append(int(m.group(1)))
            meses.append(int(m.group(2)))
            continue
        enc = str(inv.get("encabezado_conceptos_mes") or "").upper()
        m2 = re.search(r"CONCEPTOS MES DE\s+([A-ZÁÉÍÓÚ]+)", enc)
        if m2:
            mes = MESES_ES.get(m2.group(1).replace("Á", "A").replace("É", "E").replace("Í", "I").replace("Ó", "O").replace("Ú", "U"))
            if mes:
                meses.append(mes)
    if not anios or not meses:
        raise ValueError("No se pudo leer el período de la facturación (Fecha compte / CONCEPTOS MES DE).")
    anio = max(set(anios), key=anios.count)
    mes = max(set(meses), key=meses.count)
    if not 1 <= mes <= 12:
        raise ValueError(f"Mes de facturación inválido: {mes}")
    return anio, mes


def recortar_media_pagina_png(pdf_bytes: bytes, page_1based: int, half: str, *, scale: float = 2.0) -> bytes:
    """Recorta ESA media página (no el PDF entero) y la devuelve como PNG."""
    import pypdfium2 as pdfium

    half_n = "top" if str(half).lower() in {"top", "arriba", "sup", "superior"} else "bottom"
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        idx = int(page_1based) - 1
        if idx < 0 or idx >= len(pdf):
            raise ValueError(f"Página {page_1based} fuera de rango")
        page = pdf[idx]
        height = float(page.get_height())
        if half_n == "top":
            crop = (0.0, Y_MITAD, 0.0, 0.0)
        else:
            crop = (0.0, 0.0, 0.0, max(0.0, height - Y_MITAD))
        bitmap = page.render(scale=scale, crop=crop)
        pil = bitmap.to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf.close()
