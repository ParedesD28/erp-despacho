"""Workflow seguro de expedientes.

Centraliza tres reglas sensibles:
- edicion estructurada con auditoria y relaciones de partes, no texto libre;
- etapa_actual derivada del historial de actuaciones;
- vinculacion de acuerdos/promesas del CRM al expediente.

Se carga despues de los parches historicos para reemplazar sus rutas sin tocar
el motor financiero ni las tablas existentes de forma destructiva.
"""
from datetime import date, datetime
import json
import re
import unicodedata

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.encoders import jsonable_encoder
from psycopg2.extras import RealDictCursor

import main

app = main.app
templates = main.templates

CANONICAL_STAGES = [
    "1. Presentación de la demanda",
    "2. Inadmisión",
    "3. Admisión",
    "4. Medidas Cautelares",
    "5. Notificación",
    "6. Excepciones",
    "7. Sentencia",
    "8. Desistimiento tácito",
    "Terminación del Proceso",
]


def _conn():
    return main.db_pool.getconn()


def _release(conn):
    if conn is not None:
        main.db_pool.putconn(conn)


def _norm(value):
    text = str(value or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _cols(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def _table_exists(cur, table):
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s)",
        (table,),
    )
    return bool(cur.fetchone()[0])


def _redirect(path, **params):
    from urllib.parse import urlencode
    query = urlencode(params)
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=303)


def _ensure_audit_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expediente_ediciones (
            id BIGSERIAL PRIMARY KEY,
            radicado_interno TEXT NOT NULL,
            usuario TEXT,
            fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            accion TEXT NOT NULL,
            antes JSONB,
            despues JSONB
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_expediente_ediciones_radicado "
        "ON expediente_ediciones (radicado_interno, fecha DESC)"
    )


def _stage_from_act(etapa, descripcion, tipificacion):
    """Determina la etapa procesal a partir de una actuacion reciente."""
    raw = " ".join(str(x or "") for x in (etapa, tipificacion, descripcion))
    n = _norm(raw)
    if any(k in n for k in ("terminacion del proceso", "terminacion", "archivo definitivo", "paz y salvo procesal")):
        return "Terminación del Proceso"
    if any(k in n for k in ("sentencia", "fallo", "condena", "sentencia ejecutoriada")):
        return "7. Sentencia"
    if any(k in n for k in ("desistimiento tacito", "desistimiento tácito")):
        return "8. Desistimiento tácito"
    if any(k in n for k in ("excepcion", "excepciones", "contestacion a excepciones", "traslado de excepciones")):
        return "6. Excepciones"
    if any(k in n for k in ("notificacion", "notificacion personal", "cita", "citacion", "emplazamiento")):
        return "5. Notificación"
    if any(k in n for k in ("medida cautelar", "medidas cautelares", "embargo", "secuestro", "oficio de embargo", "libramiento de embargo")):
        return "4. Medidas Cautelares"
    if any(k in n for k in ("admisión", "admision", "auto admite", "mandamiento ejecutivo", "libra mandamiento", "oficios")):
        return "3. Admisión"
    if any(k in n for k in ("inadmis", "subsanacion", "subsanación")):
        return "2. Inadmisión"
    if any(k in n for k in ("presentacion de la demanda", "presentación de la demanda", "radicacion", "radicación", "reparto", "reparto y radicacion", "reparto y radicación", "inicio")):
        return "1. Presentación de la demanda"

    normalized_stage = _norm(etapa)
    for stage in CANONICAL_STAGES:
        if _norm(stage) == normalized_stage:
            return stage
    return None


def _sync_stage(cur, radicado):
    """Actualiza procesos.etapa_actual a partir de la actuacion mas reciente."""
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
    values = {name: act[idx] for idx, name in enumerate(available)}
    stage = _stage_from_act(values.get("etapa"), values.get("descripcion"), values.get("tipificacion_sugerida"))
    if not stage:
        return None
    cur.execute("UPDATE procesos SET etapa_actual=%s WHERE radicado_interno=%s", (stage, radicado))
    return stage


def _sync_all_stages(cur):
    if not _table_exists(cur, "procesos"):
        return
    cur.execute("SELECT radicado_interno FROM procesos WHERE radicado_interno IS NOT NULL")
    for row in cur.fetchall():
        _sync_stage(cur, row[0])


def _contact_options(cur):
    if not _table_exists(cur, "contactos"):
        return []
    cur.execute(
        "SELECT identificacion,nombre,tipo FROM contactos "
        "WHERE COALESCE(TRIM(identificacion),'')<>'' ORDER BY nombre ASC LIMIT 3000"
    )
    return [dict(r) for r in cur.fetchall()]


def _get_process(cur, radicado):
    cur.execute(
        "SELECT p.*, a.nombre AS abogado_asignado "
        "FROM procesos p LEFT JOIN abogados a ON a.id=p.abogado_id "
        "WHERE p.radicado_interno=%s LIMIT 1",
        (radicado,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _get_demandantes(cur, proceso):
    ids = [x.strip() for x in str(proceso.get("id_cliente") or "").split("|") if x.strip()]
    if not ids:
        return []
    cur.execute(
        "SELECT identificacion,nombre,tipo FROM contactos WHERE identificacion = ANY(%s) "
        "ORDER BY nombre ASC",
        (ids,),
    )
    found = {r["identificacion"]: dict(r) for r in cur.fetchall()}
    return [found.get(i, {"identificacion": i, "nombre": i, "tipo": "Cliente"}) for i in ids]


def _get_demandados(cur, radicado):
    if not _table_exists(cur, "procesos_litisconsorcio"):
        return []
    cur.execute(
        "SELECT DISTINCT pl.identificacion_demandado AS identificacion, "
        "COALESCE(c.nombre,pl.identificacion_demandado) AS nombre, "
        "COALESCE(c.tipo,'Contraparte') AS tipo "
        "FROM procesos_litisconsorcio pl "
        "LEFT JOIN contactos c ON c.identificacion=pl.identificacion_demandado "
        "WHERE pl.radicado_interno=%s AND COALESCE(TRIM(pl.identificacion_demandado),'')<>'' "
        "ORDER BY nombre ASC",
        (radicado,),
    )
    return [dict(r) for r in cur.fetchall()]


def _get_actuaciones(cur, radicado):
    if not _table_exists(cur, "actuaciones"):
        return []
    cur.execute(
        "SELECT * FROM actuaciones WHERE radicado_interno=%s ORDER BY fecha DESC NULLS LAST, id DESC",
        (radicado,),
    )
    return [dict(r) for r in cur.fetchall()]


def _get_crm_agreements(cur, inmueble_id, identificaciones):
    if not inmueble_id or not _table_exists(cur, "gestiones_crm"):
        return []
    cols = _cols(cur, "gestiones_crm")
    select = [c for c in ("id", "identificacion_deudor", "tipo_contacto", "resumen", "promesa_pago_fecha", "fecha", "usuario", "estado", "anulado") if c in cols]
    if "resumen" not in select:
        return []
    cur.execute(
        f"SELECT {', '.join(select)} FROM gestiones_crm "
        "WHERE inmueble_id=%s AND COALESCE(anulado,FALSE)=FALSE "
        "ORDER BY fecha DESC LIMIT 100",
        (inmueble_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    filtered = []
    wanted = {str(x).strip() for x in identificaciones if str(x).strip()}
    for row in rows:
        text = _norm(row.get("resumen"))
        is_agreement = bool(row.get("promesa_pago_fecha")) or any(k in text for k in ("acuerdo", "promesa de pago", "promesa", "convenio"))
        same_person = not row.get("identificacion_deudor") or str(row.get("identificacion_deudor")).strip() in wanted
        if is_agreement and same_person:
            filtered.append(row)
    return filtered


def _snapshot(proceso, demandantes, demandados):
    return {
        "proceso": proceso,
        "demandantes": demandantes,
        "demandados": demandados,
    }


def _remove_route(path, methods=None):
    methods = methods or set()
    app.router.routes[:] = [
        r for r in app.router.routes
        if not (getattr(r, "path", None) == path and (not methods or methods.intersection(getattr(r, "methods", set()))))
    ]


def expediente_workflow_context(request: Request, radicado: str):
    conn = _conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                process = _get_process(cur, radicado)
                if not process:
                    raise HTTPException(status_code=404, detail="Expediente no encontrado")
                stage = _sync_stage(cur, radicado) or process.get("etapa_actual")
                process["etapa_actual"] = stage
                demandantes = _get_demandantes(cur, process)
                demandados = _get_demandados(cur, radicado)
                actuaciones = _get_actuaciones(cur, radicado)
                crm = _get_crm_agreements(
                    cur,
                    process.get("inmueble_id"),
                    [x.get("identificacion") for x in demandados] + [x.get("identificacion") for x in demandantes],
                )
                contacts = _contact_options(cur)
                audit = []
                if _table_exists(cur, "expediente_ediciones"):
                    cur.execute(
                        "SELECT id,fecha,usuario,accion FROM expediente_ediciones WHERE radicado_interno=%s ORDER BY fecha DESC LIMIT 20",
                        (radicado,),
                    )
                    audit = [dict(r) for r in cur.fetchall()]
            return templates.TemplateResponse(
                request=request,
                name="detalle_expediente_v2.html",
                context={
                    "request": request,
                    "proceso": process,
                    "demandantes": demandantes,
                    "demandados": demandados,
                    "contactos": contacts,
                    "actuaciones": actuaciones,
                    "acuerdos_crm": crm,
                    "audit_ediciones": audit,
                },
            )
    finally:
        _release(conn)


_remove_route("/expediente/{radicado}", {"GET"})
app.get("/expediente/{radicado}")(expediente_workflow_context)


@app.post("/expediente/guardar-estructurado")
async def guardar_expediente_estructurado(request: Request):
    form = await request.form()
    radicado = str(form.get("radicado_interno") or "").strip()
    if not radicado:
        return _redirect("/expedientes", error="Expediente+sin+radicado+interno")

    demandante_ids = [str(x).strip() for x in form.getlist("demandante_id") if str(x).strip()]
    demandado_ids = [str(x).strip() for x in form.getlist("demandado_id") if str(x).strip()]
    # Deduplicacion por identificacion, preservando orden.
    demandante_ids = list(dict.fromkeys(demandante_ids))
    demandado_ids = list(dict.fromkeys(demandado_ids))
    medidas = [str(x).strip() for x in form.getlist("medida") if str(x).strip()]

    conn = _conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                process = _get_process(cur, radicado)
                if not process:
                    raise HTTPException(status_code=404, detail="Expediente no encontrado")
                cols = _cols(cur, "procesos")
                _ensure_audit_table(cur)
                before = _snapshot(process, _get_demandantes(cur, process), _get_demandados(cur, radicado))

                radicado_rama = str(form.get("radicado_rama") or "").strip()
                if "radicado_rama" in cols and radicado_rama and radicado_rama != process.get("radicado_rama"):
                    cur.execute(
                        "SELECT 1 FROM procesos WHERE radicado_rama=%s AND radicado_interno<>%s LIMIT 1",
                        (radicado_rama, radicado),
                    )
                    if cur.fetchone():
                        raise ValueError("El radicado Rama ya pertenece a otro expediente")

                numeric_pretensiones = process.get("pretensiones")
                raw_pret = str(form.get("pretensiones") or "").replace(".", "").replace(",", ".").strip()
                if raw_pret:
                    numeric_pretensiones = float(raw_pret)

                editable = {
                    "radicado_rama": radicado_rama,
                    "naturaleza": str(form.get("naturaleza") or "").strip(),
                    "juzgado": str(form.get("juzgado") or "").strip(),
                    "estado": str(form.get("estado") or "Activo").strip() or "Activo",
                    "pretensiones": numeric_pretensiones,
                }
                if "medidas_cautelares" in cols:
                    editable["medidas_cautelares"] = "\n".join(medidas)
                if "abogado_id" in cols:
                    abogado_text = str(form.get("abogado_id") or "").strip()
                    editable["abogado_id"] = int(abogado_text) if abogado_text.isdigit() else None
                usable = [k for k in editable if k in cols and (k != "radicado_rama" or editable[k])]
                if usable:
                    cur.execute(
                        f"UPDATE procesos SET {', '.join(f'{k}=%s' for k in usable)} WHERE radicado_interno=%s",
                        [editable[k] for k in usable] + [radicado],
                    )

                if "id_cliente" in cols:
                    if not demandante_ids:
                        raise ValueError("Debe quedar al menos un demandante vinculado")
                    placeholders = ",".join(["%s"] * len(demandante_ids))
                    cur.execute(
                        f"SELECT identificacion FROM contactos WHERE identificacion IN ({placeholders})",
                        demandante_ids,
                    )
                    valid_dem = {r[0] for r in cur.fetchall()}
                    if set(demandante_ids) != valid_dem:
                        raise ValueError("Uno de los demandantes seleccionados no existe en Contactos")
                    cur.execute("UPDATE procesos SET id_cliente=%s WHERE radicado_interno=%s", (" | ".join(demandante_ids), radicado))

                if _table_exists(cur, "procesos_litisconsorcio"):
                    if not demandado_ids:
                        raise ValueError("Debe quedar al menos un demandado vinculado")
                    placeholders = ",".join(["%s"] * len(demandado_ids))
                    cur.execute(
                        f"SELECT identificacion FROM contactos WHERE identificacion IN ({placeholders})",
                        demandado_ids,
                    )
                    valid_ddo = {r[0] for r in cur.fetchall()}
                    if set(demandado_ids) != valid_ddo:
                        raise ValueError("Uno de los demandados seleccionados no existe en Contactos")
                    lcols = _cols(cur, "procesos_litisconsorcio")
                    if "radicado_interno" not in lcols or "identificacion_demandado" not in lcols:
                        raise RuntimeError("La tabla de partes no tiene las columnas esperadas")
                    cur.execute("DELETE FROM procesos_litisconsorcio WHERE radicado_interno=%s", (radicado,))
                    for ident in demandado_ids:
                        cur.execute(
                            "INSERT INTO procesos_litisconsorcio (radicado_interno,identificacion_demandado) VALUES (%s,%s)",
                            (radicado, ident),
                        )
                    if "demandado" in cols and "id_demandado" in cols:
                        cur.execute(
                            "SELECT nombre FROM contactos WHERE identificacion IN (" + placeholders + ") ORDER BY nombre",
                            demandado_ids,
                        )
                        names = [r[0] for r in cur.fetchall()]
                        cur.execute(
                            "UPDATE procesos SET demandado=%s,id_demandado=%s WHERE radicado_interno=%s",
                            (" | ".join(names), " | ".join(demandado_ids), radicado),
                        )

                after_process = _get_process(cur, radicado)
                after = _snapshot(after_process, _get_demandantes(cur, after_process), _get_demandados(cur, radicado))
                cur.execute(
                    "INSERT INTO expediente_ediciones (radicado_interno,usuario,accion,antes,despues) VALUES (%s,%s,%s,%s::jsonb,%s::jsonb)",
                    (radicado, request.cookies.get("token_erp") or "ERP", "EDICION_MAESTRA", json.dumps(before, default=str), json.dumps(after, default=str)),
                )
                _sync_stage(cur, radicado)
        return _redirect(f"/expediente/{radicado}", mensaje="Expediente+actualizado")
    except Exception as exc:
        print(f"[EXPEDIENTE][EDICION] {radicado}: {exc!r}", flush=True)
        return _redirect(f"/expediente/{radicado}", error=str(exc).replace(" ", "+"))
    finally:
        _release(conn)


_remove_route("/actuacion/nueva", {"POST"})


@app.post("/actuacion/nueva")
async def nueva_actuacion_segura(request: Request):
    form = await request.form()
    radicado = str(form.get("radicado_interno") or "").strip()
    if not radicado:
        return _redirect("/expedientes", error="Radicado+interno+requerido")
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if not _table_exists(cur, "actuaciones"):
                    raise RuntimeError("No existe la tabla actuaciones")
                cols = _cols(cur, "actuaciones")
                payload = {
                    "radicado_interno": radicado,
                    "fecha": form.get("fecha") or date.today(),
                    "etapa": str(form.get("etapa") or "Auto de Trámite / General").strip(),
                    "descripcion": str(form.get("descripcion") or form.get("sub_etapa") or "Actuación registrada").strip(),
                    "usuario": "ERP",
                    "tipificacion_sugerida": str(form.get("sub_etapa") or "Observación").strip(),
                }
                use = [c for c in payload if c in cols]
                cur.execute(
                    f"INSERT INTO actuaciones ({', '.join(use)}) VALUES ({', '.join(['%s']*len(use))}) RETURNING id",
                    [payload[c] for c in use],
                )
                _sync_stage(cur, radicado)
        return _redirect(f"/expediente/{radicado}", mensaje="Actuación+registrada+y+etapa+actualizada")
    except Exception as exc:
        print(f"[ACTUACION] Error registrando {radicado}: {exc!r}", flush=True)
        return _redirect(f"/expediente/{radicado}", error="No+fue+posible+registrar+la+actuación")
    finally:
        _release(conn)


_remove_route("/actuacion/eliminar", {"POST"})


@app.post("/actuacion/eliminar")
def eliminar_actuacion_segura(actuacion_id: int, radicado_interno: str):
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM actuaciones WHERE id=%s AND radicado_interno=%s", (actuacion_id, radicado_interno))
                if cur.rowcount == 0:
                    raise ValueError("Actuación no encontrada")
                _sync_stage(cur, radicado_interno)
        return _redirect(f"/expediente/{radicado_interno}", mensaje="Actuación+eliminada+y+etapa+recalculada")
    except Exception as exc:
        print(f"[ACTUACION] Error eliminando {actuacion_id}: {exc!r}", flush=True)
        return _redirect(f"/expediente/{radicado_interno}", error="No+fue+posible+eliminar+la+actuación")
    finally:
        _release(conn)


_remove_route("/descargar-excel", {"GET"})


@app.get("/descargar-excel")
def descargar_excel_con_etapas():
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                _sync_all_stages(cur)
            output = main.generar_informe_ejecutivo_excel(conn)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=reporte_ejecutivo_expedientes.xlsx"},
        )
    finally:
        _release(conn)


print("[EXPEDIENTE_WORKFLOW] Edicion estructurada + auditoria + etapas automaticas + CRM activados", flush=True)
