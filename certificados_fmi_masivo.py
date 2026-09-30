"""Carga masiva de FMI para el lote de certificados (pegar lista / CSV / cédula).

No inventa FMI: solo mapea texto que el usuario aporta a índices del preview.
El mapa resultante se envía como `fmi_por_indice` al generar.

Match por cédula: compara dígitos contra titular y co-propietarios/codeudores
listados en `deudores` / `propietarios` del preview (preferencia proyecto:
codeudor ≈ co-propietario en cuotas admin).
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
_COL_CEDULA = frozenset(
    {
        "cedula",
        "cédula",
        "documento",
        "cc",
        "nro_documento",
        "numero_documento",
        "num_documento",
        "identificacion",
        "identificación",
        "doc",
    }
)


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


def normalizar_cedula(valor: Any) -> str:
    """Deja solo dígitos (quita puntos, guiones, espacios, letras)."""
    if valor is None:
        return ""
    # Excel a veces entrega floats (12345678.0)
    if isinstance(valor, float):
        if valor.is_integer():
            return str(int(valor))
        return re.sub(r"\D+", "", str(valor))
    if isinstance(valor, int):
        return str(valor)
    return re.sub(r"\D+", "", str(valor).strip())


def cedulas_de_resultado(resultado: dict[str, Any]) -> set[str]:
    """
    Todas las cédulas asociadas a una cuenta del preview.

    Incluye titular principal, `deudores` / `propietarios` (co-propietarios /
    codeudores) y campos sueltos `titular_cedula`.
    """
    out: set[str] = set()
    r = resultado or {}

    def _add(raw: Any) -> None:
        n = normalizar_cedula(raw)
        if n:
            out.add(n)

    _add(r.get("titular_cedula"))
    sel = r.get("titular_seleccionado") or {}
    if isinstance(sel, dict):
        _add(sel.get("cedula"))

    for key in ("deudores", "propietarios"):
        for p in r.get(key) or []:
            if isinstance(p, dict):
                _add(p.get("cedula"))
            else:
                _add(p)

    datos = r.get("datos_neon") or {}
    if isinstance(datos, dict):
        _add(datos.get("titular_cedula"))
        for key in ("deudores", "propietarios"):
            for p in datos.get(key) or []:
                if isinstance(p, dict):
                    _add(p.get("cedula"))

    return out


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


def _map_header_columns(fieldnames: Iterable[str]) -> dict[str, str]:
    """nombre_canonico → nombre original en el archivo."""
    colmap: dict[str, str] = {}
    for name in fieldnames:
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
        elif key in _COL_CEDULA:
            colmap["cedula"] = name
    return colmap


_CLAVES_FILA = ("indice", "archivo", "torre_apto", "codigo_cuenta", "cedula")


def parse_fmi_csv(texto: str) -> list[dict[str, str]]:
    """
    Parsea CSV/TSV con encabezado.

    Requiere columna `fmi` (o alias) y al menos una clave de fila:
    `indice`, `archivo`, `torre_apto`/`clave_canonica`, `codigo_cuenta` o `cedula`
    (alias: documento, cc, nro_documento, identificacion).
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

    colmap = _map_header_columns(reader.fieldnames)

    if "fmi" not in colmap:
        raise ValueError("CSV debe incluir columna fmi")
    if not any(k in colmap for k in _CLAVES_FILA):
        raise ValueError(
            "CSV debe incluir indice, archivo, torre_apto/clave_canonica, "
            "codigo_cuenta o cedula además de fmi"
        )

    filas: list[dict[str, str]] = []
    for row in reader:
        if not row:
            continue
        item: dict[str, str] = {"fmi": str(row.get(colmap["fmi"]) or "").strip()}
        for canon in _CLAVES_FILA:
            if canon in colmap:
                raw_val = row.get(colmap[canon])
                if raw_val is None:
                    item[canon] = ""
                else:
                    item[canon] = str(raw_val).strip()
        # Saltar filas totalmente vacías
        if not any(item.values()):
            continue
        filas.append(item)
    return filas


def filas_desde_excel(contenido: bytes, *, nombre: str = "") -> list[dict[str, str]]:
    """
    Lee la primera hoja de un .xlsx/.xls y la trata como tabla FMI.

    Requiere encabezado con `fmi` + clave (`cedula` u otras). Usa openpyxl/pandas.
    """
    import pandas as pd

    name_l = (nombre or "").lower()
    engine = None
    if name_l.endswith(".xls") and not name_l.endswith(".xlsx"):
        engine = "xlrd"
    try:
        df = pd.read_excel(io.BytesIO(contenido), engine=engine, dtype=str)
    except Exception as exc:  # noqa: BLE001 — mensaje claro al usuario
        raise ValueError(f"No se pudo leer el Excel: {exc}") from exc
    if df is None or df.empty:
        return []
    # Normalizar NaN → ""
    df = df.fillna("")
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return parse_fmi_csv(buf.getvalue())


def indice_cedulas_preview(
    resultados: list[dict[str, Any]],
) -> dict[str, list[int]]:
    """cédula normalizada → lista de índices de cuenta en el preview."""
    by_ced: dict[str, list[int]] = {}
    for i, r in enumerate(resultados or []):
        for ced in cedulas_de_resultado(r):
            by_ced.setdefault(ced, []).append(i)
    # Deduplicar índices por cédula (misma cuenta listada 2×)
    for ced, idxs in list(by_ced.items()):
        seen: list[int] = []
        for idx in idxs:
            if idx not in seen:
                seen.append(idx)
        by_ced[ced] = seen
    return by_ced


def mapear_por_cedula(
    filas: list[dict[str, str]],
    resultados: list[dict[str, Any]],
    *,
    mapa_existente: Optional[dict[int, str]] = None,
    asignar_conflictos: bool = False,
) -> dict[str, Any]:
    """
    Asigna FMI por cédula (titular o co-propietario/codeudor).

    Default: si una cédula matchea varias cuentas → conflicto, no asigna.
    `asignar_conflictos=True` asignaría a todas (no recomendado; UI no lo usa).
    Filas sin cédula se ignoran aquí (van por `mapear_csv_a_indices`).
    """
    out = dict(mapa_existente or {})
    by_ced = indice_cedulas_preview(resultados)
    aplicados: list[dict[str, Any]] = []
    sin_match: list[dict[str, Any]] = []
    conflictos: list[dict[str, Any]] = []
    omitidos = 0
    errores: list[str] = []

    for n, fila in enumerate(filas, start=2):
        raw_ced = (fila.get("cedula") or "").strip()
        if not raw_ced:
            continue
        fmi = (fila.get("fmi") or "").strip()
        ced = normalizar_cedula(raw_ced)
        if not ced:
            errores.append(f"fila {n}: cédula inválida '{raw_ced}'")
            continue
        candidatos = list(by_ced.get(ced) or [])
        if not candidatos:
            sin_match.append(
                {
                    "fila": n,
                    "cedula": raw_ced,
                    "cedula_norm": ced,
                    "fmi": fmi,
                }
            )
            continue
        if len(candidatos) > 1 and not asignar_conflictos:
            conflictos.append(
                {
                    "fila": n,
                    "cedula": raw_ced,
                    "cedula_norm": ced,
                    "fmi": fmi,
                    "indices": candidatos,
                }
            )
            continue
        for idx in candidatos if asignar_conflictos else candidatos[:1]:
            out[int(idx)] = fmi
            aplicados.append(
                {
                    "fila": n,
                    "indice": idx,
                    "cedula": raw_ced,
                    "cedula_norm": ced,
                    "fmi": fmi,
                    "via": "cedula",
                }
            )
            if not fmi:
                omitidos += 1

    return {
        "mapa": out,
        "aplicadas": len(aplicados),
        "detalle": aplicados,
        "sin_match": sin_match,
        "conflictos": conflictos,
        "errores": errores,
        "vacios": omitidos,
    }


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
    asignar_conflictos_cedula: bool = False,
) -> dict[str, Any]:
    """
    Resuelve cada fila CSV a un índice del preview.

    Prioridad de match por fila:
      indice → cedula (titular/co-propietario) → archivo → torre_apto → codigo_cuenta.
    Cédula ambigua (varias cuentas): conflicto, no asigna (default).
    """
    out = dict(mapa_existente or {})
    aplicados: list[dict[str, Any]] = []
    errores: list[str] = []
    sin_match: list[dict[str, Any]] = []
    conflictos: list[dict[str, Any]] = []
    omitidos = 0

    by_archivo: dict[str, list[int]] = {}
    by_torre: dict[str, list[int]] = {}
    by_cuenta: dict[str, list[int]] = {}
    by_ced = indice_cedulas_preview(resultados)
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
        raw_ced = (fila.get("cedula") or "").strip()
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
        elif raw_ced:
            ced = normalizar_cedula(raw_ced)
            if not ced:
                errores.append(f"fila {n}: cédula inválida '{raw_ced}'")
                continue
            candidatos = list(by_ced.get(ced) or [])
            if not candidatos:
                sin_match.append(
                    {
                        "fila": n,
                        "cedula": raw_ced,
                        "cedula_norm": ced,
                        "fmi": fmi,
                    }
                )
                continue
            if len(candidatos) > 1 and not asignar_conflictos_cedula:
                conflictos.append(
                    {
                        "fila": n,
                        "cedula": raw_ced,
                        "cedula_norm": ced,
                        "fmi": fmi,
                        "indices": candidatos,
                    }
                )
                continue
            via = "cedula"
            targets = candidatos if asignar_conflictos_cedula else candidatos[:1]
            for t_idx in targets:
                out[int(t_idx)] = fmi
                aplicados.append(
                    {
                        "fila_csv": n,
                        "indice": t_idx,
                        "fmi": fmi,
                        "via": via,
                        "cedula": raw_ced,
                        "cedula_norm": ced,
                    }
                )
                if not fmi:
                    omitidos += 1
            continue
        else:
            candidatos = []
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
                errores.append(
                    f"fila {n}: sin clave de fila (indice/cedula/archivo/…)"
                )
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
        "sin_match": sin_match,
        "conflictos": conflictos,
        "vacios": omitidos,
    }


def plantilla_csv_cedulas(
    resultados: Iterable[dict[str, Any]],
    *,
    solo_emitibles: bool = True,
    fmi_por_indice: Optional[dict[int, str]] = None,
) -> str:
    """
    CSV cedula,nombre,torre_apto,codigo_cuenta,fmi — una fila por propietario.

    Sirve para rellenar FMI offline cruzando por CC (titular o codeudor).
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["cedula", "nombre", "torre_apto", "codigo_cuenta", "indice", "fmi"]
    )
    fmi_map = fmi_por_indice or {}
    emitibles = {"ok", "varios_propietarios", "fallback_pdf"}
    for i, r in enumerate(resultados or []):
        if solo_emitibles and (r.get("estado") not in emitibles):
            continue
        torre = _torre_de_resultado(r)
        cuenta = r.get("codigo_cuenta") or ""
        fmi = fmi_map.get(i, "")
        props = list(r.get("deudores") or r.get("propietarios") or [])
        if not props:
            sel = r.get("titular_seleccionado") or {}
            if isinstance(sel, dict) and (sel.get("cedula") or sel.get("nombre")):
                props = [sel]
            elif r.get("titular_cedula") or r.get("titular_pdf"):
                props = [
                    {
                        "cedula": r.get("titular_cedula"),
                        "nombre": r.get("titular_pdf"),
                    }
                ]
        if not props:
            writer.writerow(["", "", torre, cuenta, i, fmi])
            continue
        for p in props:
            if not isinstance(p, dict):
                continue
            writer.writerow(
                [
                    p.get("cedula") or "",
                    p.get("nombre") or "",
                    torre,
                    cuenta,
                    i,
                    fmi,
                ]
            )
    return buf.getvalue()


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
