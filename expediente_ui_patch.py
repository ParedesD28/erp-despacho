"""UI final del expediente: vista de solo lectura + editor modal.

Mantiene el diseño de consulta del expediente y concentra toda edición en un
modal. Usa cursores RealDictCursor de forma consistente y delega el cálculo de
etapa a expediente_workflow_patch.
"""
from decimal import Decimal, InvalidOperation
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.encoders import jsonable_encoder
from psycopg2.extras import RealDictCursor
import main
import expediente_workflow_patch as workflow

app = main.app
templates = main.templates


def _conn():
    return main.db_pool.getconn()


def _release(conn):
    if conn is not None:
        main.db_pool.putconn(conn)


def _table_exists(cur, table):
    return bool(workflow._table_exists(cur, table))


def _cols(cur, table):
    return set(workflow._cols(cur, table))


def _redirect(path, **params):
    from urllib.parse import urlencode
    query = urlencode(params)
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=303)


def _get_process(cur, radicado):
    return workflow._get_process(cur, radicado)


def _parse_money(value):
    text = str(value or "").strip().replace("$", "").replace(" ", "")
    if not text:
        return Decimal("0")
    try:
        if "," in text:
            # 1.234.567,89 -> 1234567.89
            text = text.replace(".", "").replace(",", ".")
        elif text.count(".") > 1:
            text = text.replace(".", "")
        return Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("Las pretensiones deben ser un valor monetario válido")


def _contact_options(cur):
    return workflow._contact_options(cur)


def _get_abogados(cur):
    if not _table_exists(cur, "abogados"):
        return []
    cur.execute("SELECT id,nombre FROM abogados ORDER BY nombre ASC")
    return [dict(r) for r in cur.fetchall()]


def _get_crm(cur, proceso, ids):
    return workflow._get_crm_agreements(cur, proceso.get("inmueble_id"), ids)


def _get_audit(cur, radicado):
    if not _table_exists(cur, "expediente_ediciones"):
        return []
    cur.execute(
        "SELECT id,fecha,usuario,accion FROM expediente_ediciones "
        "WHERE radicado_interno=%s ORDER BY fecha DESC LIMIT 20",
        (radicado,),
    )
    return [dict(r) for r in cur.fetchall()]


def expediente_detalle_final(request: Request, radicado: str):
    conn = _conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                proceso = _get_process(cur, radicado)
                if not proceso:
                    raise HTTPException(status_code=404, detail="Expediente no encontrado")
                # Siempre sincronizamos la etapa al entrar al expediente.
                stage = workflow._sync_stage(cur, radicado)
                if stage:
                    proceso["etapa_actual"] = stage
                demandantes = workflow._get_demandantes(cur, proceso)
                demandados = workflow._get_demandados(cur, radicado)
                actuaciones = workflow._get_actuaciones(cur, radicado)
                contacts = _contact_options(cur)
                abogados = _get_abogados(cur)
                ids_partes = [x.get("identificacion") for x in demandantes + demandados]
                acuerdos = _get_crm(cur, proceso, ids_partes)
                auditoria = _get_audit(cur, radicado)
                medidas = [x.strip() for x in str(proceso.get("medidas_cautelares") or "").splitlines() if x.strip()]
                contexto = {
                    "request": request,
                    "proceso": proceso,
                    "demandantes": demandantes,
                    "demandados": demandados,
                    "contactos": contacts,
                    "abogados": abogados,
                    "actuaciones": actuaciones,
                    "acuerdos_crm": acuerdos,
                    "audit_ediciones": auditoria,
                    "medidas": medidas,
                    "demandante_ids": {x.get("identificacion") for x in demandantes},
                    "demandado_ids": {x.get("identificacion") for x in demandados},
                }
                return templates.TemplateResponse(
                    request=request,
                    name="detalle_expediente_v3.html",
                    context=contexto,
                )
    finally:
        _release(conn)


@app.get("/expediente/{radicado}", include_in_schema=False)
def expediente_detalle_final_route(request: Request, radicado: str):
    return expediente_detalle_final(request, radicado)


# Eliminar las rutas anteriores para que este editor sea la única fuente.
app.router.routes[:] = [
    r for r in app.router.routes
    if not (
        getattr(r, "path", None) in {"/expediente/{radicado}", "/expediente/guardar-estructurado"}
        and getattr(r, "methods", set())
    )
]
app.get("/expediente/{radicado}", include_in_schema=False)(expediente_detalle_final_route)


@app.post("/expediente/guardar-estructurado", include_in_schema=False)
async def guardar_expediente_final(request: Request):
    form = await request.form()
    radicado = str(form.get("radicado_interno") or "").strip()
    if not radicado:
        return _redirect("/expedientes", error="Expediente+sin+radicado+interno")

    demandante_ids = list(dict.fromkeys(str(x).strip() for x in form.getlist("demandante_id") if str(x).strip()))
    demandado_ids = list(dict.fromkeys(str(x).strip() for x in form.getlist("demandado_id") if str(x).strip()))
    medidas = [str(x).strip() for x in form.getlist("medida") if str(x).strip()]

    conn = _conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                proceso = _get_process(cur, radicado)
                if not proceso:
                    raise HTTPException(status_code=404, detail="Expediente no encontrado")
                cols = _cols(cur, "procesos")
                if not _table_exists(cur, "contactos"):
                    raise RuntimeError("No existe la tabla contactos")
                if not demandante_ids:
                    raise ValueError("Debe quedar al menos un demandante")
                if not demandado_ids:
                    raise ValueError("Debe quedar al menos un demandado")

                all_ids = list(dict.fromkeys(demandante_ids + demandado_ids))
                placeholders = ",".join(["%s"] * len(all_ids))
                cur.execute(
                    f"SELECT identificacion,nombre FROM contactos WHERE identificacion IN ({placeholders})",
                    all_ids,
                )
                contacts = {str(r["identificacion"]): r["nombre"] for r in cur.fetchall()}
                if set(all_ids) != set(contacts):
                    raise ValueError("Hay una parte seleccionada que no existe en Contactos")

                rama = str(form.get("radicado_rama") or "").strip()
                if "radicado_rama" in cols and rama:
                    cur.execute(
                        "SELECT 1 FROM procesos WHERE radicado_rama=%s AND radicado_interno<>%s LIMIT 1",
                        (rama, radicado),
                    )
                    if cur.fetchone():
                        raise ValueError("El radicado Rama ya pertenece a otro expediente")

                editable = {
                    "radicado_rama": rama,
                    "naturaleza": str(form.get("naturaleza") or "").strip(),
                    "juzgado": str(form.get("juzgado") or "").strip(),
                    "estado": str(form.get("estado") or "Activo").strip() or "Activo",
                    "pretensiones": _parse_money(form.get("pretensiones")),
                }
                if "medidas_cautelares" in cols:
                    editable["medidas_cautelares"] = "\n".join(medidas)
                if "abogado_id" in cols:
                    abogado_id = str(form.get("abogado_id") or "").strip()
                    editable["abogado_id"] = int(abogado_id) if abogado_id.isdigit() else None
                if "id_cliente" in cols:
                    editable["id_cliente"] = " | ".join(demandante_ids)
                if "demandante" in cols:
                    editable["demandante"] = " | ".join(contacts[x] for x in demandante_ids)
                if "id_demandado" in cols:
                    editable["id_demandado"] = " | ".join(demandado_ids)
                if "demandado" in cols:
                    editable["demandado"] = " | ".join(contacts[x] for x in demandado_ids)

                usable = [k for k in editable if k in cols and k != "radicado_interno"]
                cur.execute(
                    f"UPDATE procesos SET {', '.join(f'{k}=%s' for k in usable)} WHERE radicado_interno=%s",
                    [editable[k] for k in usable] + [radicado],
                )

                if _table_exists(cur, "procesos_litisconsorcio"):
                    lcols = _cols(cur, "procesos_litisconsorcio")
                    if "radicado_interno" in lcols and "identificacion_demandado" in lcols:
                        cur.execute("DELETE FROM procesos_litisconsorcio WHERE radicado_interno=%s", (radicado,))
                        for ident in demandado_ids:
                            cur.execute(
                                "INSERT INTO procesos_litisconsorcio (radicado_interno,identificacion_demandado) VALUES (%s,%s)",
                                (radicado, ident),
                            )

                workflow._ensure_audit_table(cur)
                current = _get_process(cur, radicado)
                after = {
                    "proceso": jsonable_encoder(current),
                    "demandantes": [dict(x) for x in workflow._get_demandantes(cur, current)],
                    "demandados": [dict(x) for x in workflow._get_demandados(cur, radicado)],
                }
                usuario = getattr(request.state, "user_id", None) or "ERP"
                cur.execute(
                    "INSERT INTO expediente_ediciones (radicado_interno,usuario,accion,antes,despues) VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)",
                    (
                        radicado,
                        str(usuario),
                        "EDICION_MAESTRA",
                        json.dumps({"proceso": proceso, "demandantes": [dict(x) for x in workflow._get_demandantes(cur, proceso)], "demandados": [dict(x) for x in workflow._get_demandados(cur, radicado)]}, default=str),
                        json.dumps(after, default=str),
                    ),
                )
                stage = workflow._sync_stage(cur, radicado)
                print(f"[EXPEDIENTE][EDICION] {radicado} actualizado; etapa={stage or 'sin cambio'}", flush=True)
        return _redirect(f"/expediente/{radicado}", mensaje="Expediente+actualizado")
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[EXPEDIENTE][EDICION] Error {radicado}: {exc!r}", flush=True)
        return _redirect(f"/expediente/{radicado}", error="No+fue+posible+actualizar+el+expediente")
    finally:
        _release(conn)
