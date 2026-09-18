"""Migración controlada de IDs internos históricos de expedientes.

Convierte únicamente:
    EN REPARTO1 ... EN REPARTO6
en IDs internos secuenciales EXP-xxxx, manteniendo:
    procesos.radicado_rama = 'EN REPARTO'

La migración es transaccional, detecta referencias FK y referencias lógicas
por columna radicado_interno, registra su ejecución y aborta ante cualquier
inconsistencia.
"""
from __future__ import annotations

import json

import db
from observability import log_msg

MIGRATION_KEY = "legacy_en_reparto_1_6_to_exp_v1"
LOCK_KEY = 71302541
LEGACY_IDS = [f"EN REPARTO{i}" for i in range(1, 7)]


def _table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='public' AND table_name=%s
        )
        """,
        (table_name,),
    )
    row = cur.fetchone()
    return bool(row[0] if row else False)


def _ensure_migration_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS migraciones_expedientes (
            id BIGSERIAL PRIMARY KEY,
            migration_key TEXT NOT NULL UNIQUE,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            mapping JSONB NOT NULL,
            referencias_actualizadas JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )


def _already_applied(cur) -> bool:
    cur.execute(
        "SELECT 1 FROM migraciones_expedientes WHERE migration_key=%s LIMIT 1",
        (MIGRATION_KEY,),
    )
    return cur.fetchone() is not None


def _fetch_fk_references(cur) -> list[dict]:
    cur.execute(
        """
        SELECT k.table_name, k.column_name, rc.update_rule
        FROM information_schema.key_column_usage k
        JOIN information_schema.referential_constraints rc
          ON rc.constraint_schema=k.constraint_schema
         AND rc.constraint_name=k.constraint_name
        JOIN information_schema.key_column_usage uk
          ON uk.constraint_schema=rc.unique_constraint_schema
         AND uk.constraint_name=rc.unique_constraint_name
         AND uk.position_in_unique_constraint=k.position_in_unique_constraint
        WHERE k.table_schema='public'
          AND uk.table_schema='public'
          AND uk.table_name='procesos'
          AND uk.column_name='radicado_interno'
        ORDER BY k.table_name, k.column_name
        """
    )
    return [
        {"table": row[0], "column": row[1], "update_rule": str(row[2] or "").upper()}
        for row in cur.fetchall()
    ]


def _fetch_tables_with_radicado_interno(cur) -> list[str]:
    cur.execute(
        """
        SELECT c.table_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema=c.table_schema
         AND t.table_name=c.table_name
        WHERE c.table_schema='public'
          AND c.column_name='radicado_interno'
          AND t.table_type='BASE TABLE'
        ORDER BY c.table_name
        """
    )
    return [row[0] for row in cur.fetchall()]


def _build_reference_plan(cur) -> tuple[list[tuple[str, str]], set[tuple[str, str]]]:
    fk_refs = _fetch_fk_references(cur)
    by_pair: dict[tuple[str, str], set[str]] = {}
    for ref in fk_refs:
        by_pair.setdefault((ref["table"], ref["column"]), set()).add(ref["update_rule"])

    cascade_pairs: set[tuple[str, str]] = set()
    manual_pairs: set[tuple[str, str]] = set()

    for pair, rules in by_pair.items():
        if "CASCADE" in rules and len(rules) > 1:
            raise RuntimeError(
                "Referencia FK ambigua para "
                f"{pair[0]}.{pair[1]}: reglas de actualización {sorted(rules)}"
            )
        if rules == {"CASCADE"}:
            cascade_pairs.add(pair)
        else:
            manual_pairs.add(pair)

    for table in _fetch_tables_with_radicado_interno(cur):
        if table == "procesos":
            continue
        pair = (table, "radicado_interno")
        if pair not in cascade_pairs:
            manual_pairs.add(pair)

    return sorted(manual_pairs), cascade_pairs


def _next_internal_ids(cur, count: int) -> list[str]:
    cur.execute(
        """
        SELECT COALESCE(
            MAX(CAST(SUBSTRING(radicado_interno FROM 5) AS BIGINT)),
            0
        )
        FROM procesos
        WHERE radicado_interno ~ '^EXP-[0-9]+$'
        """
    )
    row = cur.fetchone()
    next_number = int(row[0] or 0) + 1 if row else 1
    target_ids: list[str] = []

    while len(target_ids) < count:
        candidate = f"EXP-{next_number:04d}"
        cur.execute(
            "SELECT 1 FROM procesos WHERE radicado_interno=%s LIMIT 1",
            (candidate,),
        )
        if not cur.fetchone():
            target_ids.append(candidate)
        next_number += 1

    return target_ids


def migrate_legacy_en_reparto() -> dict:
    conn = None
    try:
        conn = db.get_connection()
        with conn.cursor() as cur:
            if not _table_exists(cur, "procesos"):
                raise RuntimeError("No existe la tabla procesos")

            _ensure_migration_table(cur)
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))

            if _already_applied(cur):
                log_msg(
                    "✅ [MIGRACION_EXP]",
                    f"{MIGRATION_KEY} ya fue aplicada; no se ejecuta nuevamente.",
                )
                return {"status": "already_applied", "migration_key": MIGRATION_KEY}

            cur.execute(
                """
                SELECT radicado_interno, radicado_rama
                FROM procesos
                WHERE radicado_interno = ANY(%s)
                ORDER BY radicado_interno
                """,
                (LEGACY_IDS,),
            )
            legacy_rows = cur.fetchall()
            found = {row[0] for row in legacy_rows}
            missing = [old_id for old_id in LEGACY_IDS if old_id not in found]

            if missing:
                raise RuntimeError(
                    "Migración abortada: faltan expedientes históricos "
                    + ", ".join(missing)
                )
            if len(legacy_rows) != len(LEGACY_IDS):
                raise RuntimeError("Migración abortada: no se encontraron exactamente los 6 expedientes históricos.")

            if any(str(row[1] or "").strip().upper() != "EN REPARTO" for row in legacy_rows):
                raise RuntimeError(
                    "Migración abortada: uno de EN REPARTO1..6 ya tiene un radicado_rama distinto de EN REPARTO."
                )

            target_ids = _next_internal_ids(cur, len(LEGACY_IDS))
            mapping = dict(zip(LEGACY_IDS, target_ids, strict=True))
            manual_pairs, cascade_pairs = _build_reference_plan(cur)

            from psycopg2 import sql
            updated_refs: dict[str, int] = {}

            # Referencias no CASCADE: primero hijos, luego padre.
            for old_id, new_id in mapping.items():
                for table, column in manual_pairs:
                    if table == "procesos":
                        continue
                    statement = sql.SQL("UPDATE {}.{} SET {}=%s WHERE {}=%s").format(
                        sql.Identifier("public"),
                        sql.Identifier(table),
                        sql.Identifier(column),
                        sql.Identifier(column),
                    )
                    cur.execute(statement, (new_id, old_id))
                    if cur.rowcount:
                        key = f"{table}.{column}"
                        updated_refs[key] = updated_refs.get(key, 0) + cur.rowcount

            for old_id, new_id in mapping.items():
                cur.execute(
                    """
                    UPDATE procesos
                    SET radicado_interno=%s,
                        radicado_rama='EN REPARTO'
                    WHERE radicado_interno=%s
                    """,
                    (new_id, old_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"La actualización de {old_id} afectó {cur.rowcount} filas; se esperaba 1."
                    )

            # Verificación de referencias residuales.
            verify_pairs = set(manual_pairs) | set(cascade_pairs)
            leftovers: dict[str, int] = {}

            for table, column in sorted(verify_pairs):
                total = 0
                for old_id in LEGACY_IDS:
                    from psycopg2 import sql
                    q = sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE {}=%s").format(
                        sql.Identifier("public"),
                        sql.Identifier(table),
                        sql.Identifier(column),
                    )
                    cur.execute(q, (old_id,))
                    row = cur.fetchone()
                    total += int(row[0] or 0) if row else 0
                if total:
                    leftovers[f"{table}.{column}"] = total

            if leftovers:
                raise RuntimeError(
                    "La verificación detectó referencias antiguas residuales: "
                    + json.dumps(leftovers, ensure_ascii=False, sort_keys=True)
                )

            mapping_json = {
                old_id: {"new_id": new_id, "radicado_rama": "EN REPARTO"}
                for old_id, new_id in mapping.items()
            }

            cur.execute(
                """
                INSERT INTO migraciones_expedientes
                    (migration_key, mapping, referencias_actualizadas)
                VALUES (%s, %s::jsonb, %s::jsonb)
                """,
                (
                    MIGRATION_KEY,
                    json.dumps(mapping_json, ensure_ascii=False),
                    json.dumps(
                        {
                            "manual": updated_refs,
                            "cascade": sorted(f"{t}.{c}" for t, c in cascade_pairs),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )

        conn.commit()

        result = {
            "status": "migrated",
            "migration_key": MIGRATION_KEY,
            "mapping": mapping_json,
            "manual_references": updated_refs,
            "cascade_references": sorted(f"{t}.{c}" for t, c in cascade_pairs),
        }
        log_msg(
            "✅ [MIGRACION_EXP]",
            "Migración EN REPARTO1..6 completada: "
            + json.dumps(mapping_json, ensure_ascii=False),
        )
        return result

    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_msg("🚨 [MIGRACION_EXP]", f"Migración abortada y revertida: {exc!r}")
        raise
    finally:
        if conn is not None:
            conn.release()
