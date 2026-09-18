"""Preflight de esquema en solo lectura.

No crea, altera ni elimina objetos. Las migraciones son la única fuente de
cambios estructurales.
"""
from __future__ import annotations

import db


REQUIRED_COLUMNS = {
    "procesos": {
        "radicado_interno", "radicado_rama", "naturaleza", "juzgado",
        "estado_rama", "tipo_cartera", "tipo_proceso_id", "inmueble_id",
        "abogado_id", "pretensiones",
    },
    "contactos": {"id", "identificacion", "nombre", "telefono"},
    "proceso_partes": {"radicado_interno", "contacto_id", "rol", "es_principal"},
    "tipos_proceso": {"id", "codigo", "nombre", "activo"},
    "tipos_obligacion": {
        "id", "codigo", "nombre", "activo", "fuente_saldo",
        "requiere_documento", "requiere_conjunto", "requiere_inmueble",
    },
    "obligaciones": {
        "id", "tipo_obligacion_id", "deudor_contacto_id",
        "acreedor_contacto_id", "inmueble_id", "capital_inicial",
        "fuente_saldo", "proceso_id",
    },
    "proceso_obligaciones": {"radicado_interno", "obligacion_id", "es_principal"},
    "obligacion_movimientos": {
        "id", "obligacion_id", "tipo", "concepto", "valor", "fecha",
    },
    "inmuebles_ph": {"id", "contacto_id", "conjunto_id", "torre_apto"},
    "conjuntos_residenciales": {"id", "nombre", "contacto_id"},
    "expensas_ph": {"id", "inmueble_id", "concepto", "valor_capital", "obligation_id"},
    "gestiones_crm": {"id", "obligacion_id", "radicado_interno"},
    "acuerdos_pago": {"id", "obligacion_id"},
    "vencimientos": {"id", "obligacion_id"},
    "sms_cola_envios": {
        "id", "obligacion_id", "saldo_calculado",
        "saldo_fuente", "saldo_verificado", "saldo_calculado_en",
    },
    "inmueble_propietarios": {"inmueble_id", "contacto_id"},
    "historico_tasas": {
        "anio", "mes", "tasa_usura_ea", "ibc_ea",
        "modalidad", "validado_sfc",
    },
    "schema_migrations": {"version", "applied_at"},
}


def _table_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (table,),
    )
    return {str(row[0]).lower() for row in cur.fetchall()}


def verify(required_versions: tuple[str, ...] = (
    "20260918_00_runtime_schema_base",
    "20260918_proceso_obligacion_base",
)) -> None:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            missing_tables = []
            missing_columns: dict[str, list[str]] = {}

            for table, expected in REQUIRED_COLUMNS.items():
                actual = _table_columns(cur, table)
                if not actual:
                    missing_tables.append(table)
                    continue
                diff = sorted(expected - actual)
                if diff:
                    missing_columns[table] = diff

            if missing_tables or missing_columns:
                parts = []
                if missing_tables:
                    parts.append("tablas faltantes: " + ", ".join(missing_tables))
                if missing_columns:
                    parts.append(
                        "columnas faltantes: "
                        + "; ".join(
                            f"{table}=[{', '.join(cols)}]"
                            for table, cols in missing_columns.items()
                        )
                    )
                raise RuntimeError("[SCHEMA PREFLIGHT] " + " | ".join(parts))

            cur.execute(
                """
                SELECT 1
                FROM pg_indexes
                WHERE schemaname='public'
                  AND indexname='uq_contactos_identificacion_idx'
                LIMIT 1
                """
            )
            if not cur.fetchone():
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] Falta unicidad de contactos.identificacion"
                )

            cur.execute(
                """
                SELECT 1
                FROM pg_indexes
                WHERE schemaname='public'
                  AND indexname='uq_procesos_radicado_rama_real'
                LIMIT 1
                """
            )
            if not cur.fetchone():
                raise RuntimeError(
                    "[SCHEMA PREFLIGHT] Falta unicidad de radicados Rama reales"
                )

            for required_version in required_versions:
                cur.execute(
                    """
                    SELECT 1
                    FROM schema_migrations
                    WHERE version=%s
                    LIMIT 1
                    """,
                    (required_version,),
                )
                if not cur.fetchone():
                    raise RuntimeError(
                        f"[SCHEMA PREFLIGHT] No está registrada la migración {required_version}"
                    )
    finally:
        conn.release()
