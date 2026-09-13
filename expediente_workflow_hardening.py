"""Endurecimiento final del workflow de expedientes.

Se carga despues de expediente_workflow_patch para eliminar dos fuentes de
regresiones: parseo ambiguo de valores monetarios y exposicion de excepciones
tecnicas al usuario.
"""
from decimal import Decimal, InvalidOperation
import json

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from psycopg2.extras import RealDictCursor

import main
import expediente_workflow_patch as workflow
import expediente_editor_patch as editor

app = main.app


def _redirect(path, **params):
    from urllib.parse import urlencode
    query = urlencode(params)
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=303)


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


def _row_value(row, key_or_index, default=None):
    """Lee tuple y RealDictRow sin acceso posicional inseguro."""
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
    return {str(_row_value(r, "column_name", _row_value(r, 0, ""))) for r in cur.fetchall() if _row_value(r, "column_name", _row_value(r, 0))}


def _safe_table_exists(cur, table):
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s) AS exists_table",
        (table,),
    )
    row = cur.fetchone()
    return bool(_row_value(row, "exists_table", _row_value(row, 0, False)))


def _safe_sync_stage(cur, radicado):
    """Version compatible con RealDictCursor del cálculo automático de etapa."""
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
    stage = workflow._stage_from_act(values.get("etapa"), values.get("descripcion"), values.get("tipificacion_sugerida"))
    if not stage:
        return None
    cur.execute("UPDATE procesos SET etapa_actual=%s WHERE radicado_interno=%s", (stage, radicado))
    return stage


# Compatibilidad centralizada: todos los parches nuevos pueden trabajar
# indistintamente con RealDictCursor o cursores de tuplas.
workflow._cols = _safe_cols
workflow._table_exists = _safe_table_exists
workflow._sync_stage = _safe_sync_stage
editor._row_value = _row_value
editor._cols = _safe_cols


def _table_names(cur):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    return {str(_row_value(r, "table_name", _row_value(r, 0, ""))) for r in cur.fetchall() if _row_value(r, "table_name", _row_value(r, 0))}


editor._tables = _table_names


def _sync_all_stages_safe(cur):
    if not _safe_table_exists(cur, "procesos"):
        return
    cur.execute("SELECT radicado_interno FROM procesos WHERE radicado_interno IS NOT NULL")
    for row in cur.fetchall():
        radicado = _row_value(row, "radicado_interno", _row_value(row, 0))
        if radicado:
            _safe_sync_stage(cur, radicado)


workflow._sync_all_stages = _sync_all_stages_safe


def _remove_route(path, methods=None):
    methods = methods or set()
    app.router.routes[:] = [
        r for r in app.router.routes
        if not (getattr(r, "path", None) == path and (not methods or methods.intersection(getattr(r, "methods", set()))))
    ]


_remove_route("/expediente/guardar-estructurado", {"POST"})


@app.post("/expediente/guardar-estructurado")
async def guardar_expediente_estructurado_hardened(request: Request):
    form = await request.form()
    radicado = str(form.get("radicado_interno") or "").strip()
    if not radicado:
        return _redirect("/expedientes", error="Expediente+sin+radicado+interno")

    demandante_ids = list(dict.fromkeys(str(x).strip() for x in form.getlist("demandante_id") if str(x).strip()))
    demandado_ids = list(dict.fromkeys(str(x).strip() for x in form.getlist("demandado_id") if str(x).strip()))
    medidas = [str(x).strip() for x in form.getlist("medida") if str(x).strip()]

    conn = workflow._conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                process = workflow._get_process(cur, radicado)
                if not process:
                    raise HTTPException(status_code=404, detail="Expediente no encontrado")
                cols = workflow._cols(cur, "procesos")
                workflow._ensure_audit_table(cur)
                before = workflow._snapshot(process, workflow._get_demandantes(cur, process), workflow._get_demandados(cur, radicado))

                radicado_rama = str(form.get("radicado_rama") or "").strip()
                if "radicado_rama" in cols and radicado_rama and radicado_rama != process.get("radicado_rama"):
                    cur.execute(
                        "SELECT 1 AS duplicate FROM procesos WHERE radicado_rama=%s AND radicado_interno<>%s LIMIT 1",
                        (radicado_rama, radicado),
                    )
                    if cur.fetchone():
                        raise ValueError("El radicado Rama ya pertenece a otro expediente")

                pretensiones = process.get("pretensiones")
                raw_pret = str(form.get("pretensiones") or "").strip()
                if raw_pret:
                    pretensiones = _parse_money(raw_pret, pretensiones)

                editable = {
                    "radicado_rama": radicado_rama,
                    "naturaleza": str(form.get("naturaleza") or "").strip(),
                    "juzgado": str(form.get("juzgado") or "").strip(),
                    "estado": str(form.get("estado") or "Activo").strip() or "Activo",
                    "pretensiones": pretensiones,
                }
                if "medidas_cautelares" in cols:
                    editable["medidas_cautelares"] = "\n".join(medidas)
                usable = [k for k in editable if k in cols and (k != "radicado_rama" or editable[k])]
                if usable:
                    cur.execute(
                        f"UPDATE procesos SET {', '.join(f'{k}=%s' for k in usable)} WHERE radicado_interno=%s",
                        [editable[k] for k in usable] + [radicado],
                    )

                if "id_cliente" in cols:
                    if not demandante_ids:
                        raise ValueError("Debe quedar al menos un demandante vinculado")
                    marks = ",".join(["%s"] * len(demandante_ids))
                    cur.execute(f"SELECT identificacion FROM contactos WHERE identificacion IN ({marks})", demandante_ids)
                    valid = {str(_row_value(r, "identificacion", _row_value(r, 0, ""))) for r in cur.fetchall() if _row_value(r, "identificacion", _row_value(r, 0))}
                    if set(demandante_ids) != valid:
                        raise ValueError("Uno de los demandantes seleccionados no existe en Contactos")
                    cur.execute("UPDATE procesos SET id_cliente=%s WHERE radicado_interno=%s", (" | ".join(demandante_ids), radicado))

                if _safe_table_exists(cur, "procesos_litisconsorcio"):
                    if not demandado_ids:
                        raise ValueError("Debe quedar al menos un demandado vinculado")
                    marks = ",".join(["%s"] * len(demandado_ids))
                    cur.execute(f"SELECT identificacion FROM contactos WHERE identificacion IN ({marks})", demandado_ids)
                    valid = {str(_row_value(r, "identificacion", _row_value(r, 0, ""))) for r in cur.fetchall() if _row_value(r, "identificacion", _row_value(r, 0))}
                    if set(demandado_ids) != valid:
                        raise ValueError("Uno de los demandados seleccionados no existe en Contactos")
                    lcols = workflow._cols(cur, "procesos_litisconsorcio")
                    if "radicado_interno" not in lcols or "identificacion_demandado" not in lcols:
                        raise RuntimeError("La tabla de partes no tiene las columnas esperadas")
                    cur.execute("DELETE FROM procesos_litisconsorcio WHERE radicado_interno=%s", (radicado,))
                    for ident in demandado_ids:
                        cur.execute(
                            "INSERT INTO procesos_litisconsorcio (radicado_interno,identificacion_demandado) VALUES (%s,%s)",
                            (radicado, ident),
                        )
                    if "demandado" in cols and "id_demandado" in cols:
                        cur.execute(f"SELECT nombre FROM contactos WHERE identificacion IN ({marks}) ORDER BY nombre", demandado_ids)
                        names = [str(_row_value(r, "nombre", _row_value(r, 0, ""))) for r in cur.fetchall() if _row_value(r, "nombre", _row_value(r, 0))]
                        cur.execute(
                            "UPDATE procesos SET demandado=%s,id_demandado=%s WHERE radicado_interno=%s",
                            (" | ".join(names), " | ".join(demandado_ids), radicado),
                        )

                after_process = workflow._get_process(cur, radicado)
                after = workflow._snapshot(after_process, workflow._get_demandantes(cur, after_process), workflow._get_demandados(cur, radicado))
                cur.execute(
                    "INSERT INTO expediente_ediciones (radicado_interno,usuario,accion,antes,despues) VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)",
                    (
                        radicado,
                        request.cookies.get("token_erp") or "ERP",
                        "EDICION_MAESTRA",
                        json.dumps(before, default=str),
                        json.dumps(after, default=str),
                    ),
                )
                _safe_sync_stage(cur, radicado)
        return _redirect(f"/expediente/{radicado}", mensaje="Expediente+actualizado")
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[EXPEDIENTE][EDICION] {radicado}: {exc!r}", flush=True)
        return _redirect(f"/expediente/{radicado}", error="No+fue+posible+actualizar+el+expediente.+Revise+los+datos+y+vuelva+a+intentarlo.")
    finally:
        workflow._release(conn)


print("[EXPEDIENTE_HARDENING] Compatibilidad RealDictCursor/tuplas + etapa automatica + parseo monetario + errores protegidos", flush=True)
