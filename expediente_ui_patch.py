"""Vista de expediente: consulta limpia + editor modal alineado con radicación."""
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


def _redirect(path, **params):
    from urllib.parse import urlencode
    query = urlencode(params)
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=303)


def _parse_money(value):
    text = str(value or "").strip().replace("$", "").replace(" ", "")
    if not text:
        return Decimal("0")
    try:
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        elif text.count(".") > 1:
            text = text.replace(".", "")
        return Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("Las pretensiones deben ser un valor monetario válido")


def _get_abogados(cur):
    if not workflow._table_exists(cur, "abogados"):
        return []
    cur.execute("SELECT id,nombre FROM abogados ORDER BY nombre ASC")
    return [dict(r) for r in cur.fetchall()]


def _contact_options(cur):
    return workflow._contact_options(cur)


def _audit(cur, radicado):
    if not workflow._table_exists(cur, "expediente_ediciones"):
        return []
    cur.execute(
        "SELECT id,fecha,usuario,accion FROM expediente_ediciones WHERE radicado_interno=%s ORDER BY fecha DESC LIMIT 20",
        (radicado,),
    )
    return [dict(r) for r in cur.fetchall()]


def _context_data(cur, request, radicado):
    proceso = workflow._get_process(cur, radicado)
    if not proceso:
        raise HTTPException(status_code=404, detail="Expediente no encontrado")
    stage = workflow._sync_stage(cur, radicado)
    if stage:
        proceso["etapa_actual"] = stage
    demandantes = workflow._get_demandantes(cur, proceso)
    demandados = workflow._get_demandados(cur, radicado)
    actuaciones = workflow._get_actuaciones(cur, radicado)
    contactos = _contact_options(cur)
    abogados = _get_abogados(cur)
    ids = [x.get("identificacion") for x in demandantes + demandados]
    acuerdos = workflow._get_crm_agreements(cur, proceso.get("inmueble_id"), ids)
    audit = _audit(cur, radicado)
    return {
        "request": request,
        "proceso": proceso,
        "demandantes": demandantes,
        "demandados": demandados,
        "contactos": contactos,
        "abogados": abogados,
        "actuaciones": actuaciones,
        "acuerdos_crm": acuerdos,
        "audit_ediciones": audit,
        "demandante_ids": {str(x.get("identificacion")) for x in demandantes if x.get("identificacion")},
        "demandado_ids": {str(x.get("identificacion")) for x in demandados if x.get("identificacion")},
    }


def expediente_detalle_final(request: Request, radicado: str):
    conn = _conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                return templates.TemplateResponse(
                    request=request,
                    name="detalle_expediente_v4.html",
                    context=_context_data(cur, request, radicado),
                )
    finally:
        _release(conn)


# Esta ruta es la única vista GET del detalle del expediente.
app.router.routes[:] = [
    r for r in app.router.routes
    if getattr(r, "path", None) != "/expediente/{radicado}"
]
app.get("/expediente/{radicado}", include_in_schema=False)(expediente_detalle_final)


@app.post("/expediente/guardar-estructurado", include_in_schema=False)
async def guardar_expediente_final(request: Request):
    form = await request.form()
    radicado = str(form.get("radicado_interno") or "").strip()
    if not radicado:
        return _redirect("/expedientes", error="Expediente+sin+radicado+interno")

    demandante_ids = list(dict.fromkeys(str(x).strip() for x in form.getlist("demandante_id") if str(x).strip()))
    demandado_ids = list(dict.fromkeys(str(x).strip() for x in form.getlist("demandado_id") if str(x).strip()))
    medidas = list(dict.fromkeys(str(x).strip() for x in form.getlist("medida") if str(x).strip()))

    conn = _conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                proceso = workflow._get_process(cur, radicado)
                if not proceso:
                    raise HTTPException(status_code=404, detail="Expediente no encontrado")
                cols = workflow._cols(cur, "procesos")
                if not workflow._table_exists(cur, "contactos"):
                    raise ValueError("No existe la tabla de contactos")
                if not demandante_ids:
                    raise ValueError("Debe existir al menos un demandante")
                if not demandado_ids:
                    raise ValueError("Debe existir al menos un demandado")

                all_ids = list(dict.fromkeys(demandante_ids + demandado_ids))
                placeholders = ",".join(["%s"] * len(all_ids))
                cur.execute(
                    f"SELECT identificacion,nombre FROM contactos WHERE identificacion IN ({placeholders})",
                    all_ids,
                )
                contacts = {str(r["identificacion"]): r["nombre"] for r in cur.fetchall()}
                missing = [x for x in all_ids if x not in contacts]
                if missing:
                    raise ValueError("Hay una parte seleccionada que no existe en Contactos")

                before = {
                    "proceso": jsonable_encoder(proceso),
                    "demandantes": [dict(x) for x in workflow._get_demandantes(cur, proceso)],
                    "demandados": [dict(x) for x in workflow._get_demandados(cur, radicado)],
                }

                rama = str(form.get("radicado_rama") or "").strip()
                if not rama:
                    raise ValueError("El radicado Rama Judicial es obligatorio")
                if "radicado_rama" in cols:
                    cur.execute(
                        "SELECT 1 FROM procesos WHERE radicado_rama=%s AND radicado_interno<>%s LIMIT 1",
                        (rama, radicado),
                    )
                    if cur.fetchone():
                        raise ValueError("El radicado Rama ya pertenece a otro expediente")

                naturaleza = str(form.get("naturaleza") or "").strip()
                juzgado = str(form.get("juzgado") or "").strip()
                if not naturaleza:
                    raise ValueError("La naturaleza es obligatoria")

                if "pretensiones" in form:
                    pretensiones = _parse_money(form.get("pretensiones"))
                else:
                    pretensiones = proceso.get("pretensiones")

                editable = {
                    "radicado_rama": rama,
                    "naturaleza": naturaleza,
                    "juzgado": juzgado,
                    "pretensiones": pretensiones,
                }
                # Estado NO forma parte de la edición: se conserva el valor existente.
                if "medidas_cautelares" in cols:
                    editable["medidas_cautelares"] = "\n".join(medidas)
                if "abogado_id" in cols:
                    abogado = str(form.get("abogado_id") or "").strip()
                    editable["abogado_id"] = int(abogado) if abogado.isdigit() else None
                if "id_cliente" in cols:
                    editable["id_cliente"] = " | ".join(demandante_ids)
                if "demandante" in cols:
                    editable["demandante"] = " | ".join(contacts[x] for x in demandante_ids)
                if "id_demandado" in cols:
                    editable["id_demandado"] = " | ".join(demandado_ids)
                if "demandado" in cols:
                    editable["demandado"] = " | ".join(contacts[x] for x in demandado_ids)

                usable = [k for k in editable if k in cols and k != "radicado_interno"]
                if usable:
                    cur.execute(
                        f"UPDATE procesos SET {', '.join(f'{k}=%s' for k in usable)} WHERE radicado_interno=%s",
                        [editable[k] for k in usable] + [radicado],
                    )

                if workflow._table_exists(cur, "procesos_litisconsorcio"):
                    lcols = workflow._cols(cur, "procesos_litisconsorcio")
                    if "radicado_interno" in lcols and "identificacion_demandado" in lcols:
                        cur.execute("DELETE FROM procesos_litisconsorcio WHERE radicado_interno=%s", (radicado,))
                        for ident in demandado_ids:
                            cur.execute(
                                "INSERT INTO procesos_litisconsorcio (radicado_interno,identificacion_demandado) VALUES (%s,%s)",
                                (radicado, ident),
                            )

                workflow._ensure_audit_table(cur)
                after_process = workflow._get_process(cur, radicado)
                after = {
                    "proceso": jsonable_encoder(after_process),
                    "demandantes": [dict(x) for x in workflow._get_demandantes(cur, after_process)],
                    "demandados": [dict(x) for x in workflow._get_demandados(cur, radicado)],
                }
                usuario = str(getattr(request.state, "user_id", None) or "ERP")
                cur.execute(
                    "INSERT INTO expediente_ediciones (radicado_interno,usuario,accion,antes,despues) VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)",
                    (radicado, usuario, "EDICION_RADICACION", json.dumps(before, default=str), json.dumps(after, default=str)),
                )
                workflow._sync_stage(cur, radicado)
                print(f"[EXPEDIENTE][EDICION] {radicado} guardado sin cambiar etapa/estado; partes normalizadas", flush=True)
        return _redirect(f"/expediente/{radicado}", mensaje="Expediente+actualizado")
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[EXPEDIENTE][EDICION] Error {radicado}: {exc!r}", flush=True)
        return _redirect(f"/expediente/{radicado}", error="No+fue+posible+actualizar+el+expediente")
    finally:
        _release(conn)
