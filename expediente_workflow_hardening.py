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
            # En Colombia un punto seguido de tres cifras suele ser miles.
            if len(fraction) == 3 and integer.replace("-", "").isdigit():
                text = integer + fraction
        return Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("El valor de pretensiones no es válido")


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
                        "SELECT 1 FROM procesos WHERE radicado_rama=%s AND radicado_interno<>%s LIMIT 1",
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
                    valid = {r[0] for r in cur.fetchall()}
                    if set(demandante_ids) != valid:
                        raise ValueError("Uno de los demandantes seleccionados no existe en Contactos")
                    cur.execute("UPDATE procesos SET id_cliente=%s WHERE radicado_interno=%s", (" | ".join(demandante_ids), radicado))

                if workflow._table_exists(cur, "procesos_litisconsorcio"):
                    if not demandado_ids:
                        raise ValueError("Debe quedar al menos un demandado vinculado")
                    marks = ",".join(["%s"] * len(demandado_ids))
                    cur.execute(f"SELECT identificacion FROM contactos WHERE identificacion IN ({marks})", demandado_ids)
                    valid = {r[0] for r in cur.fetchall()}
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
                        names = [r[0] for r in cur.fetchall()]
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
                workflow._sync_stage(cur, radicado)
        return _redirect(f"/expediente/{radicado}", mensaje="Expediente+actualizado")
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[EXPEDIENTE][EDICION] {radicado}: {exc!r}", flush=True)
        return _redirect(f"/expediente/{radicado}", error="No+fue+posible+actualizar+el+expediente.+Revise+los+datos+y+vuelva+a+intentarlo.")
    finally:
        workflow._release(conn)


print("[EXPEDIENTE_HARDENING] Parseo monetario y errores de edicion protegidos", flush=True)
