"""Selector normalizado de candidatos SMS.

Fuente principal: proceso_partes -> contactos -> procesos vigentes.
El saldo no determina si un demandado puede aparecer; solo se usa como
filtro opcional y como dato informativo del mensaje.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

import db

CARTERAS_VALIDAS = {"PREJURIDICO", "JURIDICO"}
BLOQUEO_REENVIO_HORAS = 24


def _saldo_clause(saldo_minimo: float, saldo_maximo: Optional[float]) -> Tuple[str, List[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if saldo_minimo and float(saldo_minimo) > 0:
        clauses.append("saldo_total >= %s")
        params.append(float(saldo_minimo))
    if saldo_maximo is not None:
        clauses.append("saldo_total <= %s")
        params.append(float(saldo_maximo))
    return (" AND ".join(clauses) if clauses else "TRUE"), params


def candidatos_cartera(
    cur,
    tipo_cartera: str = "",
    saldo_minimo: float = 0,
    saldo_maximo: Optional[float] = None,
    ids: Optional[List[int]] = None,
):
    cartera = (tipo_cartera or "").upper().strip()
    if cartera and cartera not in CARTERAS_VALIDAS:
        raise ValueError("Tipo de cartera no válido.")

    saldo_where, saldo_params = _saldo_clause(saldo_minimo, saldo_maximo)
    params: List[Any] = list(saldo_params)

    where = [
        "c.telefono IS NOT NULL",
        "TRIM(c.telefono) <> ''",
        "regexp_replace(c.telefono, '[^0-9]', ' ', 'g') ~ '(^| )3[0-9]{9}( |$)'",
        "NOT EXISTS ("
        " SELECT 1 FROM sms_cola_envios prev"
        " WHERE prev.contacto_id = c.id"
        "   AND prev.estado = 'ENVIADO'"
        "   AND prev.fecha_envio >= NOW() - INTERVAL '24 hours'"
        ")",
        "NOT EXISTS ("
        " SELECT 1 FROM sms_cola_envios cola"
        " WHERE cola.contacto_id = c.id"
        "   AND cola.estado IN ('PENDIENTE','EN_PROCESO')"
        ")",
        f"{saldo_where}",
    ]

    if cartera:
        where.append("COALESCE(v.tipo_cartera, 'PREJURIDICO') = %s")
        params.append(cartera)
    if ids:
        where.append("c.id = ANY(%s)")
        params.append(ids)

    query = f"""
        WITH vigentes AS (
            SELECT DISTINCT ON (pp.contacto_id)
                pp.contacto_id,
                p.radicado_interno,
                p.inmueble_id,
                p.tipo_cartera,
                p.naturaleza,
                p.etapa_actual,
                p.pretensiones
            FROM proceso_partes pp
            JOIN procesos p
              ON p.radicado_interno = pp.radicado_interno
            WHERE pp.rol = 'DEMANDADO'
              AND LOWER(TRIM(COALESCE(p.estado, 'Activo'))) NOT IN (
                  'inactivo', 'cerrado', 'terminado', 'cancelado',
                  'archivado', 'anulado', 'finalizado', 'suspendido'
              )
            ORDER BY
                pp.contacto_id,
                CASE WHEN UPPER(COALESCE(p.tipo_cartera, '')) = 'JURIDICO'
                     THEN 0 ELSE 1 END,
                p.radicado_interno DESC
        ),
        saldo_ph AS (
            SELECT
                e.inmueble_id,
                COALESCE(SUM(
                    CASE WHEN LOWER(COALESCE(e.concepto, '')) <> 'abono'
                         THEN e.valor_capital ELSE -e.valor_capital END
                ), 0) AS saldo_total
            FROM expensas_ph e
            GROUP BY e.inmueble_id
        ),
        saldo_obligaciones AS (
            SELECT
                o.identificacion_deudor,
                COALESCE(SUM(COALESCE(o.capital, 0)), 0) AS saldo_total
            FROM obligaciones o
            WHERE LOWER(COALESCE(o.estado, '')) NOT IN (
                'pagada', 'cancelada', 'saldada', 'a paz y salvo'
            )
            GROUP BY o.identificacion_deudor
        ),
        base AS (
            SELECT
                c.id AS contacto_id,
                c.identificacion,
                c.nombre,
                c.telefono,
                v.radicado_interno,
                v.naturaleza,
                v.etapa_actual,
                v.tipo_cartera,
                v.inmueble_id,
                i.conjunto_residencial,
                i.torre_apto,
                CASE
                    WHEN v.inmueble_id IS NOT NULL AND COALESCE(sp.saldo_total, 0) > 0
                        THEN sp.saldo_total
                    WHEN COALESCE(so.saldo_total, 0) > 0
                        THEN so.saldo_total
                    WHEN COALESCE(v.pretensiones, 0) > 0
                        THEN v.pretensiones
                    ELSE 0
                END AS saldo_total,
                CASE
                    WHEN v.inmueble_id IS NOT NULL AND COALESCE(sp.saldo_total, 0) > 0
                        THEN 'EXPENSAS_PH'
                    WHEN COALESCE(so.saldo_total, 0) > 0
                        THEN 'OBLIGACION'
                    WHEN COALESCE(v.pretensiones, 0) > 0
                        THEN 'PRETENSIONES'
                    ELSE 'SIN_VALOR'
                END AS saldo_fuente
            FROM vigentes v
            JOIN contactos c ON c.id = v.contacto_id
            LEFT JOIN inmuebles_ph i ON i.id = v.inmueble_id
            LEFT JOIN saldo_ph sp ON sp.inmueble_id = v.inmueble_id
            LEFT JOIN saldo_obligaciones so ON so.identificacion_deudor = c.identificacion
        )
        SELECT
            contacto_id,
            identificacion,
            nombre,
            telefono,
            radicado_interno,
            naturaleza,
            etapa_actual,
            tipo_cartera,
            inmueble_id,
            COALESCE(conjunto_residencial, 'Proceso ' || radicado_interno) AS conjunto_residencial,
            COALESCE(torre_apto, '') AS torre_apto,
            saldo_total,
            saldo_fuente
        FROM base
        WHERE {' AND '.join(where)}
        ORDER BY nombre ASC, contacto_id ASC;
    """

    cur.execute(query, params)
    return cur.fetchall()


def _ensure_sms_candidate_indexes() -> None:
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_proceso_partes_sms_candidato "
                    "ON proceso_partes (contacto_id, rol, radicado_interno);"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_contactos_sms_telefono "
                    "ON contactos (id, telefono);"
                )
    finally:
        conn.release()


def install() -> None:
    import sms_router
    _ensure_sms_candidate_indexes()
    sms_router._candidatos_cartera = candidatos_cartera
    print(
        "✅ [SMS CANDIDATOS] Selector normalizado activo: proceso_partes -> contactos | "
        "demandados con proceso vigente + teléfono | bloqueo 24h",
        flush=True,
    )
