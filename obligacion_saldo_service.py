"""Motor único de saldo por obligación.

El tipo de obligación decide la fuente:
- EXPENSAS_PH: liquidador PH existente.
- OBLIGACION: capital inicial + movimientos genéricos.
- cualquier otra fuente: saldo no verificable.

Nunca usa procesos.pretensiones como saldo.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from psycopg2.extras import RealDictCursor
from zoneinfo import ZoneInfo

import db
import liquidador

TZ_COLOMBIA = ZoneInfo("America/Bogota")
TIPO_TASA_PH = "Máxima Legal"
TASA_FIJA_PH = 2.5
HONORARIOS_PCT_PH = 23.8
GASTOS_PH = 0.0


def ahora_colombia() -> datetime:
    return datetime.now(TZ_COLOMBIA)


def _as_dict(row: Any, columns: list[str]) -> dict:
    if isinstance(row, dict):
        return dict(row)
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def _saldo_generico(cur, obligacion_id: int) -> dict:
    cur.execute(
        """
        SELECT
            o.id,
            o.capital_inicial,
            o.estado,
            o.fuente_saldo,
            tob.codigo AS tipo_obligacion_codigo
        FROM obligaciones o
        LEFT JOIN tipos_obligacion tob ON tob.id=o.tipo_obligacion_id
        WHERE o.id=%s
        LIMIT 1
        """,
        (obligacion_id,),
    )
    row = cur.fetchone()
    if not row:
        return {
            "saldo_total": None,
            "saldo_verificado": False,
            "saldo_fuente": "OBLIGACION_INEXISTENTE",
            "saldo_calculado_en": ahora_colombia(),
        }

    data = _as_dict(
        row,
        ["id", "capital_inicial", "estado", "fuente_saldo", "tipo_obligacion_codigo"],
    )
    capital = Decimal(str(data.get("capital_inicial") or 0))

    cur.execute(
        """
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN tipo IN ('CARGO','AJUSTE_CARGO') THEN valor
                    WHEN tipo IN ('ABONO','AJUSTE_ABONO') THEN -valor
                    ELSE 0
                END
            ),0) AS variacion
        FROM obligacion_movimientos
        WHERE obligacion_id=%s
        """,
        (obligacion_id,),
    )
    mov = cur.fetchone()
    variacion_row = _as_dict(mov, ["variacion"]) if mov else {"variacion": 0}
    saldo = max(Decimal("0"), capital + Decimal(str(variacion_row["variacion"] or 0)))

    return {
        "saldo_total": round(float(saldo), 2),
        "saldo_verificado": True,
        "saldo_fuente": "OBLIGACION_MOVIMIENTOS",
        "saldo_calculado_en": ahora_colombia(),
        "detalle": {
            "capital_inicial": float(capital),
            "variacion_movimientos": float(variacion_row["variacion"] or 0),
            "tipo_obligacion": data.get("tipo_obligacion_codigo"),
            "estado": data.get("estado"),
        },
    }


def _saldo_ph(obligacion: dict, fecha_corte: Optional[date]) -> dict:
    inmueble_id = obligacion.get("inmueble_id")
    if not inmueble_id:
        return {
            "saldo_total": None,
            "saldo_verificado": False,
            "saldo_fuente": "EXPENSAS_PH_SIN_INMUEBLE",
            "saldo_calculado_en": ahora_colombia(),
        }

    try:
        resultados, resumen, _ = liquidador.motor_calculo_judicial(
            int(inmueble_id),
            TIPO_TASA_PH,
            TASA_FIJA_PH,
            HONORARIOS_PCT_PH,
            GASTOS_PH,
            fecha_corte or ahora_colombia().date(),
            autocausar=False,
            obligacion_id=int(obligacion["id"]),
        )
    except Exception as exc:
        return {
            "saldo_total": None,
            "saldo_verificado": False,
            "saldo_fuente": "LIQUIDADOR_PH_ERROR",
            "saldo_calculado_en": ahora_colombia(),
            "saldo_error": str(exc)[:250],
        }
    if not resultados or not resumen:
        return {
            "saldo_total": None,
            "saldo_verificado": False,
            "saldo_fuente": "EXPENSAS_PH_SIN_DEUDA",
            "saldo_calculado_en": ahora_colombia(),
        }

    return {
        "saldo_total": round(float(resumen.get("gran_total") or 0), 2),
        "saldo_verificado": True,
        "saldo_fuente": "LIQUIDADOR_PH",
        "saldo_calculado_en": ahora_colombia(),
        "detalle_liquidacion": resumen,
    }


def calcular_saldo_obligacion(
    obligacion_id: Optional[int],
    *,
    fecha_corte: Optional[date] = None,
    conn=None,
) -> dict:
    if not obligacion_id:
        return {
            "saldo_total": None,
            "saldo_verificado": False,
            "saldo_fuente": "SIN_OBLIGACION",
            "saldo_calculado_en": ahora_colombia(),
        }

    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    o.id,
                    o.inmueble_id,
                    o.fuente_saldo,
                    o.estado,
                    tob.codigo AS tipo_obligacion_codigo
                FROM obligaciones o
                LEFT JOIN tipos_obligacion tob ON tob.id=o.tipo_obligacion_id
                WHERE o.id=%s
                LIMIT 1
                """,
                (int(obligacion_id),),
            )
            row = cur.fetchone()
            if not row:
                return {
                    "saldo_total": None,
                    "saldo_verificado": False,
                    "saldo_fuente": "OBLIGACION_INEXISTENTE",
                    "saldo_calculado_en": ahora_colombia(),
                }

            ob = _as_dict(
                row,
                ["id", "inmueble_id", "fuente_saldo", "estado", "tipo_obligacion_codigo"],
            )

            fuente = str(ob.get("fuente_saldo") or "").upper()
            if fuente == "EXPENSAS_PH" or ob.get("tipo_obligacion_codigo") == "CUOTAS_ADMINISTRACION":
                result = _saldo_ph(ob, fecha_corte)
            elif fuente == "OBLIGACION":
                result = _saldo_generico(cur, int(obligacion_id))
            else:
                result = {
                    "saldo_total": None,
                    "saldo_verificado": False,
                    "saldo_fuente": fuente or "FUENTE_NO_CONFIGURADA",
                    "saldo_calculado_en": ahora_colombia(),
                }

            result["obligacion_id"] = int(obligacion_id)
            result["tipo_obligacion"] = ob.get("tipo_obligacion_codigo")
            return result
    finally:
        if owns_conn and conn is not None:
            conn.release()


def enriquecer_candidatos(candidatos):
    cache: dict[int, dict] = {}
    salida = []

    for candidato in candidatos:
        item = dict(candidato)
        obligation_id = item.get("obligacion_id")

        if obligation_id:
            oid = int(obligation_id)
            if oid not in cache:
                cache[oid] = calcular_saldo_obligacion(oid)
            item.update(cache[oid])
        else:
            item.update(
                {
                    "saldo_total": None,
                    "saldo_verificado": False,
                    "saldo_fuente": "SIN_OBLIGACION",
                    "saldo_calculado_en": ahora_colombia(),
                }
            )

        salida.append(item)

    return salida
