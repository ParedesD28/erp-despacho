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


def _create_triggers(cur) -> None:
    """Instala solo la sincronización procesos -> proceso_partes.

    La sincronización inversa se desactiva durante la transición para evitar
    recursividad al insertar/editar un expediente. Los campos históricos de
    procesos siguen siendo la compatibilidad de escritura hasta migrar main.py.
    """
    # Elimina el trigger bidireccional anterior y su función para que un alta
    # de proceso no intente modificar el mismo registro desde un trigger anidado.
    cur.execute(f"DROP TRIGGER IF EXISTS {_TRG_PARTES_A_PROCESO} ON proceso_partes")
    cur.execute(f"DROP FUNCTION IF EXISTS {_FN_PARTES_A_PROCESO}()")

    cur.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FN_PROCESO_A_PARTES}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF pg_trigger_depth() > 1 THEN
                RETURN NEW;
            END IF;

            DELETE FROM proceso_partes
            WHERE radicado_interno = NEW.radicado_interno;

            INSERT INTO proceso_partes (radicado_interno, contacto_id, rol, es_principal)
            SELECT
                NEW.radicado_interno,
                c.id,
                'DEMANDANTE',
                rn = 1
            FROM (
                SELECT
                    trim(value) AS identificacion,
                    row_number() OVER (ORDER BY ordinality) AS rn
                FROM regexp_split_to_table(COALESCE(NEW.id_cliente, ''), '\\s*\\|\\s*')
                     WITH ORDINALITY AS s(value, ordinality)
                WHERE trim(value) <> ''
            ) d
            JOIN contactos c ON c.identificacion = d.identificacion
            ON CONFLICT (radicado_interno, contacto_id, rol) DO NOTHING;

            INSERT INTO proceso_partes (radicado_interno, contacto_id, rol, es_principal)
            SELECT
                NEW.radicado_interno,
                c.id,
                'DEMANDADO',
                rn = 1
            FROM (
                SELECT
                    trim(value) AS identificacion,
                    row_number() OVER (ORDER BY ordinality) AS rn
                FROM regexp_split_to_table(COALESCE(NEW.id_demandado, ''), '\\s*\\|\\s*')
                     WITH ORDINALITY AS s(value, ordinality)
                WHERE trim(value) <> ''
            ) d
            JOIN contactos c ON c.identificacion = d.identificacion
            ON CONFLICT (radicado_interno, contacto_id, rol) DO NOTHING;

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
        INSERT INTO proceso_partes (radicado_interno, contacto_id, rol, es_principal)
        SELECT
            p.radicado_interno,
            c.id,
            'DEMANDANTE',
            ROW_NUMBER() OVER (PARTITION BY p.radicado_interno ORDER BY s.ordinality) = 1
        FROM procesos p
        CROSS JOIN LATERAL regexp_split_to_table(COALESCE(p.id_cliente, ''), '\\s*\\|\\s*')
            WITH ORDINALITY AS s(value, ordinality)
        JOIN contactos c ON c.identificacion = trim(s.value)
        WHERE NOT EXISTS (
            SELECT 1
            FROM proceso_partes pp
            WHERE pp.radicado_interno = p.radicado_interno
        )
        ON CONFLICT (radicado_interno, contacto_id, rol) DO NOTHING
        """
    )

    cur.execute(
        """
        INSERT INTO proceso_partes (radicado_interno, contacto_id, rol, es_principal)
        SELECT
            p.radicado_interno,
            c.id,
            'DEMANDADO',
            ROW_NUMBER() OVER (PARTITION BY p.radicado_interno ORDER BY s.ordinality) = 1
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
        ON CONFLICT (radicado_interno, contacto_id, rol) DO NOTHING
        """
    )
    return pendientes


def ensure_schema() -> None:
    """Instala la capa de compatibilidad normalizada de forma idempotente."""
    conn = None
    try:
        conn = db.get_connection()
        with conn.cursor() as cur:
            required = ("procesos", "contactos", "proceso_partes")
            missing = [table for table in required if not _table_exists(cur, table)]
            if missing:
                raise RuntimeError(
                    "Faltan tablas requeridas para normalizar partes: " + ", ".join(missing)
                )

            for column in ("id_cliente", "demandante", "id_demandado", "demandado"):
                if not _column_exists(cur, "procesos", column):
                    raise RuntimeError(f"Falta columna procesos.{column}")

            _create_partes_indexes(cur)
            _create_triggers(cur)
            backfilled = _backfill_legacy_processes(cur)
            conn.commit()

        log_msg(
            "✅ [PROCESO_PARTES]",
            f"Compatibilidad normalizada activa | procesos iniciales migrados: {backfilled}",
        )
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_msg("⚠️ [PROCESO_PARTES]", f"No se pudo activar la sincronización: {exc}")
    finally:
        if conn is not None:
            conn.release()
