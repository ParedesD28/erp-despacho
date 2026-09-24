"""
Extracción de estados de cuenta PH (software COLON Contabilidad)
desde PDF sin tablas nativas hacia filas estructuradas y Excel.
"""
from __future__ import annotations

import io
import re
import secrets
import time
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Union

import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pypdf import PdfReader

Source = Union[str, Path, bytes, bytearray, BinaryIO]

MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB por archivo
MAX_ARCHIVOS_LOTE = 40
MAX_LOTE_BYTES = 200 * 1024 * 1024  # 200 MB total por lote
_MIN_CHARS_TEXTO = 80
_DATE_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
_MONEY_RE = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")
_DOC_TIPO_RE = re.compile(r"^[A-Za-z]{2,6}$")
_DOC_NUM_RE = re.compile(r"^\d{4,12}$")
_CODIGO_CUENTA_RE = re.compile(r"^\d{3,8}$")
_BLOQUE_NUMERO_INLINE_RE = re.compile(
    r"Bloque\s+(\S+)\s+N[uú]mero\s+(\S+)",
    re.IGNORECASE,
)
_DECORATIVE_CHARS = set("-._")
_RUIDO_CONCEPTO = {
    "periodo",
    "pagina",
    "página",
    "concepto",
    "documento",
    "fecha",
    "valor",
    "abono",
    "saldo",
    "software",
    "colon",
    "saldo anterior",
    "movimiento",
    "total",
    "identificacion",
    "identificación",
    "señor (a):",
    "senor (a):",
}

_COLUMNS = (
    "Archivo",
    "Titular",
    "Bloque",
    "Apartamento",
    "Codigo Cuenta",
    "Concepto",
    "Tipo Documento",
    "Número",
    "Fecha",
    "Valor",
    "Abono",
    "Saldo",
)
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


def _reparar_texto_pdf(texto: str) -> str:
    """
    Mitiga mojibake típico de PDF (UTF-8 leído como latin-1) y normaliza espacios.
    No inventa caracteres perdidos (�); solo repara cuando hay evidencia.
    """
    if not texto:
        return ""
    s = texto.replace("\xa0", " ").strip()
    if "Ã" in s or "Â" in s:
        try:
            s = s.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    # cp1252 confusions frecuentes en streams PDF
    for bad, good in (
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\u201c", '"'),
        ("\u201d", '"'),
        ("\u2013", "-"),
        ("\u2014", "-"),
    ):
        s = s.replace(bad, good)
    return re.sub(r"\s+", " ", s).strip()


def _es_linea_decorativa(linea: str) -> bool:
    compact = linea.replace(" ", "")
    return bool(compact) and set(compact) <= _DECORATIVE_CHARS


def _parse_monto(raw: str) -> float:
    txt = str(raw).replace(",", "").replace(" ", "").strip()
    return float(txt)


def _es_monto(raw: str) -> bool:
    return bool(_MONEY_RE.match(str(raw).strip()))


def _limpiar_lineas(texto: str) -> list[str]:
    lineas: list[str] = []
    for cruda in (texto or "").splitlines():
        linea = _reparar_texto_pdf(cruda)
        if not linea or _es_linea_decorativa(linea):
            continue
        lineas.append(linea)
    return lineas


def _extraer_texto_pagina(page) -> str:
    """Extrae texto con fallback; detecta páginas casi vacías (posible escaneo)."""
    texto = page.extract_text() or ""
    if len(texto.strip()) < 10:
        try:
            texto = page.extract_text(extraction_mode="layout") or texto
        except TypeError:
            pass
    return texto


def _es_concepto_valido(concepto: str) -> bool:
    c = (concepto or "").strip()
    if len(c) < 3:
        return False
    if _DATE_RE.match(c) or _es_monto(c):
        return False
    low = c.lower()
    if low in _RUIDO_CONCEPTO:
        return False
    if low.startswith("pagina ") or low.startswith("página "):
        return False
    if low.startswith("periodo"):
        return False
    # Debe tener letras (evita basura numérica)
    if not re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", c):
        return False
    return True


def _es_titular_candidato(linea: str) -> bool:
    if not linea or len(linea) < 5:
        return False
    if _DATE_RE.match(linea) or _es_monto(linea) or _CODIGO_CUENTA_RE.match(linea):
        return False
    low = linea.lower()
    if low in _RUIDO_CONCEPTO or low in {"bloque", "numero", "número"}:
        return False
    if low.startswith("aporte ") or low.startswith("intereses ") or low.startswith("seguro "):
        return False
    letras = sum(ch.isalpha() for ch in linea)
    return letras >= 4 and not _DOC_TIPO_RE.match(linea)


def extraer_cabecera_portada(lineas: list[str]) -> dict:
    """
    Lee bloque / apartamento / titular / código de la hoja 1 (COLON).
    Patrón típico:
      9401
      Bloque
      9
      Numero
      401
      BAÑOL RIVERA MAURICIO
    """
    cabecera = {
        "titular": None,
        "bloque": None,
        "apartamento": None,
        "codigo_cuenta": None,
        "conjunto": lineas[0] if lineas else None,
        "advertencia_encoding": False,
    }

    for i, linea in enumerate(lineas):
        inline = _BLOQUE_NUMERO_INLINE_RE.search(linea)
        if inline:
            cabecera["bloque"] = inline.group(1)
            cabecera["apartamento"] = inline.group(2)

        if linea.lower() == "bloque" and i + 1 < len(lineas):
            cabecera["bloque"] = lineas[i + 1]
            if i > 0 and _CODIGO_CUENTA_RE.match(lineas[i - 1]):
                cabecera["codigo_cuenta"] = lineas[i - 1]

        if linea.lower() in {"numero", "número"} and i + 1 < len(lineas):
            cabecera["apartamento"] = lineas[i + 1]
            # Titular suele ser la línea siguiente al número de apartamento
            if i + 2 < len(lineas) and _es_titular_candidato(lineas[i + 2]):
                cabecera["titular"] = lineas[i + 2]

    if cabecera["titular"] and "\ufffd" in cabecera["titular"]:
        cabecera["advertencia_encoding"] = True

    return cabecera


def _validar_bloque_transaccion(concepto, tipo, numero, fecha, valor_raw, abono_raw, saldo_raw) -> dict:
    """Valida el bloque de 7 líneas; evita 'transcribir' basura como movimiento."""
    if not _DATE_RE.match(fecha):
        raise ValueError("fecha inválida")
    if not _es_concepto_valido(concepto):
        raise ValueError("concepto inválido")
    if not _DOC_TIPO_RE.match(tipo):
        raise ValueError("tipo documento inválido")
    if not _DOC_NUM_RE.match(numero):
        raise ValueError("número documento inválido")
    if not (_es_monto(valor_raw) and _es_monto(abono_raw) and _es_monto(saldo_raw)):
        raise ValueError("montos inválidos")
    valor = _parse_monto(valor_raw)
    abono = _parse_monto(abono_raw)
    saldo = _parse_monto(saldo_raw)
    if valor < 0 or abono < 0:
        raise ValueError("montos negativos")
    return {
        "Concepto": concepto,
        "Tipo Documento": tipo.upper(),
        "Número": numero,
        "Fecha": fecha,
        "Valor": valor,
        "Abono": abono,
        "Saldo": saldo,
    }


def _auditar_continuidad_saldo(rows: list[dict]) -> int:
    """
    Cuenta saltos de saldo inconsistentes: saldo_prev + valor - abono ≈ saldo.
    Tolerancia 1.0 por redondeos. No descarta filas; solo métrica de calidad.
    """
    if len(rows) < 2:
        return 0
    inconsistencias = 0
    for prev, cur in zip(rows, rows[1:]):
        esperado = round(float(prev["Saldo"]) + float(cur["Valor"]) - float(cur["Abono"]), 2)
        if abs(esperado - float(cur["Saldo"])) > 1.0:
            inconsistencias += 1
    return inconsistencias


def analizar_estado_cuenta_pdf(source: Source) -> dict:
    """
    Extrae cabecera (hoja 1), movimientos y métricas de calidad.

    Blindajes:
      1) Validación estructural de cada bloque (anti-mala transcripción)
      2) Buffer continuo entre páginas + omisiones auditables
      3) Reparación de encoding + detección de PDF sin texto / �
    """
    reader = PdfReader(_as_pdf_stream(source))
    if len(reader.pages) < 2:
        raise EstadoCuentaPdfError("El PDF debe tener al menos 2 páginas (se omite la portada).")

    lineas_portada = _limpiar_lineas(_extraer_texto_pagina(reader.pages[0]))
    cabecera = extraer_cabecera_portada(lineas_portada)

    # Buffer continuo entre páginas para no perder bloques cortados al salto.
    lineas: list[str] = []
    chars_movimiento = 0
    for page in reader.pages[1:]:
        texto = _extraer_texto_pagina(page)
        chars_movimiento += len(texto.strip())
        lineas.extend(_limpiar_lineas(texto))

    chars_portada = sum(len(x) for x in lineas_portada)
    if chars_portada + chars_movimiento < _MIN_CHARS_TEXTO or not lineas:
        raise EstadoCuentaPdfError(
            "El PDF no tiene texto seleccionable suficiente. "
            "Parece un escaneo o PDF solo imagen; exporte nuevamente desde COLON (no imprimir a PDF)."
        )

    rows: list[dict] = []
    omitidos_detalle: list[dict] = []
    fechas_detectadas = 0
    i = 0
    n = len(lineas)
    while i < n:
        if not _DATE_RE.match(lineas[i]):
            i += 1
            continue
        fechas_detectadas += 1
        if i < 3 or i + 3 >= n:
            omitidos_detalle.append(
                {
                    "indice": i,
                    "fecha": lineas[i],
                    "motivo": "bloque incompleto en borde de página o fin de archivo",
                }
            )
            i += 1
            continue
        try:
            mov = _validar_bloque_transaccion(
                lineas[i - 3],
                lineas[i - 2],
                lineas[i - 1],
                lineas[i],
                lineas[i + 1],
                lineas[i + 2],
                lineas[i + 3],
            )
            mov["Archivo"] = None  # lo fija el caller / lote / seguro
            mov["Titular"] = cabecera.get("titular")
            mov["Bloque"] = cabecera.get("bloque")
            mov["Apartamento"] = cabecera.get("apartamento")
            mov["Codigo Cuenta"] = cabecera.get("codigo_cuenta")
            rows.append(mov)
        except (ValueError, TypeError, IndexError) as exc:
            omitidos_detalle.append(
                {
                    "indice": i,
                    "fecha": lineas[i],
                    "motivo": str(exc) or "bloque inválido",
                }
            )
        i += 1

    if fechas_detectadas == 0:
        raise EstadoCuentaPdfError(
            "No se detectaron fechas de movimiento. "
            "Verifique que sea un estado de cuenta COLON con texto seleccionable."
        )

    inconsistencias_saldo = _auditar_continuidad_saldo(rows)
    bloques_omitidos = len(omitidos_detalle)

    return {
        "rows": rows,
        "cabecera": cabecera,
        "titular": cabecera.get("titular"),
        "bloque": cabecera.get("bloque"),
        "apartamento": cabecera.get("apartamento"),
        "codigo_cuenta": cabecera.get("codigo_cuenta"),
        "conjunto": cabecera.get("conjunto"),
        "fechas_detectadas": fechas_detectadas,
        "movimientos_extraidos": len(rows),
        "bloques_omitidos": bloques_omitidos,
        "omitidos_detalle": omitidos_detalle[:20],
        "inconsistencias_saldo": inconsistencias_saldo,
        "advertencia_encoding": bool(cabecera.get("advertencia_encoding")),
        "saldo_final": rows[-1]["Saldo"] if rows else None,
        "muestra_inicio": rows[:3],
        "muestra_fin": rows[-3:] if rows else [],
        "paginas_leidas": max(0, len(reader.pages) - 1),
        "alerta_calidad": bloques_omitidos > 0 or inconsistencias_saldo > 0,
    }


def extraer_movimientos_estado_cuenta(source: Source) -> list[dict]:
    """Lee el PDF y retorna la lista de movimientos."""
    return analizar_estado_cuenta_pdf(source)["rows"]


def _estilizar_hoja_movimientos(ws) -> None:
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
    ws.freeze_panes = "A2"

    money_idxs = {_COLUMNS.index(c) + 1 for c in _MONEY_COLS}
    max_row = max(ws.max_row, 2)
    for row in ws.iter_rows(min_row=2, max_row=max_row, max_col=len(_COLUMNS)):
        for cell in row:
            cell.border = thin
            if cell.column in money_idxs and cell.value is not None:
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")

    for idx, name in enumerate(_COLUMNS, start=1):
        letter = get_column_letter(idx)
        if name in ("Concepto", "Titular", "Archivo"):
            width = 45
        elif name in ("Bloque", "Apartamento", "Codigo Cuenta"):
            width = 14
        else:
            width = 15
        ws.column_dimensions[letter].width = width


def _estilizar_encabezado(ws, header_row: int = 1) -> None:
    """Solo la fila de títulos. Evita recorrer miles de celdas en Render."""
    header_fill = PatternFill("solid", fgColor="1E3A8A")
    header_font = Font(name="Calibri", bold=True, color="FFFFFF")
    for cell in ws[header_row]:
        if cell.value is None:
            continue
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = f"A{header_row + 1}"


def _formato_dinero(ws, nombres: set[str], header_row: int = 1) -> None:
    headers = {cell.value: cell.column for cell in ws[header_row] if cell.value}
    columnas = [headers[nombre] for nombre in nombres if nombre in headers]
    if not columnas or ws.max_row <= header_row:
        return
    for row in ws.iter_rows(
        min_row=header_row + 1,
        max_row=ws.max_row,
        min_col=min(columnas),
        max_col=max(columnas),
    ):
        for cell in row:
            if cell.column in columnas and isinstance(cell.value, (int, float)):
                cell.number_format = "#,##0"


def _estilizar_hoja_simple(ws, max_col: int = 2) -> None:
    _estilizar_encabezado(ws, 1)


def _sellar_archivo(rows: list[dict], filename: str | None) -> list[dict]:
    """Copia defensiva: cada fila queda atada al PDF de origen (anti-cruce)."""
    nombre = filename or ""
    selladas: list[dict] = []
    for row in rows:
        copia = dict(row)
        copia["Archivo"] = nombre
        selladas.append(copia)
    return selladas


def generar_excel_estado_cuenta(rows: list[dict], cabecera: dict | None = None) -> io.BytesIO:
    """Convierte movimientos (+ hoja Cabecera) en Excel formateado en memoria."""
    cabecera = cabecera or {}
    df = pd.DataFrame(rows, columns=list(_COLUMNS))
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        meta = pd.DataFrame(
            [
                ("Archivo", cabecera.get("archivo") or ""),
                ("Conjunto", cabecera.get("conjunto") or ""),
                ("Titular", cabecera.get("titular") or ""),
                ("Bloque", cabecera.get("bloque") or ""),
                ("Apartamento", cabecera.get("apartamento") or ""),
                ("Codigo Cuenta", cabecera.get("codigo_cuenta") or ""),
                ("Movimientos", len(rows)),
            ],
            columns=["Campo", "Valor"],
        )
        meta.to_excel(writer, index=False, sheet_name="Cabecera")
        df.to_excel(writer, index=False, sheet_name="Estado de Cuenta")
        _estilizar_hoja_simple(writer.book["Cabecera"], max_col=2)
        writer.book["Cabecera"].column_dimensions["A"].width = 18
        writer.book["Cabecera"].column_dimensions["B"].width = 45
        _estilizar_hoja_movimientos(writer.book["Estado de Cuenta"])

    output.seek(0)
    return output


def generar_excel_lote(cuentas: list[dict], depuracion: dict | None = None) -> io.BytesIO:
    """
    Un Excel para N PDFs: hoja Resumen (1 fila/cuenta) + Movimientos (todas las filas).
    Cada movimiento ya trae Archivo/Titular/Bloque/Apartamento de SU PDF.
    """
    resumen_rows = []
    movimientos: list[dict] = []
    for cuenta in cuentas:
        resumen_rows.append(
            {
                "Archivo": cuenta.get("archivo") or "",
                "Titular": cuenta.get("titular"),
                "Bloque": cuenta.get("bloque"),
                "Apartamento": cuenta.get("apartamento"),
                "Codigo Cuenta": cuenta.get("codigo_cuenta"),
                "Conjunto": cuenta.get("conjunto"),
                "Fechas detectadas": cuenta.get("fechas_detectadas"),
                "Movimientos": cuenta.get("movimientos_extraidos"),
                "Omitidos": cuenta.get("bloques_omitidos"),
                "Saltos saldo": cuenta.get("inconsistencias_saldo"),
                "Saldo final": cuenta.get("saldo_final"),
                "OK": (
                    cuenta.get("movimientos_extraidos", 0) > 0
                    and cuenta.get("bloques_omitidos", 0) == 0
                    and cuenta.get("inconsistencias_saldo", 0) == 0
                    and not cuenta.get("error")
                ),
                "Error": cuenta.get("error") or "",
            }
        )
        movimientos.extend(cuenta.get("rows") or [])

    if depuracion is None and movimientos:
        from estado_cuenta_etl import depurar_movimientos

        depuracion = depurar_movimientos(pd.DataFrame(movimientos))

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(resumen_rows).to_excel(writer, index=False, sheet_name="Resumen")
        from estado_cuenta_etl import CLASIFICACION_INTERES, anotar_verificacion, clasificar_concepto

        movimientos_df = anotar_verificacion(pd.DataFrame(movimientos, columns=list(_COLUMNS)))
        # La mora ya usó estas filas. En el Excel no se listan intereses.
        sin_interes = movimientos_df.loc[
            movimientos_df["Concepto"].map(clasificar_concepto) != CLASIFICACION_INTERES
        ].copy()
        sin_interes.to_excel(writer, index=False, sheet_name="Movimientos")
        if depuracion is not None:
            depuracion["depurado"].to_excel(writer, index=False, sheet_name="Depurado")
            depuracion["consolidado"].to_excel(writer, index=False, sheet_name="Consolidado")
            depuracion["cortes"].to_excel(writer, index=False, sheet_name="Inicio Mora")
            columnas_detalle = [
                "Concepto",
                "Tipo Documento",
                "Número",
                "Fecha",
                "Valor",
                "Abono",
                "Saldo",
                "Abono Acumulado",
                "Diferencia",
            ]
            for cert in depuracion.get("certificados") or []:
                hoja = cert["hoja"]
                mascara = (
                    (sin_interes["Archivo"].astype(str) == str(cert["archivo"] or ""))
                    & (sin_interes["Titular"].astype(str) == str(cert["titular"] or ""))
                    & (sin_interes["Codigo Cuenta"].astype(str) == str(cert["codigo_cuenta"] or ""))
                )
                sin_interes.loc[mascara, columnas_detalle].to_excel(
                    writer, index=False, sheet_name=hoja, startrow=6
                )
                ws_cert = writer.book[hoja]
                ws_cert["A1"] = "Titular"
                ws_cert["B1"] = cert["titular"]
                ws_cert["A2"] = "Bloque"
                ws_cert["B2"] = cert["bloque"]
                ws_cert["A3"] = "Apartamento"
                ws_cert["B3"] = cert["apartamento"]
                ws_cert["A4"] = "Codigo cuenta"
                ws_cert["B4"] = cert["codigo_cuenta"]
                ws_cert["A5"] = "Inicio mora"
                ws_cert["B5"] = cert["fecha_inicio_mora"]
                _estilizar_encabezado(ws_cert, 7)
                _formato_dinero(
                    ws_cert,
                    {"Valor", "Abono", "Saldo", "Abono Acumulado", "Diferencia"},
                    header_row=7,
                )
                ws_cert.column_dimensions["A"].width = 42
                ws_cert.column_dimensions["B"].width = 16
                ws_cert.column_dimensions["C"].width = 14
                ws_cert.column_dimensions["D"].width = 14
                for letra in ("E", "F", "G", "H", "I"):
                    ws_cert.column_dimensions[letra].width = 18
            _estilizar_encabezado(writer.book["Depurado"])
            _estilizar_encabezado(writer.book["Consolidado"])
            _estilizar_encabezado(writer.book["Inicio Mora"])
            _formato_dinero(
                writer.book["Depurado"],
                {"Valor", "Abono", "Saldo Inverso", "Diferencia Mora"},
            )
        _estilizar_encabezado(writer.book["Resumen"])
        _estilizar_encabezado(writer.book["Movimientos"])
        _formato_dinero(
            writer.book["Movimientos"],
            set(_MONEY_COLS) | {"Abono Acumulado", "Diferencia"},
        )
        for col in writer.book["Resumen"].columns:
            letter = get_column_letter(col[0].column)
            writer.book["Resumen"].column_dimensions[letter].width = 18
        writer.book["Resumen"].column_dimensions["A"].width = 36
        writer.book["Resumen"].column_dimensions["B"].width = 36
        for idx, name in enumerate(_COLUMNS, start=1):
            letter = get_column_letter(idx)
            if name in ("Concepto", "Titular", "Archivo"):
                width = 42
            elif name in ("Bloque", "Apartamento", "Codigo Cuenta"):
                width = 14
            else:
                width = 15
            writer.book["Movimientos"].column_dimensions[letter].width = width
        writer.book["Movimientos"].column_dimensions["M"].width = 18
        writer.book["Movimientos"].column_dimensions["N"].width = 18
    output.seek(0)
    return output


def procesar_estado_cuenta_pdf(source: Source) -> io.BytesIO:
    """Pipeline completo: PDF → filas → Excel en BytesIO."""
    analisis = analizar_estado_cuenta_pdf(source)
    rows = _sellar_archivo(analisis["rows"], None)
    cabecera = dict(analisis.get("cabecera") or {})
    return generar_excel_estado_cuenta(rows, cabecera)


def procesar_estado_cuenta_pdf_seguro(
    contenido: bytes,
    filename: str | None = None,
    *,
    generar_excel: bool = True,
) -> dict:
    """
    Valida el upload, analiza y opcionalmente genera Excel de UNA cuenta.
    Sin estado compartido: cada llamada es independiente.
    """
    validar_pdf_bytes(contenido, filename)
    analisis = analizar_estado_cuenta_pdf(contenido)
    rows = _sellar_archivo(analisis["rows"], filename)
    cabecera = dict(analisis.get("cabecera") or {})
    cabecera["archivo"] = filename
    out = {
        **{k: v for k, v in analisis.items() if k != "rows"},
        "archivo": filename,
        "cabecera": cabecera,
        "rows": rows,
    }
    if generar_excel:
        out["excel"] = generar_excel_estado_cuenta(rows, cabecera)
    return out


_CACHE_TTL_S = 900
_CACHE_MAX = 8
_lote_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = Lock()


def _guardar_lote(cuentas: list[dict], depuracion: dict | None) -> str:
    cache_id = secrets.token_urlsafe(16)
    ahora = time.time()
    with _cache_lock:
        vencidos = [clave for clave, (expira, _) in _lote_cache.items() if expira < ahora]
        for clave in vencidos:
            _lote_cache.pop(clave, None)
        while len(_lote_cache) >= _CACHE_MAX:
            antiguo = min(_lote_cache, key=lambda clave: _lote_cache[clave][0])
            _lote_cache.pop(antiguo, None)
        _lote_cache[cache_id] = (ahora + _CACHE_TTL_S, {"cuentas": cuentas, "depuracion": depuracion, "excel": None})
    return cache_id


def excel_desde_cache(cache_id: str) -> io.BytesIO | None:
    """Arma el Excel con el análisis ya hecho. None si el servidor se reinició."""
    if not cache_id:
        return None
    with _cache_lock:
        item = _lote_cache.get(cache_id)
        if not item or item[0] < time.time():
            _lote_cache.pop(cache_id, None)
            return None
        payload = item[1]
        listo = payload.get("excel")
    if listo is not None:
        return io.BytesIO(listo)
    excel = generar_excel_lote(payload["cuentas"], payload["depuracion"])
    data = excel.getvalue()
    with _cache_lock:
        vigente = _lote_cache.get(cache_id)
        if vigente:
            vigente[1]["excel"] = data
            vigente[1]["cuentas"] = []
            vigente[1]["depuracion"] = None
    excel.seek(0)
    return excel


def procesar_lote_estados_cuenta(
    archivos: list[tuple[str, bytes]],
    *,
    incluir_excel: bool = True,
) -> dict:
    """
    Procesa N PDFs en aislamiento estricto (sin cruces cuenta↔cuenta).

    - Cada PDF se parsea en su propia llamada (buffer/cabecera propios).
    - Cada fila se sella con el nombre de SU archivo y su titular/bloque/apto.
    - Un error en un PDF no contamina los demás: queda en Resumen con error.
    - No genera Excel por archivo (ahorra RAM/CPU); arma un solo Excel al final.
    """
    if not archivos:
        raise EstadoCuentaPdfError("Debe adjuntar al menos un PDF.")
    if len(archivos) > MAX_ARCHIVOS_LOTE:
        raise EstadoCuentaPdfError(f"Máximo {MAX_ARCHIVOS_LOTE} PDF por lote.")

    total = sum(len(blob) for _, blob in archivos)
    if total > MAX_LOTE_BYTES:
        mb = MAX_LOTE_BYTES / (1024 * 1024)
        raise EstadoCuentaPdfError(f"El lote supera el límite total de {mb:.0f} MB.")

    cuentas: list[dict] = []
    for idx, (filename, contenido) in enumerate(archivos):
        try:
            # generar_excel=False: crítico en lotes grandes (evita OOM/timeout en Render).
            uno = procesar_estado_cuenta_pdf_seguro(
                contenido, filename, generar_excel=False
            )
            cuentas.append(
                {
                    "archivo": filename,
                    "titular": uno.get("titular"),
                    "bloque": uno.get("bloque"),
                    "apartamento": uno.get("apartamento"),
                    "codigo_cuenta": uno.get("codigo_cuenta"),
                    "conjunto": uno.get("conjunto"),
                    "fechas_detectadas": uno["fechas_detectadas"],
                    "movimientos_extraidos": uno["movimientos_extraidos"],
                    "bloques_omitidos": uno["bloques_omitidos"],
                    "inconsistencias_saldo": uno.get("inconsistencias_saldo", 0),
                    "advertencia_encoding": uno.get("advertencia_encoding", False),
                    "saldo_final": uno.get("saldo_final"),
                    "alerta_calidad": bool(uno.get("alerta_calidad")),
                    "rows": uno["rows"],
                    "error": None,
                }
            )
        except EstadoCuentaPdfError as exc:
            cuentas.append(
                {
                    "archivo": filename,
                    "titular": None,
                    "bloque": None,
                    "apartamento": None,
                    "codigo_cuenta": None,
                    "conjunto": None,
                    "fechas_detectadas": 0,
                    "movimientos_extraidos": 0,
                    "bloques_omitidos": 0,
                    "inconsistencias_saldo": 0,
                    "advertencia_encoding": False,
                    "saldo_final": None,
                    "alerta_calidad": True,
                    "rows": [],
                    "error": str(exc),
                }
            )
        except Exception as exc:
            cuentas.append(
                {
                    "archivo": filename,
                    "titular": None,
                    "bloque": None,
                    "apartamento": None,
                    "codigo_cuenta": None,
                    "conjunto": None,
                    "fechas_detectadas": 0,
                    "movimientos_extraidos": 0,
                    "bloques_omitidos": 0,
                    "inconsistencias_saldo": 0,
                    "advertencia_encoding": False,
                    "saldo_final": None,
                    "alerta_calidad": True,
                    "rows": [],
                    "error": f"Error interno: {type(exc).__name__}",
                }
            )
        finally:
            # Liberar bytes del PDF ya procesado (anti-OOM en lotes de 30+).
            archivos[idx] = (filename, b"")

    ok = sum(1 for c in cuentas if not c.get("error") and c.get("movimientos_extraidos", 0) > 0)
    fallidos = sum(1 for c in cuentas if c.get("error"))
    movimientos = [row for cuenta in cuentas for row in (cuenta.get("rows") or [])]
    depuracion = None
    if movimientos:
        from estado_cuenta_etl import depurar_movimientos

        depuracion = depurar_movimientos(pd.DataFrame(movimientos))
        cortes = {
            (str(fila["Archivo"] or ""), str(fila["Titular"] or ""), str(fila["Codigo Cuenta"] or "")): fila["Fecha Inicio Mora"]
            for _, fila in depuracion["cortes"].iterrows()
        }
        for cuenta in cuentas:
            clave = (
                str(cuenta.get("archivo") or ""),
                str(cuenta.get("titular") or ""),
                str(cuenta.get("codigo_cuenta") or ""),
            )
            cuenta["fecha_inicio_mora"] = cortes.get(clave) or ""
    excel = generar_excel_lote(cuentas, depuracion) if incluir_excel else None
    cache_id = _guardar_lote(cuentas, depuracion)
    if excel is not None:
        with _cache_lock:
            guardado = _lote_cache.get(cache_id)
            if guardado:
                guardado[1]["excel"] = excel.getvalue()
                guardado[1]["cuentas"] = []
                guardado[1]["depuracion"] = None
                excel.seek(0)
    return {
        "archivos_recibidos": len(archivos),
        "archivos_ok": ok,
        "archivos_fallidos": fallidos,
        "movimientos_totales": sum(c.get("movimientos_extraidos", 0) for c in cuentas),
        "cache_id": cache_id,
        "cuentas": [
            {k: v for k, v in c.items() if k != "rows"} | {"muestra_filas": len(c.get("rows") or [])}
            for c in cuentas
        ],
        "excel": excel,
    }
