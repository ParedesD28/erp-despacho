"""Endurecimiento final del workflow de expedientes.

Mantiene compatibilidad con RealDictCursor/tuple, normaliza valores monetarios,
protege errores técnicos y sincroniza la etapa automática sin depender de los
parches legacy eliminados.
"""
from decimal import Decimal, InvalidOperation

import expediente_workflow_patch as workflow


def _row_value(row, key_or_index, default=None):
    """Lee filas tipo dict/RealDictRow y tuplas sin acceso posicional frágil."""
    if row is None:
        return default
    if isinstance(row, dict):
        if key_or_index in row:
            return row[key_or_index]
        if isinstance(key_or_index, int):
            values = list(row.values())
            return values[key_or_index] if 0 <= key_or_index < len(values) else default
        return default
    try:
        return row[key_or_index]
    except (KeyError, IndexError, TypeError):
        return default


def _safe_cols(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    values = set()
    for row in cur.fetchall():
        value = _row_value(row, "column_name", _row_value(row, 0))
        if value:
            values.add(str(value))
    return values


def _safe_table_exists(cur, table):
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s) AS exists_table",
        (table,),
    )
    row = cur.fetchone()
    return bool(_row_value(row, "exists_table", _row_value(row, 0, False)))


def _safe_sync_stage(cur, radicado):
    cols = _safe_cols(cur, "procesos")
    if "etapa_actual" not in cols or not _safe_table_exists(cur, "actuaciones"):
        return None
    act_cols = _safe_cols(cur, "actuaciones")
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
    values = {name: _row_value(act, name, _row_value(act, idx)) for idx, name in enumerate(available)}
    stage = workflow._stage_from_act(
        values.get("etapa"),
        values.get("descripcion"),
        values.get("tipificacion_sugerida"),
    )
    if not stage:
        return None
    cur.execute("UPDATE procesos SET etapa_actual=%s WHERE radicado_interno=%s", (stage, radicado))
    return stage


def _sync_all_stages_safe(cur):
    if not _safe_table_exists(cur, "procesos"):
        return
    cur.execute("SELECT radicado_interno FROM procesos WHERE radicado_interno IS NOT NULL")
    for row in cur.fetchall():
        radicado = _row_value(row, "radicado_interno", _row_value(row, 0))
        if radicado:
            _safe_sync_stage(cur, radicado)


def _parse_money(value, default=None):
    """Acepta 1.234.567,89 / 1234567.89 / 1234567 sin alterar decimales."""
    text = str(value or "").strip().replace("$", "").replace(" ", "")
    if not text:
        return default
    try:
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        elif text.count(".") > 1:
            text = text.replace(".", "")
        elif "." in text:
            integer, fraction = text.split(".", 1)
            if len(fraction) == 3 and integer.replace("-", "").isdigit():
                text = integer + fraction
        return Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("El valor de pretensiones no es válido")


workflow._row_value = _row_value
workflow._cols = _safe_cols
workflow._table_exists = _safe_table_exists
workflow._sync_stage = _safe_sync_stage
workflow._sync_all_stages = _sync_all_stages_safe
workflow._parse_money = _parse_money

print("[EXPEDIENTE_HARDENING] Compatibilidad RealDictCursor/tuplas + etapa automatica + parseo monetario", flush=True)
