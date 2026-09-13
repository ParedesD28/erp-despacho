"""Compatibilidad de expedientes con cursores de psycopg2 de cualquier tipo.

Evita accesos posicionales sobre RealDictRow y normaliza helpers del workflow.
"""
from collections.abc import Mapping
import expediente_workflow_patch as workflow


def _first(row, key, index=0, default=None):
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return default


def _cols(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return {_first(row, "column_name") for row in cur.fetchall() if _first(row, "column_name")}


def _table_exists(cur, table):
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s) AS exists_flag",
        (table,),
    )
    return bool(_first(cur.fetchone(), "exists_flag", default=False))


def _sync_stage(cur, radicado):
    cols = _cols(cur, "procesos")
    if "etapa_actual" not in cols or not _table_exists(cur, "actuaciones"):
        return None
    act_cols = _cols(cur, "actuaciones")
    available = [c for c in ("etapa", "descripcion", "tipificacion_sugerida") if c in act_cols]
    if not available:
        return None
    select = ", ".join(available)
    order_col = "fecha" if "fecha" in act_cols else "id"
    id_clause = ", id DESC" if "id" in act_cols else ""
    cur.execute(
        f"SELECT {select} FROM actuaciones WHERE radicado_interno=%s "
        f"ORDER BY {order_col} DESC NULLS LAST{id_clause} LIMIT 1",
        (radicado,),
    )
    act = cur.fetchone()
    if not act:
        return None
    values = {
        name: (act.get(name) if isinstance(act, Mapping) else act[idx])
        for idx, name in enumerate(available)
    }
    stage = workflow._stage_from_act(values.get("etapa"), values.get("descripcion"), values.get("tipificacion_sugerida"))
    if not stage:
        return None
    cur.execute("UPDATE procesos SET etapa_actual=%s WHERE radicado_interno=%s", (stage, radicado))
    return stage


def _sync_all_stages(cur):
    if not _table_exists(cur, "procesos"):
        return
    cur.execute("SELECT radicado_interno FROM procesos WHERE radicado_interno IS NOT NULL")
    for row in cur.fetchall():
        _sync_stage(cur, _first(row, "radicado_interno"))


workflow._cols = _cols
workflow._table_exists = _table_exists
workflow._sync_stage = _sync_stage
workflow._sync_all_stages = _sync_all_stages

print("[EXPEDIENTE_CURSOR] Helpers del workflow normalizados para RealDictCursor/tuple", flush=True)
