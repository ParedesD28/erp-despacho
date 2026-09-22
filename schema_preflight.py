"""Preflight de esquema en solo lectura.

Las migraciones son la única fuente de cambios estructurales. Este módulo
solo verifica que producción tenga el contrato que el código actual necesita.
"""
from __future__ import annotations

import db


REQUIRED_COLUMNS = {
    "procesos": {
        "radicado_interno", "radicado_rama", "estado_rama", "tipo_cartera",
        "tipo_proceso_id", "naturaleza", "juzgado", "inmueble_id",
        "abogado_id", "pretensiones",
    },
    "contactos": {"id", "identificacion", "nombre", "telefono"},
    "proceso_partes": {"radicado_interno", "contacto_id", "rol", "es_principal"},
    "tipos_proceso": {"id", "codigo", "nombre", "activo", "familia", "subtipo"},
    "tipos_obligacion": {
        "id", "codigo", "nombre", "activo", "fuente_saldo",
        "requiere_documento", "requiere_conjunto", "requiere_inmueble",
    },
    "obligaciones": {
        "id", "tipo_obligacion_id",
        "acreedor_contacto_id", "inmueble_id", "capital_inicial",
        "fuente_saldo", "proceso_id",
    },
    "proceso_obligaciones": {"radicado_interno", "obligacion_id", "es_principal"},
    "obligacion_movimientos": {"id", "obligacion_id", "tipo", "concepto", "valor", "fecha"},
    "inmuebles_ph": {"id", "contacto_id", "conjunto_id", "torre_apto"},
    "conjuntos_residenciales": {"id", "nombre", "contacto_id"},
    "expensas_ph": {"id", "inmueble_id", "concepto", "valor_capital", "obligation_id"},
    "gestiones_crm": {"id", "obligacion_id", "radicado_interno"},
    "acuerdos_pago": {"id", "obligacion_id"},
    "acuerdos_pago_cuotas": {
        "id", "acuerdo_id", "numero_cuota", "fecha_vencimiento",
        "valor_cuota", "estado", "anulado", "abogado_id",
    },
    "agenda_auditoria": {
        "id", "fecha", "abogado_id", "abogado_nombre", "accion",
        "tipo", "registro_id", "radicado_interno", "inmueble_id",
    },
    "vencimientos": {"id", "obligacion_id"},
    "recaudos_contabilidad": {
        "id", "obligacion_id", "inmueble_id", "identificacion_deudor",
        "nombre_deudor", "fecha_pago", "valor_total", "estado_conciliacion",
    },
    "solicitudes_paz_y_salvo": {
        "id", "obligacion_id", "inmueble_id", "recaudo_id",
        "identificacion_deudor", "estado",
    },
    "sms_cola_envios": {
        "id", "obligacion_id", "saldo_calculado",
        "saldo_fuente", "saldo_verificado", "saldo_calculado_en",
    },
    "inmueble_propietarios": {"inmueble_id", "contacto_id", "es_principal"},
    "historico_tasas": {
        "anio", "mes", "tasa_usura_ea", "ibc_ea", "modalidad", "validado_sfc",
    },
    "schema_migrations": {"version", "applied_at"},
    "expediente_ediciones": {"id", "radicado_interno", "usuario", "accion", "antes", "despues"},
    "obligacion_partes": {"id", "obligacion_id", "contacto_id", "rol", "es_principal"},
    "data_migration_exceptions": {"id", "migration_version", "entity_type", "entity_key", "reason", "status"},
    # cartas_cobro_plantillas se crea/verifica en runtime (ensure_cartas_plantillas_table);
    # no bloquea el arranque si la migración aún no se aplicó en Neon.
}


def _columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (table,),
    )
    return {str(row[0]).lower() for row in cur.fetchall()}


def verify() -> None:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            missing_tables = []
            missing_columns: list[str] = []

            for table, expected in REQUIRED_COLUMNS.items():
                actual = _columns(cur, table)
                if not actual:
                    missing_tables.append(table)
                    continue
                diff = sorted(expected - actual)
                if diff:
                    missing_columns.append(f"{table}=[{', '.join(diff)}]")

            if missing_tables or missing_columns:
                detail = []
                if missing_tables:
                    detail.append("tablas faltantes: " + ", ".join(missing_tables))
                if missing_columns:
                    detail.append("columnas faltantes: " + "; ".join(missing_columns))
                raise RuntimeError("[SCHEMA PREFLIGHT] " + " | ".join(detail))

            for index_name in (
                "uq_contactos_identificacion_idx",
                "uq_procesos_radicado_rama_real",
                "uq_proceso_obligacion_principal",
            ):
                cur.execute(
                    """
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname='public' AND indexname=%s
                    LIMIT 1
                    """,
                    (index_name,),
                )
                if not cur.fetchone():
                    raise RuntimeError(f"[SCHEMA PREFLIGHT] Falta índice requerido: {index_name}")

            cur.execute(
                """
                SELECT 1
                FROM schema_migrations
                WHERE version='20260918_proceso_obligacion_base'
                LIMIT 1
                """
            )
            if not cur.fetchone():
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] No está registrada la migración "
                    "20260918_proceso_obligacion_base"
                )

            cur.execute(
                """
                SELECT 1
                FROM schema_migrations
                WHERE version='20260918_fase3_4_endurecimiento'
                LIMIT 1
                """
            )
            if not cur.fetchone():
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] No está registrada la migración "
                    "20260918_fase3_4_endurecimiento"
                )

            cur.execute(
                """
                SELECT 1
                FROM schema_migrations
                WHERE version='20260918_fase5_migracion_controlada'
                LIMIT 1
                """
            )
            if not cur.fetchone():
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] No está registrada la migración "
                    "20260918_fase5_migracion_controlada"
                )



            cur.execute(
                """
                SELECT 1
                FROM schema_migrations
                WHERE version='20260919_fase14_eliminar_legacy_proceso_obligacion'
                LIMIT 1
                """
            )
            if not cur.fetchone():
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] No está registrada la migración "
                    "20260919_fase14_eliminar_legacy_proceso_obligacion"
                )


            cur.execute(
                """
                SELECT 1
                FROM schema_migrations
                WHERE version='20260919_fase15_completar_obligaciones_ejecutivos_historicos'
                LIMIT 1
                """
            )
            if not cur.fetchone():
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] No está registrada la reparación "
                    "20260919_fase15_completar_obligaciones_ejecutivos_historicos"
                )

            cur.execute(
                """
                SELECT COUNT(*)
                FROM procesos p
                WHERE UPPER(COALESCE(p.naturaleza,'')) <> 'VERBAL'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM proceso_obligaciones po
                      WHERE po.radicado_interno=p.radicado_interno
                  )
                """
            )
            if int(cur.fetchone()[0] or 0) != 0:
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] Existen procesos no-VERBAL sin obligación canónica"
                )

            cur.execute(
                """
                SELECT COUNT(*)
                FROM procesos p
                WHERE UPPER(COALESCE(p.naturaleza,'')) <> 'VERBAL'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM proceso_obligaciones po
                      JOIN obligacion_partes op
                        ON op.obligacion_id=po.obligacion_id
                      WHERE po.radicado_interno=p.radicado_interno
                        AND po.es_principal=TRUE
                        AND UPPER(op.rol)='DEUDOR'
                        AND op.es_principal=TRUE
                  )
                """
            )
            if int(cur.fetchone()[0] or 0) != 0:
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] Existen procesos no-VERBAL sin deudor principal "
                    "en su obligación principal"
                )

            cur.execute(
                """
                SELECT COUNT(*)
                FROM tipos_proceso
                WHERE activo=TRUE
                  AND codigo IN ('EJECUTIVO','VERBAL')
                """
            )
            if int(cur.fetchone()[0] or 0) != 2:
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] Los procedimientos EJECUTIVO y VERBAL "
                    "no están correctamente configurados"
                )

            cur.execute(
                """
                SELECT COUNT(*)
                FROM tipos_obligacion
                WHERE activo=TRUE
                """
            )
            if int(cur.fetchone()[0] or 0) == 0:
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] No existen tipos de obligación activos"
                )
    finally:
        conn.release()
