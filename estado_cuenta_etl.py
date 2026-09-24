"""
ETL en memoria: Excel crudo del convertidor COLON → deuda útil para cobro.

No reescribe el texto del concepto. No crea filas sintéticas.
Agrupa por archivo + código de cuenta + titular para que no haya cruces.
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime

import pandas as pd

CLASIFICACION_CUOTA = "CUOTA_ORDINARIA"
CLASIFICACION_EXTRA = "EXTRAORDINARIO"
CLASIFICACION_INTERES = "INTERES"

_COLUMNAS_DEPURADO = (
    "Archivo",
    "Titular",
    "Bloque",
    "Apartamento",
    "Codigo Cuenta",
    "Concepto",
    "Clasificacion",
    "Tipo Documento",
    "Número",
    "Fecha",
    "Valor",
    "Abono",
    "Saldo Inverso",
    "Diferencia Mora",
    "Fecha Inicio Mora",
    "Alerta",
)

_ALIASES = {
    "archivo": "Archivo",
    "titular": "Titular",
    "bloque": "Bloque",
    "apartamento": "Apartamento",
    "apto": "Apartamento",
    "codigo cuenta": "Codigo Cuenta",
    "código cuenta": "Codigo Cuenta",
    "codigo_cuenta": "Codigo Cuenta",
    "concepto": "Concepto",
    "tipo documento": "Tipo Documento",
    "tipo_documento": "Tipo Documento",
    "numero": "Número",
    "número": "Número",
    "fecha": "Fecha",
    "valor": "Valor",
    "abono": "Abono",
    "abonos": "Abono",
}


def _sin_tilde(texto: str) -> str:
    nfd = unicodedata.normalize("NFKD", texto)
    return "".join(ch for ch in nfd if not unicodedata.combining(ch))


def normalizar_texto(valor) -> str:
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return ""
    texto = _sin_tilde(str(valor)).upper()
    texto = re.sub(r"[^A-Z0-9 ]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _distancia(a: str, b: str) -> int:
    """Levenshtein con intercambio de letras vecinas (COUTA ~ CUOTA)."""
    if a == b:
        return 0
    if not a or not b or abs(len(a) - len(b)) > 2:
        return 99
    filas = len(a) + 1
    cols = len(b) + 1
    d = [[0] * cols for _ in range(filas)]
    for i in range(filas):
        d[i][0] = i
    for j in range(cols):
        d[0][j] = j
    for i in range(1, filas):
        for j in range(1, cols):
            costo = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + costo)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _token_cerca(token: str, objetivo: str, max_dist: int) -> bool:
    return _distancia(token, objetivo) <= max_dist


def clasificar_concepto(concepto: str) -> str:
    """
    Clasifica sin modificar el texto original.
    Tolera COUTA, CUOTA ADMINISTRATIVA y APORTE CUOTA DE ADMINISTRACION.
    """
    normal = normalizar_texto(concepto)
    if not normal:
        return CLASIFICACION_EXTRA
    if "INTERES" in normal:
        return CLASIFICACION_INTERES
    tokens = normal.split()
    tiene_cuota = any(_token_cerca(tok, "CUOTA", 1) for tok in tokens)
    tiene_admin = any(
        tok.startswith("ADMIN") and len(tok) >= 5
        or _token_cerca(tok, "ADMINISTRACION", 2)
        or _token_cerca(tok, "ADMINISTRATIVA", 2)
        for tok in tokens
    )
    if tiene_cuota and tiene_admin:
        return CLASIFICACION_CUOTA
    return CLASIFICACION_EXTRA


def _a_float(valor) -> float | None:
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not texto or texto.startswith("#") or set(texto) <= {"#"}:
        return None
    negativo = texto.startswith("(") and texto.endswith(")")
    limpio = texto.replace("(", "").replace(")", "").replace("$", "").replace(" ", "")
    if "," in limpio and "." in limpio:
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif "," in limpio:
        partes = limpio.split(",")
        if len(partes) == 2 and len(partes[1]) <= 2:
            limpio = limpio.replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    try:
        numero = float(limpio)
    except ValueError:
        return None
    return -numero if negativo else numero


def _a_fecha(valor) -> date | None:
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()
    match = re.match(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", texto)
    if not match:
        return None
    anio, mes, dia = (int(parte) for parte in match.groups())
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def _renombrar_columnas(df: pd.DataFrame) -> pd.DataFrame:
    mapa = {}
    for col in df.columns:
        clave = normalizar_texto(col).lower()
        if clave in _ALIASES:
            mapa[col] = _ALIASES[clave]
    return df.rename(columns=mapa)


def _vacio() -> dict:
    return {
        "depurado": pd.DataFrame(columns=list(_COLUMNAS_DEPURADO)),
        "consolidado": pd.DataFrame(),
        "cortes": pd.DataFrame(),
        "certificados": [],
    }


def _fecha_iso(valor) -> str:
    if valor is None or pd.isna(valor):
        return ""
    return pd.Timestamp(valor).date().isoformat()


def _fila_salida(row: pd.Series, alerta: str) -> dict:
    return {
        "Archivo": row.get("Archivo") or "",
        "Titular": row.get("Titular") or "",
        "Bloque": row.get("Bloque") or "",
        "Apartamento": row.get("Apartamento") or "",
        "Codigo Cuenta": row.get("Codigo Cuenta") or "",
        "Concepto": row.get("Concepto") if row.get("Concepto") is not None else "",
        "Clasificacion": row["_clasificacion"],
        "Tipo Documento": row.get("Tipo Documento") or "",
        "Número": row.get("Número") or "",
        "Fecha": _fecha_iso(row["_fecha"]),
        "Valor": row["_valor"],
        "Abono": row["_abono"],
        "Saldo Inverso": round(float(row["_saldo_inverso"]), 2),
        "Diferencia Mora": round(float(row["_diferencia"]), 2),
        "Fecha Inicio Mora": _fecha_iso(row.get("_inicio")),
        "Alerta": alerta,
    }


def _consolidar(depurado: pd.DataFrame) -> pd.DataFrame:
    if depurado.empty:
        return pd.DataFrame(
            columns=[
                "Archivo",
                "Titular",
                "Bloque",
                "Apartamento",
                "Codigo Cuenta",
                "Periodo",
                "Fecha Inicio Mora",
                "Fecha Causacion",
                "Valor Cuota",
                "Valor Extras",
                "Valor Total",
                "Conceptos Originales",
            ]
        )
    trabajo = depurado.copy()
    trabajo["_periodo"] = trabajo["Fecha"].astype(str).str.slice(0, 7)

    filas = []
    claves = [
        "Archivo",
        "Titular",
        "Bloque",
        "Apartamento",
        "Codigo Cuenta",
        "_periodo",
        "Fecha Inicio Mora",
    ]
    for clave, grupo in trabajo.groupby(claves, dropna=False, sort=False):
        conceptos: list[str] = []
        for concepto in grupo["Concepto"].tolist():
            texto = "" if concepto is None else str(concepto)
            if texto not in conceptos:
                conceptos.append(texto)
        cuota = float(grupo.loc[grupo["Clasificacion"] == CLASIFICACION_CUOTA, "Valor"].sum())
        if cuota == 0:
            continue
        extras = float(grupo.loc[grupo["Clasificacion"] == CLASIFICACION_EXTRA, "Valor"].sum())
        fechas = [str(f) for f in grupo["Fecha"].tolist() if str(f).strip() and str(f).lower() != "nan"]
        filas.append(
            {
                "Archivo": clave[0],
                "Titular": clave[1],
                "Bloque": clave[2],
                "Apartamento": clave[3],
                "Codigo Cuenta": clave[4],
                "Periodo": clave[5],
                "Fecha Inicio Mora": clave[6],
                "Fecha Causacion": min(fechas) if fechas else "",
                "Valor Cuota": round(cuota, 2),
                "Valor Extras": round(extras, 2),
                "Valor Total": round(cuota + extras, 2),
                "Conceptos Originales": " | ".join(conceptos),
            }
        )
    return pd.DataFrame(filas)


_MESES = (
    "",
    "ENERO",
    "FEBRERO",
    "MARZO",
    "ABRIL",
    "MAYO",
    "JUNIO",
    "JULIO",
    "AGOSTO",
    "SEPTIEMBRE",
    "OCTUBRE",
    "NOVIEMBRE",
    "DICIEMBRE",
)
_HOJAS_RESERVADAS = {"RESUMEN", "MOVIMIENTOS", "DEPURADO", "CONSOLIDADO", "INICIO MORA"}


def _nombre_hoja(titular: str, apartamento: str, usados: set[str]) -> str:
    bruto = f"{apartamento} {titular}".strip() or "DEUDOR"
    limpio = re.sub(r"[\\/*?:\[\]]", " ", bruto)
    limpio = re.sub(r"\s+", " ", limpio).strip()[:31] or "DEUDOR"
    nombre = limpio
    n = 2
    while nombre.upper() in usados or nombre.upper() in _HOJAS_RESERVADAS:
        sufijo = f" {n}"
        nombre = f"{limpio[: 31 - len(sufijo)].rstrip()}{sufijo}"
        n += 1
    usados.add(nombre.upper())
    return nombre


def armar_certificados(consolidado: pd.DataFrame) -> list[dict]:
    """Una tabla por deudor. El saldo nace en el último mes y crece hacia arriba."""
    if consolidado is None or consolidado.empty:
        return []
    trabajo = consolidado.sort_values(["Archivo", "Titular", "Periodo"], kind="mergesort")
    claves = ["Archivo", "Titular", "Bloque", "Apartamento", "Codigo Cuenta"]
    usados: set[str] = set()
    salida: list[dict] = []
    for clave, grupo in trabajo.groupby(claves, dropna=False, sort=False):
        grupo = grupo.sort_values("Periodo", kind="mergesort")
        base = []
        for _, row in grupo.iterrows():
            cuota = round(float(row["Valor Cuota"] or 0), 2)
            extra = round(float(row["Valor Extras"] or 0), 2)
            periodo = str(row["Periodo"])
            anio, mes = periodo.split("-")
            base.append(
                {
                    "MES": _MESES[int(mes)],
                    "AÑO": int(anio),
                    "CUOTAS ORDINARIAS": cuota or None,
                    "CUOTAS EXTRAORDINARIAS": extra or None,
                    "FECHA CAUSACION": row.get("Fecha Causacion") or "",
                    "CONCEPTOS ORIGINALES": row.get("Conceptos Originales") or "",
                    "_cuota": cuota,
                    "_extra": extra,
                }
            )
        # Ancla en el último mes. Cada fila de arriba = saldo de abajo + cobro de esta fila.
        saldos = [0.0] * len(base)
        if base:
            saldos[-1] = round(base[-1]["_cuota"] + base[-1]["_extra"], 2)
            for i in range(len(base) - 2, -1, -1):
                saldos[i] = round(saldos[i + 1] + base[i]["_cuota"] + base[i]["_extra"], 2)
        filas = []
        for item, saldo in zip(base, saldos):
            filas.append(
                {
                    "MES": item["MES"],
                    "AÑO": item["AÑO"],
                    "CUOTAS ORDINARIAS": item["CUOTAS ORDINARIAS"],
                    "CUOTAS EXTRAORDINARIAS": item["CUOTAS EXTRAORDINARIAS"],
                    "FECHA CAUSACION": item["FECHA CAUSACION"],
                    "SALDO": saldo,
                    "CONCEPTOS ORIGINALES": item["CONCEPTOS ORIGINALES"],
                }
            )
        salida.append(
            {
                "hoja": _nombre_hoja(str(clave[1] or ""), str(clave[3] or ""), usados),
                "archivo": clave[0],
                "titular": clave[1],
                "bloque": clave[2],
                "apartamento": clave[3],
                "codigo_cuenta": clave[4],
                "fecha_inicio_mora": grupo.iloc[0]["Fecha Inicio Mora"],
                "tabla": pd.DataFrame(filas),
            }
        )
    return salida


def acomodar_extraordinarias(df: pd.DataFrame) -> pd.DataFrame:
    """
    La extraordinaria se pone al lado de la cuota ordinaria del mismo mes.
    Sin ordinaria en ese mes, la extraordinaria no sale.
    """
    if df is None or df.empty:
        salida = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        salida["Cuota Extraordinaria"] = pd.Series(dtype="float64")
        salida["Conceptos Extraordinarios"] = pd.Series(dtype="object")
        return salida

    trabajo = df.reset_index(drop=True)
    for col in ("Archivo", "Codigo Cuenta", "Titular", "Concepto", "Fecha", "Valor"):
        if col not in trabajo.columns:
            trabajo[col] = None
    trabajo["_clasificacion"] = trabajo["Concepto"].map(clasificar_concepto)
    trabajo["_periodo"] = trabajo["Fecha"].map(lambda valor: str(valor or "")[:7])
    extra_valor: list[float | None] = [None] * len(trabajo)
    extra_nombres: list[str | None] = [None] * len(trabajo)
    conservar: list[int] = []
    claves = ["Archivo", "Codigo Cuenta", "Titular", "_periodo"]
    for _, grupo in trabajo.groupby(claves, dropna=False, sort=False):
        ordinarias = [int(i) for i in grupo.index if grupo.loc[i, "_clasificacion"] == CLASIFICACION_CUOTA]
        if not ordinarias:
            continue
        conservar.extend(ordinarias)
        extras = grupo.loc[grupo["_clasificacion"] == CLASIFICACION_EXTRA]
        if extras.empty:
            continue
        ancla = ordinarias[0]
        valores = extras["Valor"].map(_a_float).fillna(0.0)
        extra_valor[ancla] = round(float(valores.sum()), 2)
        nombres: list[str] = []
        for concepto in extras["Concepto"].tolist():
            texto = "" if concepto is None else str(concepto)
            if texto and texto not in nombres:
                nombres.append(texto)
        extra_nombres[ancla] = " | ".join(nombres)
    salida = trabajo.loc[conservar].copy()
    salida["Cuota Extraordinaria"] = [extra_valor[i] for i in salida.index]
    salida["Conceptos Extraordinarios"] = [extra_nombres[i] for i in salida.index]
    salida = salida.drop(columns=["_clasificacion", "_periodo"])
    columnas = [col for col in salida.columns if col not in ("Cuota Extraordinaria", "Conceptos Extraordinarios")]
    if "Valor" in columnas:
        columnas.insert(columnas.index("Valor") + 1, "Cuota Extraordinaria")
    else:
        columnas.append("Cuota Extraordinaria")
    columnas.append("Conceptos Extraordinarios")
    return salida.loc[:, columnas].reset_index(drop=True)


def anotar_verificacion(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega, por cuenta y en el orden de la hoja:
    Abono Acumulado (desde la última fila hacia arriba) y
    Diferencia = Saldo del PDF - Abono Acumulado.
    """
    if df is None or df.empty:
        vacio = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        vacio["Abono Acumulado"] = pd.Series(dtype="float64")
        vacio["Diferencia"] = pd.Series(dtype="float64")
        return vacio

    trabajo = df.reset_index(drop=True)
    for col in ("Archivo", "Codigo Cuenta", "Titular"):
        if col not in trabajo.columns:
            trabajo[col] = ""
    if "Abono" not in trabajo.columns:
        trabajo["Abono"] = None
    if "Saldo" not in trabajo.columns:
        trabajo["Saldo"] = None

    abonos = trabajo["Abono"].map(_a_float)
    saldos = trabajo["Saldo"].map(_a_float)
    acumulado = [float("nan")] * len(trabajo)
    diferencia = [float("nan")] * len(trabajo)
    for _, grupo in trabajo.groupby(["Archivo", "Codigo Cuenta", "Titular"], dropna=False, sort=False):
        posiciones = list(grupo.index)
        total = len(posiciones)
        montos = [0.0] * total
        ultimo = abonos.iloc[posiciones[-1]]
        montos[-1] = 0.0 if pd.isna(ultimo) else float(ultimo)
        for i in range(total - 2, -1, -1):
            propio = abonos.iloc[posiciones[i]]
            montos[i] = montos[i + 1] + (0.0 if pd.isna(propio) else float(propio))
        for i, pos in enumerate(posiciones):
            acumulado[pos] = round(montos[i], 2)
            saldo_pdf = saldos.iloc[pos]
            if pd.notna(saldo_pdf):
                diferencia[pos] = round(float(saldo_pdf) - montos[i], 2)
    trabajo["Abono Acumulado"] = acumulado
    trabajo["Diferencia"] = diferencia
    return trabajo


def depurar_movimientos(df: pd.DataFrame) -> dict:
    """
    Aplica el corte de mora y deja solo FAC sin intereses.

    M nace en el abono de la última fila y suma el abono de cada fila hacia arriba.
    La diferencia es el saldo del PDF menos M. El 3.er negativo de esa
    diferencia, contado desde abajo, marca el inicio. Se conserva ese día
    completo y se descarta solo lo anterior.
    """
    if df is None or df.empty:
        return _vacio()

    trabajo = _renombrar_columnas(df.copy())
    for col in ("Archivo", "Titular", "Bloque", "Apartamento", "Codigo Cuenta", "Concepto", "Tipo Documento", "Número"):
        if col not in trabajo.columns:
            trabajo[col] = ""
    if "Valor" not in trabajo.columns or "Abono" not in trabajo.columns or "Fecha" not in trabajo.columns:
        raise ValueError("El Excel no trae columnas Fecha, Valor y Abono.")
    if "Saldo" not in trabajo.columns:
        trabajo["Saldo"] = None

    trabajo["_orden"] = range(len(trabajo))
    trabajo["_fecha"] = pd.to_datetime(trabajo["Fecha"].map(_a_fecha), errors="coerce")
    trabajo["_valor_raw"] = trabajo["Valor"].map(_a_float)
    trabajo["_abono_raw"] = trabajo["Abono"].map(_a_float)
    trabajo["_saldo_raw"] = trabajo["Saldo"].map(_a_float)
    trabajo["_valor"] = trabajo["_valor_raw"].fillna(0.0)
    trabajo["_abono"] = trabajo["_abono_raw"].fillna(0.0)
    trabajo["_clasificacion"] = trabajo["Concepto"].map(clasificar_concepto)
    trabajo["_tipo"] = trabajo["Tipo Documento"].map(lambda v: normalizar_texto(v))

    depurado_filas: list[dict] = []
    cortes: list[dict] = []

    claves = ["Archivo", "Codigo Cuenta", "Titular"]
    for _, grupo in trabajo.groupby(claves, dropna=False, sort=False):
        ordenado = grupo.sort_values(["_fecha", "_orden"], kind="mergesort", na_position="last").reset_index(drop=True)
        total = len(ordenado)
        saldos = [0.0] * total
        if total:
            saldos[-1] = float(ordenado.loc[total - 1, "_abono"])
            for i in range(total - 2, -1, -1):
                saldos[i] = saldos[i + 1] + float(ordenado.loc[i, "_abono"])
        ordenado["_saldo_inverso"] = saldos
        diferencias = []
        for i in range(total):
            saldo_pdf = ordenado.loc[i, "_saldo_raw"]
            if pd.isna(saldo_pdf):
                diferencias.append(float("nan"))
            else:
                diferencias.append(round(float(saldo_pdf) - saldos[i], 2))
        ordenado["_diferencia"] = diferencias
        negativos = [
            i
            for i in range(total - 1, -1, -1)
            if pd.notna(ordenado.loc[i, "_diferencia"]) and ordenado.loc[i, "_diferencia"] < 0
        ]
        inicio = ordenado.loc[negativos[2], "_fecha"] if len(negativos) >= 3 else pd.NaT
        if pd.isna(inicio):
            inicio = pd.NaT
        ordenado["_inicio"] = inicio

        if pd.isna(inicio):
            vigentes = ordenado
        else:
            vigentes = ordenado[ordenado["_fecha"].notna() & (ordenado["_fecha"] >= inicio)]

        for _, row in vigentes.iterrows():
            if row["_clasificacion"] == CLASIFICACION_INTERES or row["_tipo"] != "FAC":
                continue
            alertas = []
            if pd.isna(row["_valor_raw"]):
                alertas.append("valor_ilegible")
            if pd.isna(row["_abono_raw"]):
                alertas.append("abono_ilegible")
            if pd.isna(row["_saldo_raw"]):
                alertas.append("saldo_ilegible")
            if pd.isna(row["_fecha"]):
                alertas.append("fecha_ilegible")
            depurado_filas.append(_fila_salida(row, ",".join(alertas)))

        cortes.append(
            {
                "Archivo": ordenado.loc[0, "Archivo"] if total else "",
                "Titular": ordenado.loc[0, "Titular"] if total else "",
                "Codigo Cuenta": ordenado.loc[0, "Codigo Cuenta"] if total else "",
                "Bloque": ordenado.loc[0, "Bloque"] if total else "",
                "Apartamento": ordenado.loc[0, "Apartamento"] if total else "",
                "Negativos": len(negativos),
                "Corte Aplicado": not pd.isna(inicio),
                "Fecha Inicio Mora": _fecha_iso(inicio),
                "Filas Antes": total,
                "Filas Depuradas": sum(
                    1
                    for _, row in vigentes.iterrows()
                    if row["_clasificacion"] != CLASIFICACION_INTERES and row["_tipo"] == "FAC"
                ),
            }
        )

    depurado = pd.DataFrame(depurado_filas, columns=list(_COLUMNAS_DEPURADO))
    consolidado = _consolidar(depurado)
    return {
        "depurado": depurado,
        "consolidado": consolidado,
        "cortes": pd.DataFrame(cortes),
        "certificados": armar_certificados(consolidado),
    }
