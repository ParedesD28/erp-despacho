"""
Extracción de estados de cuenta PH (software COLON Contabilidad)
desde PDF sin tablas nativas hacia filas estructuradas y Excel.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import BinaryIO, Union

import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pypdf import PdfReader

Source = Union[str, Path, bytes, bytearray, BinaryIO]

MAX_PDF_BYTES = 15 * 1024 * 1024  # 15 MB
_DATE_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
_DECORATIVE_CHARS = set("-._")

_COLUMNS = ("Concepto", "Tipo Documento", "Número", "Fecha", "Valor", "Abono", "Saldo")
_MONEY_COLS = ("Valor", "Abono", "Saldo")


class EstadoCuentaPdfError(ValueError):
    """Error controlado de validación o extracción del PDF."""


def _as_pdf_stream(source: Source) -> io.BytesIO:
    """Normaliza path / bytes / file-like a un stream seekable para pypdf."""
    if isinstance(source, (str, Path)):
        return io.BytesIO(Path(source).read_bytes())
    if isinstance(source, (bytes, bytearray)):
        return io.BytesIO(bytes(source))
    data = source.read()
    if isinstance(data, str):
        data = data.encode("latin-1")
    return io.BytesIO(data)


def validar_pdf_bytes(contenido: bytes, filename: str | None = None) -> None:
    """Valida tamaño, extensión y firma mágica PDF."""
    if not contenido:
        raise EstadoCuentaPdfError("El archivo está vacío.")
    if len(contenido) > MAX_PDF_BYTES:
        mb = MAX_PDF_BYTES / (1024 * 1024)
        raise EstadoCuentaPdfError(f"El PDF supera el límite de {mb:.0f} MB.")
    if filename:
        name = filename.lower().strip()
        if not name.endswith(".pdf"):
            raise EstadoCuentaPdfError("Solo se permiten archivos .pdf.")
    if not contenido.lstrip().startswith(b"%PDF"):
        raise EstadoCuentaPdfError("El archivo no parece un PDF válido.")


def _es_linea_decorativa(linea: str) -> bool:
    compact = linea.replace(" ", "")
    return bool(compact) and set(compact) <= _DECORATIVE_CHARS


def _parse_monto(raw: str) -> float:
    return float(str(raw).replace(",", "").replace(" ", "").strip())


def _limpiar_lineas(texto: str) -> list[str]:
    lineas: list[str] = []
    for cruda in (texto or "").splitlines():
        linea = cruda.strip()
        if not linea or _es_linea_decorativa(linea):
            continue
        lineas.append(linea)
    return lineas


def analizar_estado_cuenta_pdf(source: Source) -> dict:
    """
    Extrae movimientos y métricas de calidad.

    Retorna:
      rows, fechas_detectadas, movimientos_extraidos, bloques_omitidos,
      saldo_final, muestra_inicio, muestra_fin
    """
    reader = PdfReader(_as_pdf_stream(source))
    if len(reader.pages) < 2:
        raise EstadoCuentaPdfError("El PDF debe tener al menos 2 páginas (se omite la portada).")

    lineas: list[str] = []
    for page in reader.pages[1:]:
        lineas.extend(_limpiar_lineas(page.extract_text() or ""))

    rows: list[dict] = []
    fechas_detectadas = 0
    bloques_omitidos = 0
    i = 0
    n = len(lineas)
    while i < n:
        if not _DATE_RE.match(lineas[i]):
            i += 1
            continue
        fechas_detectadas += 1
        if i < 3 or i + 3 >= n:
            bloques_omitidos += 1
            i += 1
            continue
        try:
            rows.append(
                {
                    "Concepto": lineas[i - 3],
                    "Tipo Documento": lineas[i - 2],
                    "Número": lineas[i - 1],
                    "Fecha": lineas[i],
                    "Valor": _parse_monto(lineas[i + 1]),
                    "Abono": _parse_monto(lineas[i + 2]),
                    "Saldo": _parse_monto(lineas[i + 3]),
                }
            )
        except (ValueError, TypeError, IndexError):
            bloques_omitidos += 1
        i += 1

    return {
        "rows": rows,
        "fechas_detectadas": fechas_detectadas,
        "movimientos_extraidos": len(rows),
        "bloques_omitidos": bloques_omitidos,
        "saldo_final": rows[-1]["Saldo"] if rows else None,
        "muestra_inicio": rows[:3],
        "muestra_fin": rows[-3:] if rows else [],
        "paginas_leidas": max(0, len(reader.pages) - 1),
    }


def extraer_movimientos_estado_cuenta(source: Source) -> list[dict]:
    """Lee el PDF (omite página 1) y retorna la lista de movimientos."""
    return analizar_estado_cuenta_pdf(source)["rows"]


def generar_excel_estado_cuenta(rows: list[dict]) -> io.BytesIO:
    """Convierte la lista de movimientos en un Excel formateado en memoria."""
    df = pd.DataFrame(rows, columns=list(_COLUMNS))
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Estado de Cuenta")
        ws = writer.book["Estado de Cuenta"]

        header_fill = PatternFill("solid", fgColor="1E3A8A")
        header_font = Font(name="Calibri", bold=True, color="FFFFFF")
        thin = Border(
            left=Side(style="thin", color="CBD5E1"),
            right=Side(style="thin", color="CBD5E1"),
            top=Side(style="thin", color="CBD5E1"),
            bottom=Side(style="thin", color="CBD5E1"),
        )

        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin

        money_idxs = {_COLUMNS.index(c) + 1 for c in _MONEY_COLS}
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(_COLUMNS)):
            for cell in row:
                cell.border = thin
                if cell.column in money_idxs:
                    cell.number_format = "#,##0.00"
                    cell.alignment = Alignment(horizontal="right")

        for idx, name in enumerate(_COLUMNS, start=1):
            letter = get_column_letter(idx)
            ws.column_dimensions[letter].width = 45 if name == "Concepto" else 15

        ws.freeze_panes = "A2"

    output.seek(0)
    return output


def procesar_estado_cuenta_pdf(source: Source) -> io.BytesIO:
    """Pipeline completo: PDF → filas → Excel en BytesIO."""
    rows = extraer_movimientos_estado_cuenta(source)
    return generar_excel_estado_cuenta(rows)


def procesar_estado_cuenta_pdf_seguro(
    contenido: bytes,
    filename: str | None = None,
) -> dict:
    """
    Valida el upload, analiza y genera Excel.
    Retorna meta + excel (BytesIO). No escribe a disco ni a DB.
    """
    validar_pdf_bytes(contenido, filename)
    analisis = analizar_estado_cuenta_pdf(contenido)
    excel = generar_excel_estado_cuenta(analisis["rows"])
    return {
        **{k: v for k, v in analisis.items() if k != "rows"},
        "rows": analisis["rows"],
        "excel": excel,
    }
