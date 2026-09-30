"""Carga masiva de FMI para el lote de certificados (pegar lista / CSV).

No inventa FMI: solo mapea texto que el usuario aporta a índices del preview.
El mapa resultante se envía como `fmi_por_indice` al generar.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Iterable, Optional

# Columnas reconocidas (case-insensitive, con/sin acentos normalizados).
_COL_INDICE = frozenset({"indice", "índice", "index", "idx", "#"})
_COL_FMI = frozenset({"fmi", "folio", "folio_matricula", "matricula"})
_COL_ARCHIVO = frozenset({"archivo", "file", "pdf", "nombre_archivo"})
_COL_TORRE = frozenset(
    {
        "torre_apto",
        "torre-apto",
        "clave_canonica",
        "clave",
        "bloque_apto",
        "unidad",
    }
)
_COL_CUENTA = frozenset({"codigo_cuenta", "cuenta", "cod_cuenta", "codigo"})


def _norm_key(s: str) -> str:
    t = (s or "").strip().lower()
    t = (
        t.replace("í", "i")
        .replace("á", "a")
        .replace("é", "e")
        .replace("ó", "o")
        .replace("ú", "u")
    )
    return t


def _norm_match(s: str) -> str:
    """Normaliza valor de matching (archivo / torre / cuenta)."""
    t = (s or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def parse_fmi_lista(texto: str) -> list[str]:
    """Una línea = un FMI. Líneas vacías se conservan como '' (hueco en orden)."""
    if texto is None:
        return []
    # splitlines() descarta el newline final vacío; conserva huecos intermedios.
    return [ln.strip() for ln in texto.splitlines()]


def aplicar_fmi_lista(
    lineas: list[str],
    indices_destino: list[int],
    *,
    mapa_existente: Optional[dict[int, str]] = None,
) -> dict[str, Any]:
    """
    Aplica FMI en el mismo orden que `indices_destino` (filas visibles/seleccionadas).

    Sobran líneas o faltan: se reportan; no se inventan índices.
    Valores vacíos limpian el FMI de esa fila.
    """
    out = dict(mapa_existente or {})
    n_dest = len(indices_destino)
    n_src = len(lineas)
    aplicadas = 0
    for i, idx in enumerate(indices_destino):
        if i >= n_src:
            break
        out[int(idx)] = (lineas[i] or "").strip()
        aplicadas += 1
    return {
        "mapa": out,
        "aplicadas": aplicadas,
        "destino": n_dest,
        "lineas": n_src,
        "sobrantes": max(0, n_src - n_dest),
        "faltantes": max(0, n_dest - n_src),
    }


def _detect_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        class _Comma(csv.Dialect):
            delimiter = ","
            quotechar = '"'
            doublequote = True
            skipinitialspace = True
            lineterminator = "\n"
            quoting = csv.QUOTE_MINIMAL

        return _Comma()


def parse_fmi_csv(texto: str) -> list[dict[str, str]]:
    """
    Parsea CSV/TSV con encabezado.

    Requiere columna `fmi` (o alias) y al menos una clave de fila:
    `indice`, `archivo`, `torre_apto`/`clave_canonica`, `codigo_cuenta`.
    """
    raw = (texto or "").strip()
    if not raw:
        return []
    # Quitar BOM
    if raw.startswith("\ufeff"):
        raw = raw.lstrip("\ufeff")
    sample = raw[:4096]
    dialect = _detect_dialect(sample)
    reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("CSV sin encabezado")

    colmap: dict[str, str] = {}
    for name in reader.fieldnames:
        key = _norm_key(name or "")
        if key in _COL_INDICE:
            colmap["indice"] = name
        elif key in _COL_FMI:
            colmap["fmi"] = name
        elif key in _COL_ARCHIVO:
            colmap["archivo"] = name
        elif key in _COL_TORRE:
            colmap["torre_apto"] = name
        elif key in _COL_CUENTA:
            colmap["codigo_cuenta"] = name

    if "fmi" not in colmap:
        raise ValueError("CSV debe incluir columna fmi")
    if not any(k in colmap for k in ("indice", "archivo", "torre_apto", "codigo_cuenta")):
        raise ValueError(
            "CSV debe incluir indice, archivo, torre_apto/clave_canonica "
            "o codigo_cuenta además de fmi"
        )

    filas: list[dict[str, str]] = []
    for row in reader:
        if not row:
            continue
        item: dict[str, str] = {"fmi": str(row.get(colmap["fmi"]) or "").strip()}
        if "indice" in colmap:
            item["indice"] = str(row.get(colmap["indice"]) or "").strip()
        if "archivo" in colmap:
            item["archivo"] = str(row.get(colmap["archivo"]) or "").strip()
        if "torre_apto" in colmap:
            item["torre_apto"] = str(row.get(colmap["torre_apto"]) or "").strip()
        if "codigo_cuenta" in colmap:
            item["codigo_cuenta"] = str(
                row.get(colmap["codigo_cuenta"]) or ""
            ).strip()
        # Saltar filas totalmente vacías
        if not any(item.values()):
            continue
        filas.append(item)
    return filas


def _torre_de_resultado(r: dict[str, Any]) -> str:
    return str(
        r.get("torre_apto_neon")
        or r.get("clave_canonica")
        or r.get("torre_apto")
        or ""
    ).strip()


def mapear_csv_a_indices(
    filas: list[dict[str, str]],
    resultados: list[dict[str, Any]],
    *,
    mapa_existente: Optional[dict[int, str]] = None,
) -> dict[str, Any]:
    """
    Resuelve cada fila CSV a un índice del preview.

    Prioridad de match por fila: indice → archivo → torre_apto/clave → codigo_cuenta.
    Ambigüedades (varios candidatos) se reportan y no se aplican.
    """
    out = dict(mapa_existente or {})
    aplicados: list[dict[str, Any]] = []
    errores: list[str] = []
    omitidos = 0

    by_archivo: dict[str, list[int]] = {}
    by_torre: dict[str, list[int]] = {}
    by_cuenta: dict[str, list[int]] = {}
    for i, r in enumerate(resultados or []):
        a = _norm_match(str(r.get("archivo") or ""))
        if a:
            by_archivo.setdefault(a, []).append(i)
        t = _norm_match(_torre_de_resultado(r))
        if t:
            by_torre.setdefault(t, []).append(i)
        c = _norm_match(str(r.get("codigo_cuenta") or ""))
        if c:
            by_cuenta.setdefault(c, []).append(i)

    for n, fila in enumerate(filas, start=2):  # 2 = tras encabezado
        fmi = (fila.get("fmi") or "").strip()
        idx: Optional[int] = None
        via = ""

        raw_idx = (fila.get("indice") or "").strip()
        if raw_idx != "":
            try:
                idx = int(float(raw_idx)) if "." in raw_idx else int(raw_idx)
            except ValueError:
                errores.append(f"fila {n}: indice inválido '{raw_idx}'")
                continue
            if idx < 0 or idx >= len(resultados):
                errores.append(f"fila {n}: indice {idx} fuera de rango")
                continue
            via = "indice"
        else:
            candidatos: list[int] = []
            archivo = (fila.get("archivo") or "").strip()
            torre = (fila.get("torre_apto") or "").strip()
            cuenta = (fila.get("codigo_cuenta") or "").strip()
            if archivo:
                candidatos = list(by_archivo.get(_norm_match(archivo), []))
                via = "archivo"
            elif torre:
                candidatos = list(by_torre.get(_norm_match(torre), []))
                via = "torre_apto"
            elif cuenta:
                candidatos = list(by_cuenta.get(_norm_match(cuenta), []))
                via = "codigo_cuenta"
            else:
                errores.append(f"fila {n}: sin clave de fila (indice/archivo/…)")
                continue
            if not candidatos:
                errores.append(f"fila {n}: sin match por {via}")
                continue
            if len(candidatos) > 1:
                errores.append(
                    f"fila {n}: {via} ambiguo ({len(candidatos)} filas)"
                )
                continue
            idx = candidatos[0]

        out[int(idx)] = fmi
        aplicados.append({"fila_csv": n, "indice": idx, "fmi": fmi, "via": via})
        if not fmi:
            omitidos += 1

    return {
        "mapa": out,
        "aplicadas": len(aplicados),
        "detalle": aplicados,
        "errores": errores,
        "vacios": omitidos,
    }


def plantilla_csv_lote(
    resultados: Iterable[dict[str, Any]],
    *,
    solo_emitibles: bool = True,
    fmi_por_indice: Optional[dict[int, str]] = None,
) -> str:
    """
    CSV con columnas indice,archivo,torre_apto,codigo_cuenta,fmi
    para rellenar offline y re-subir.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["indice", "archivo", "torre_apto", "codigo_cuenta", "fmi"]
    )
    fmi_map = fmi_por_indice or {}
    emitibles = {"ok", "varios_propietarios", "fallback_pdf"}
    for i, r in enumerate(resultados or []):
        if solo_emitibles and (r.get("estado") not in emitibles):
            continue
        if solo_emitibles and not (r.get("capital_limpio_a_demandar")):
            # Preview UI marca emitible con capital; incluir igual si estado ok.
            if r.get("estado") not in emitibles:
                continue
        writer.writerow(
            [
                i,
                r.get("archivo") or "",
                _torre_de_resultado(r),
                r.get("codigo_cuenta") or "",
                fmi_map.get(i, ""),
            ]
        )
    return buf.getvalue()
