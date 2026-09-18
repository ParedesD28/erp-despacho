"""Compatibilidad y sincronización de partes normalizadas del expediente.

La tabla proceso_partes es la fuente estructural de las relaciones entre un
expediente y sus personas/entidades. Durante la transición, este módulo mantiene
los campos históricos de procesos actualizados para no romper módulos que todavía
los leen, pero evita la sincronización bidireccional durante el alta del proceso.

No elimina tablas ni datos heredados. La migración es idempotente y reversible.
"""
from __future__ import annotations

import db
from observability import log_msg

_TRG_PROCESO_A_PARTES = "trg_sync_proceso_a_proceso_partes"
_FN_PROCESO_A_PARTES = "fn_sync_proceso_a_proceso_partes"
_TRG_PARTES_A_PROCESO = "trg_sync_proceso_partes_a_proceso"
_FN_PARTES_A_PROCESO = "fn_sync_proceso_partes_a_proceso"


def _table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        )
        """,
        (table_name,),
    )
    row = cur.fetchone()
    return bool(row[0] if row else False)


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = %s
        )
        """,
        (table_name, column_name),
    )
    row = cur.fetchone()
    return bool(row[0] if row else False)


def _ensure_contactos_identificacion_unique(cur) -> None:
    """Garantiza la unicidad real de contactos.identificacion.

    Las rutas actuales del ERP utilizan ON CONFLICT (identificacion), por lo que
    PostgreSQL debe disponer de una restricción o índice UNIQUE compatible.
    Primero se valida la ausencia de duplicados para no alterar datos existentes.
    """
    cur.execute(
        """
        SELECT identificacion, COUNT(*)
        FROM contactos
        WHERE identificacion IS NOT NULL
        GROUP BY identificacion
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    )
    duplicate = cur.fetchone()
    if duplicate:
        raise RuntimeError(
            "Existen identificaciones duplicadas en contactos; "
            f"no se puede crear la unicidad: {duplicate[0]} ({duplicate[1]} registros)"
        )

    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_contactos_identificacion_idx
            ON public.contactos (identificacion)
        """
    )


def _create_partes_indexes(cur) -> None:
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_proceso_partes_radicado_rol
            ON proceso_partes (radicado_interno, rol)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_proceso_partes_contacto_rol
            ON proceso_partes (contacto_id, rol)
        """
    )


def _insert_partes_if_missing(cur, radicado: str, contacto_id: int, rol: str, es_principal: bool) -> None:
    cur.execute(
        """
        INSERT INTO proceso_partes (radicado_interno, contacto_id, rol, es_principal)
        SELECT %s, %s, %s, %s
        WHERE NOT EXISTS (
            SELECT 1
            FROM proceso_partes
            WHERE radicado_interno = %s
              AND contacto_id = %s
              AND rol = %s
        )
        """,
        (radicado, contacto_id, rol, es_principal, radicado, contacto_id, rol),
    )


def _create_triggers(cur) -> None:
    """Instala solo la sincronización procesos -> proceso_partes.

    La sincronización inversa se desactiva durante la transición para evitar
    recursividad al insertar/editar un expediente. Los campos históricos de
    procesos siguen siendo la compatibilidad de escritura hasta migrar main.py.
    """
    cur.execute(f"DROP TRIGGER IF EXISTS {_TRG_PARTES_A_PROCESO} ON proceso_partes")
    cur.execute(f"DROP FUNCTION IF EXISTS {_FN_PARTES_A_PROCESO}()")

    cur.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FN_PROCESO_A_PARTES}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parte RECORD;
        BEGIN
            IF pg_trigger_depth() > 1 THEN
                RETURN NEW;
            END IF;

            DELETE FROM proceso_partes
            WHERE radicado_interno = NEW.radicado_interno;

            FOR parte IN
                SELECT
                    trim(value) AS identificacion,
                    row_number() OVER (ORDER BY ordinality) AS rn
                FROM regexp_split_to_table(COALESCE(NEW.id_cliente, ''), '\\s*\\|\\s*')
                     WITH ORDINALITY AS s(value, ordinality)
                WHERE trim(value) <> ''
            LOOP
                INSERT INTO proceso_partes (radicado_interno, contacto_id, rol, es_principal)
                SELECT NEW.radicado_interno, c.id, 'DEMANDANTE', parte.rn = 1
                FROM contactos c
                WHERE c.identificacion = parte.identificacion
                  AND NOT EXISTS (
                      SELECT 1
                      FROM proceso_partes pp
                      WHERE pp.radicado_interno = NEW.radicado_interno
                        AND pp.contacto_id = c.id
                        AND pp.rol = 'DEMANDANTE'
                  );
            END LOOP;

            FOR parte IN
                SELECT
                    trim(value) AS identificacion,
                    row_number() OVER (ORDER BY ordinality) AS rn
                FROM regexp_split_to_table(COALESCE(NEW.id_demandado, ''), '\\s*\\|\\s*')
                     WITH ORDINALITY AS s(value, ordinality)
                WHERE trim(value) <> ''
            LOOP
                INSERT INTO proceso_partes (radicado_interno, contacto_id, rol, es_principal)
                SELECT NEW.radicado_interno, c.id, 'DEMANDADO', parte.rn = 1
                FROM contactos c
                WHERE c.identificacion = parte.identificacion
                  AND NOT EXISTS (
                      SELECT 1
                      FROM proceso_partes pp
                      WHERE pp.radicado_interno = NEW.radicado_interno
                        AND pp.contacto_id = c.id
                        AND pp.rol = 'DEMANDADO'
                  );
            END LOOP;

            RETURN NEW;
        END;
        $$;
        """
    )

    cur.execute(f"DROP TRIGGER IF EXISTS {_TRG_PROCESO_A_PARTES} ON procesos")
    cur.execute(
        f"""
        CREATE TRIGGER {_TRG_PROCESO_A_PARTES}
        AFTER INSERT OR UPDATE OF id_cliente, id_demandado ON procesos
        FOR EACH ROW
        EXECUTE FUNCTION {_FN_PROCESO_A_PARTES}()
        """
    )


def _backfill_legacy_processes(cur) -> int:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM procesos p
        WHERE NOT EXISTS (
            SELECT 1
            FROM proceso_partes pp
            WHERE pp.radicado_interno = p.radicado_interno
        )
        """
    )
    row = cur.fetchone()
    pendientes = int(row[0] if row else 0)
    if pendientes == 0:
        return 0

    cur.execute(
        """
        SELECT
            p.radicado_interno,
            c.id,
            ROW_NUMBER() OVER (PARTITION BY p.radicado_interno ORDER BY s.ordinality) = 1 AS es_principal
        FROM procesos p
        CROSS JOIN LATERAL regexp_split_to_table(COALESCE(p.id_cliente, ''), '\\s*\\|\\s*')
            WITH ORDINALITY AS s(value, ordinality)
        JOIN contactos c ON c.identificacion = trim(s.value)
        WHERE NOT EXISTS (
            SELECT 1
            FROM proceso_partes pp
            WHERE pp.radicado_interno = p.radicado_interno
        )
        """
    )
    for radicado, contacto_id, es_principal in cur.fetchall():
        _insert_partes_if_missing(cur, radicado, contacto_id, "DEMANDANTE", bool(es_principal))

    cur.execute(
        """
        SELECT
            p.radicado_interno,
            c.id,
            ROW_NUMBER() OVER (PARTITION BY p.radicado_interno ORDER BY s.ordinality) = 1 AS es_principal
        FROM procesos p
        CROSS JOIN LATERAL regexp_split_to_table(COALESCE(p.id_demandado, ''), '\\s*\\|\\s*')
            WITH ORDINALITY AS s(value, ordinality)
        JOIN contactos c ON c.identificacion = trim(s.value)
        WHERE EXISTS (
            SELECT 1
            FROM proceso_partes pp
            WHERE pp.radicado_interno = p.radicado_interno
              AND pp.rol = 'DEMANDANTE'
        )
          AND NOT EXISTS (
            SELECT 1
            FROM proceso_partes pp
            WHERE pp.radicado_interno = p.radicado_interno
              AND pp.rol = 'DEMANDADO'
        )
        """
    )
    for radicado, contacto_id, es_principal in cur.fetchall():
        _insert_partes_if_missing(cur, radicado, contacto_id, "DEMANDADO", bool(es_principal))

    return pendientes


def ensure_schema() -> None:
    """Verifica la capa normalizada de partes; las migraciones hacen cualquier DDL."""
    conn = None
    try:
        conn = db.get_connection()
        with conn.cursor() as cur:
            required_tables = ("procesos", "contactos", "proceso_partes")
            missing = [table for table in required_tables if not _table_exists(cur, table)]
            if missing:
                raise RuntimeError(
                    "Faltan tablas requeridas para partes: " + ", ".join(missing)
                )

            required_columns = {
                "procesos": {"radicado_interno", "id_cliente", "demandante", "id_demandado", "demandado"},
                "contactos": {"id", "identificacion", "nombre"},
                "proceso_partes": {"radicado_interno", "contacto_id", "rol", "es_principal"},
            }
            for table, expected in required_columns.items():
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=%s
                    """,
                    (table,),
                )
                actual = {str(row[0]) for row in cur.fetchall()}
                missing_cols = sorted(expected - actual)
                if missing_cols:
                    raise RuntimeError(
                        f"Faltan columnas en {table}: {', '.join(missing_cols)}"
                    )

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
                raise RuntimeError("Falta índice UNIQUE de contactos.identificacion")

            cur.execute(
                """
                SELECT 1
                FROM pg_indexes
                WHERE schemaname='public'
                  AND indexname='idx_proceso_partes_radicado_rol'
                LIMIT 1
                """
            )
            if not cur.fetchone():
                raise RuntimeError("Falta índice idx_proceso_partes_radicado_rol")

        log_msg("✅ [PROCESO_PARTES]", "Contrato normalizado verificado; sin DDL de runtime.")
    except Exception as exc:
        log_msg("⚠️ [PROCESO_PARTES]", f"Preflight incompleto: {exc}")
        raise
    finally:
        if conn is not None:
            conn.release()
