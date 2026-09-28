"""
Integración Bolsa Global ↔ lote PDF estado de cuenta.

Separa la liquidación Art. 1653-1655 del Excel de solo-transcripción
(`generar_excel_lote`). Opera sobre movimientos ya parseados/sellados.
"""
from __future__ import annotations

import io
import json
from typing import Any

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from bolsa_global_mora import determinar_mora_y_capital_limpio


def _meta_cuenta(cuenta: dict[str, Any]) -> dict[str, Any]:
    return {
        "archivo": cuenta.get("archivo") or "",
        "titular": cuenta.get("titular") or "",
        "bloque": cuenta.get("bloque") or "",
        "apartamento": cuenta.get("apartamento") or "",
        "codigo_cuenta": cuenta.get("codigo_cuenta") or "",
        "conjunto": cuenta.get("conjunto") or "",
        "movimientos_extraidos": int(cuenta.get("movimientos_extraidos") or 0),
        "error_parseo": cuenta.get("error") or None,
    }


def calcular_bolsa_por_cuenta(cuenta: dict[str, Any]) -> dict[str, Any]:
    """Ejecuta Bolsa Global sobre una cuenta (un PDF / deudor)."""
    meta = _meta_cuenta(cuenta)
    if cuenta.get("error"):
        vacio = determinar_mora_y_capital_limpio([])
        return {
            **meta,
            **vacio,
            "errores_procesamiento": [
                f"PDF no parseado: {cuenta.get('error')}",
                *list(vacio.get("errores_procesamiento") or []),
            ],
            "omitido": True,
        }

    rows = cuenta.get("rows") or []
    resultado = determinar_mora_y_capital_limpio(rows)
    return {**meta, **resultado, "omitido": False}


def calcular_bolsa_lote(cuentas: list[dict[str, Any]] | None) -> dict[str, Any]:
    """
    Un bloque de resultado por deudor/archivo (sin mezclar rows entre cuentas).

    `cuentas` debe traer `rows` sellados (como en la caché del lote o el
    retorno interno de `procesar_lote_estados_cuenta` antes de strippear).
    """
    cuentas = list(cuentas or [])
    resultados = [calcular_bolsa_por_cuenta(c) for c in cuentas]
    con_capital = [
        r for r in resultados if not r.get("omitido") and (r.get("capital_limpio_a_demandar") or [])
    ]
    return {
        "cuentas_evaluadas": len(resultados),
        "cuentas_con_capital": len(con_capital),
        "total_capital_demandado_lote": round(
            sum(float(r.get("total_capital_demandado") or 0) for r in resultados),
            2,
        ),
        "resultados": resultados,
    }


def resultado_a_json_bytes(payload: dict[str, Any]) -> io.BytesIO:
    raw = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    return io.BytesIO(raw)


def generar_excel_bolsa_global(payload: dict[str, Any]) -> io.BytesIO:
    """
    Excel aparte del de transcripción: Resumen + Capital limpio + Errores.
    No modifica ni reutiliza `generar_excel_lote`.
    """
    resultados = list(payload.get("resultados") or [])
    resumen_rows = []
    capital_rows = []
    error_rows = []

    for r in resultados:
        resumen_rows.append(
            {
                "Archivo": r.get("archivo") or "",
                "Titular": r.get("titular") or "",
                "Bloque": r.get("bloque") or "",
                "Apartamento": r.get("apartamento") or "",
                "Codigo Cuenta": r.get("codigo_cuenta") or "",
                "Fecha inicio mora": r.get("fecha_inicio_mora") or "",
                "Total capital demandado": r.get("total_capital_demandado"),
                "Bolsa inicial": r.get("bolsa_global_inicial"),
                "Bolsa remanente": r.get("bolsa_remanente_final"),
                "Ítems capital limpio": len(r.get("capital_limpio_a_demandar") or []),
                "Errores": len(r.get("errores_procesamiento") or []),
                "Omitido": bool(r.get("omitido")),
            }
        )
        for item in r.get("capital_limpio_a_demandar") or []:
            capital_rows.append(
                {
                    "Archivo": r.get("archivo") or "",
                    "Titular": r.get("titular") or "",
                    "Fecha": item.get("fecha") or "",
                    "Concepto": item.get("concepto") or "",
                    "Valor a demandar": item.get("valor_a_demandar"),
                    "Nota": item.get("nota") or "",
                    "Mes corte": item.get("mes_corte") or "",
                    "Clase": item.get("clase_concepto") or "",
                }
            )
        for err in r.get("errores_procesamiento") or []:
            error_rows.append(
                {
                    "Archivo": r.get("archivo") or "",
                    "Titular": r.get("titular") or "",
                    "Error": err,
                }
            )

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(resumen_rows).to_excel(writer, index=False, sheet_name="Resumen Bolsa")
        pd.DataFrame(capital_rows).to_excel(writer, index=False, sheet_name="Capital limpio")
        pd.DataFrame(error_rows).to_excel(writer, index=False, sheet_name="Errores")

        header_fill = PatternFill("solid", fgColor="0F172A")
        header_font = Font(color="FFFFFF", bold=True)
        for sheet_name in ("Resumen Bolsa", "Capital limpio", "Errores"):
            ws = writer.book[sheet_name]
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="left", vertical="center")
            for col in ws.columns:
                letter = get_column_letter(col[0].column)
                width = 18
                if col[0].value in ("Archivo", "Titular", "Concepto", "Nota", "Error"):
                    width = 36
                ws.column_dimensions[letter].width = width

    output.seek(0)
    return output
