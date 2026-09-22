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


def calcular_totales_cartera(
    *,
    fecha_corte: Optional[date] = None,
    conn=None,
) -> dict:
    """Agrega capital, intereses, honorarios y total actualizado de cartera PH.

    Usa una obligación EXPENSAS_PH activa por inmueble (la de mayor id) y el
    mismo motor de liquidación del liquidador judicial.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()

    capital = 0.0
    intereses = 0.0
    honorarios = 0.0
    valor_cartera = 0.0
    incluidas = 0
    errores = 0
    sin_deuda = 0
    obligaciones: list[dict] = []

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (o.inmueble_id)
                    o.id,
                    o.inmueble_id,
                    o.fuente_saldo,
                    o.estado,
                    tob.codigo AS tipo_obligacion_codigo
                FROM obligaciones o
                LEFT JOIN tipos_obligacion tob ON tob.id = o.tipo_obligacion_id
                WHERE o.inmueble_id IS NOT NULL
                  AND (
                        UPPER(COALESCE(o.fuente_saldo, '')) = 'EXPENSAS_PH'
                     OR tob.codigo = 'CUOTAS_ADMINISTRACION'
                  )
                  AND UPPER(COALESCE(o.estado, 'ACTIVA')) NOT IN (
                        'CANCELADA', 'ANULADA', 'PAGADA', 'INACTIVO', 'INACTIVA'
                  )
                ORDER BY o.inmueble_id, o.id DESC
                """
            )
            obligaciones = [
                _as_dict(
                    row,
                    ["id", "inmueble_id", "fuente_saldo", "estado", "tipo_obligacion_codigo"],
                )
                for row in cur.fetchall()
            ]
    finally:
        # Liberar antes de liquidar: el motor abre conexiones propias.
        if owns_conn and conn is not None:
            conn.release()
            conn = None

    corte = fecha_corte or ahora_colombia().date()
    for ob in obligaciones:
        result = _saldo_ph(ob, corte)
        if not result.get("saldo_verificado"):
            fuente = str(result.get("saldo_fuente") or "")
            if fuente == "EXPENSAS_PH_SIN_DEUDA":
                sin_deuda += 1
            else:
                errores += 1
            continue

        detalle = result.get("detalle_liquidacion") or {}
        cap = float(detalle.get("capital") or 0)
        ints = float(detalle.get("intereses") or 0)
        hon = float(detalle.get("honorarios") or 0)
        total = float(
            detalle.get("gran_total")
            if detalle.get("gran_total") is not None
            else (result.get("saldo_total") or 0)
        )

        capital += cap
        intereses += ints
        honorarios += hon
        valor_cartera += total
        incluidas += 1

    return {
        "capital": round(capital, 2),
        "intereses": round(intereses, 2),
        "honorarios": round(honorarios, 2),
        "valor_cartera": round(valor_cartera, 2),
        "total_actualizado": round(valor_cartera, 2),
        "obligaciones_incluidas": incluidas,
        "obligaciones_sin_deuda": sin_deuda,
        "obligaciones_error": errores,
        "saldo_calculado_en": ahora_colombia(),
        "fecha_corte": corte,
    }


_SNAPSHOT_EMPTY = {
    "capital": 0.0,
    "intereses": 0.0,
    "honorarios": 0.0,
    "valor_cartera": 0.0,
    "total_actualizado": 0.0,
    "obligaciones_incluidas": 0,
    "obligaciones_sin_deuda": 0,
    "obligaciones_error": 0,
    "saldo_calculado_en": None,
    "fecha_corte": None,
    "actualizado": False,
}


def ensure_cartera_snapshot_table(conn=None) -> None:
    """Crea la tabla de snapshot si no existe (una sola fila id=1)."""
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cartera_totales_snapshot (
                        id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                        capital NUMERIC(18,2) NOT NULL DEFAULT 0,
                        intereses NUMERIC(18,2) NOT NULL DEFAULT 0,
                        honorarios NUMERIC(18,2) NOT NULL DEFAULT 0,
                        valor_cartera NUMERIC(18,2) NOT NULL DEFAULT 0,
                        obligaciones_incluidas INTEGER NOT NULL DEFAULT 0,
                        obligaciones_sin_deuda INTEGER NOT NULL DEFAULT 0,
                        obligaciones_error INTEGER NOT NULL DEFAULT 0,
                        fecha_corte DATE,
                        calculado_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        actualizado_por TEXT
                    )
                    """
                )
    finally:
        if owns_conn and conn is not None:
            conn.release()


def leer_snapshot_cartera(*, conn=None) -> dict:
    """Lee el último total de cartera sin liquidar. No toca el liquidador."""
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()
    try:
        ensure_cartera_snapshot_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT capital, intereses, honorarios, valor_cartera,
                       obligaciones_incluidas, obligaciones_sin_deuda,
                       obligaciones_error, fecha_corte, calculado_en
                FROM cartera_totales_snapshot
                WHERE id = 1
                LIMIT 1
                """
            )
            row = cur.fetchone()
        if not row:
            return dict(_SNAPSHOT_EMPTY)

        data = dict(row)
        capital = float(data.get("capital") or 0)
        intereses = float(data.get("intereses") or 0)
        honorarios = float(data.get("honorarios") or 0)
        valor = float(data.get("valor_cartera") or 0)
        calculado = data.get("calculado_en")
        corte = data.get("fecha_corte")
        return {
            "capital": round(capital, 2),
            "intereses": round(intereses, 2),
            "honorarios": round(honorarios, 2),
            "valor_cartera": round(valor, 2),
            "total_actualizado": round(valor, 2),
            "obligaciones_incluidas": int(data.get("obligaciones_incluidas") or 0),
            "obligaciones_sin_deuda": int(data.get("obligaciones_sin_deuda") or 0),
            "obligaciones_error": int(data.get("obligaciones_error") or 0),
            "saldo_calculado_en": calculado,
            "fecha_corte": corte,
            "actualizado": True,
        }
    finally:
        if owns_conn and conn is not None:
            conn.release()


def guardar_snapshot_cartera(totales: dict, *, actualizado_por: str | None = None, conn=None) -> dict:
    """Persiste el resultado de calcular_totales_cartera en la fila única del snapshot."""
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()
    try:
        ensure_cartera_snapshot_table(conn)
        calculado = totales.get("saldo_calculado_en") or ahora_colombia()
        corte = totales.get("fecha_corte")
        if hasattr(corte, "isoformat"):
            pass
        elif corte:
            corte = date.fromisoformat(str(corte)[:10])
        else:
            corte = ahora_colombia().date()

        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO cartera_totales_snapshot (
                        id, capital, intereses, honorarios, valor_cartera,
                        obligaciones_incluidas, obligaciones_sin_deuda,
                        obligaciones_error, fecha_corte, calculado_en, actualizado_por
                    ) VALUES (
                        1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        capital = EXCLUDED.capital,
                        intereses = EXCLUDED.intereses,
                        honorarios = EXCLUDED.honorarios,
                        valor_cartera = EXCLUDED.valor_cartera,
                        obligaciones_incluidas = EXCLUDED.obligaciones_incluidas,
                        obligaciones_sin_deuda = EXCLUDED.obligaciones_sin_deuda,
                        obligaciones_error = EXCLUDED.obligaciones_error,
                        fecha_corte = EXCLUDED.fecha_corte,
                        calculado_en = EXCLUDED.calculado_en,
                        actualizado_por = EXCLUDED.actualizado_por
                    RETURNING capital, intereses, honorarios, valor_cartera,
                              obligaciones_incluidas, obligaciones_sin_deuda,
                              obligaciones_error, fecha_corte, calculado_en
                    """,
                    (
                        float(totales.get("capital") or 0),
                        float(totales.get("intereses") or 0),
                        float(totales.get("honorarios") or 0),
                        float(totales.get("valor_cartera") or totales.get("total_actualizado") or 0),
                        int(totales.get("obligaciones_incluidas") or 0),
                        int(totales.get("obligaciones_sin_deuda") or 0),
                        int(totales.get("obligaciones_error") or 0),
                        corte,
                        calculado,
                        (actualizado_por or "")[:120] or None,
                    ),
                )
                row = cur.fetchone() or {}
        saved = dict(row)
        valor = float(saved.get("valor_cartera") or 0)
        return {
            "capital": round(float(saved.get("capital") or 0), 2),
            "intereses": round(float(saved.get("intereses") or 0), 2),
            "honorarios": round(float(saved.get("honorarios") or 0), 2),
            "valor_cartera": round(valor, 2),
            "total_actualizado": round(valor, 2),
            "obligaciones_incluidas": int(saved.get("obligaciones_incluidas") or 0),
            "obligaciones_sin_deuda": int(saved.get("obligaciones_sin_deuda") or 0),
            "obligaciones_error": int(saved.get("obligaciones_error") or 0),
            "saldo_calculado_en": saved.get("calculado_en"),
            "fecha_corte": saved.get("fecha_corte"),
            "actualizado": True,
        }
    finally:
        if owns_conn and conn is not None:
            conn.release()


def actualizar_snapshot_cartera(
    *,
    fecha_corte: Optional[date] = None,
    actualizado_por: str | None = None,
) -> dict:
    """Liquida toda la cartera una vez y guarda el snapshot."""
    totales = calcular_totales_cartera(fecha_corte=fecha_corte)
    return guardar_snapshot_cartera(totales, actualizado_por=actualizado_por)
