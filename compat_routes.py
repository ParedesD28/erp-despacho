"""Rutas de compatibilidad del ERP.

Este módulo conecta los templates existentes con FastAPI sin duplicar la lógica
financiera. Se importa después de main.py desde start.py.
"""
import json
from datetime import date
from fastapi import Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from psycopg2.extras import RealDictCursor

import main

app = main.app
templates = main.templates


def _conn():
    return main.db_pool.getconn() if getattr(main, "db_pool", None) else None


def _release(conn):
    if conn is not None and getattr(main, "db_pool", None):
        main.db_pool.putconn(conn)


def _table_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return {r["column_name"] for r in cur.fetchall()}


def _redirect(path, **params):
    from urllib.parse import urlencode
    query = urlencode(params)
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=303)


@app.get("/", include_in_schema=False)
def root_compat():
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/contactos")
def contactos(request: Request, buscar_cedula: str | None = None):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cols = _table_columns(cur, "contactos")
            wanted = ["id", "identificacion", "nombre", "tipo", "telefono", "email", "direccion", "ciudad"]
            select_cols = [c for c in wanted if c in cols]
            if not select_cols:
                raise RuntimeError("La tabla contactos no tiene columnas consultables")
            sql = f"SELECT {', '.join(select_cols)} FROM contactos"
            args = []
            if buscar_cedula:
                term = buscar_cedula.split(" - ", 1)[0].strip()
                sql += " WHERE identificacion ILIKE %s OR nombre ILIKE %s"
                args = [f"%{term}%", f"%{buscar_cedula}%"]
            sql += " ORDER BY nombre ASC LIMIT 500"
            cur.execute(sql, args)
            rows = cur.fetchall()
            contacto_actual = rows[0] if buscar_cedula and rows else None
            return templates.TemplateResponse(
                request=request,
                name="contactos.html",
                context={"request": request, "lista_contactos": rows, "contacto_actual": contacto_actual},
            )
    finally:
        _release(conn)


@app.post("/contactos/guardar")
def guardar_contacto(
    request: Request,
    identificacion: str = Form(...),
    nombre: str = Form(...),
    tipo: str = Form(...),
    telefono: str = Form(""),
    email: str = Form(""),
    direccion: str = Form(""),
    ciudad: str = Form("PEREIRA"),
    id_contacto: str | None = Form(None),
):
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cols = _table_columns(cur, "contactos")
                values = {
                    "identificacion": identificacion.strip(), "nombre": nombre.strip(), "tipo": tipo,
                    "telefono": telefono.strip(), "email": email.strip(), "direccion": direccion.strip(),
                    "ciudad": ciudad.strip() or "PEREIRA",
                }
                usable = [c for c in values if c in cols]
                if id_contacto and "id" in cols:
                    sets = ", ".join(f"{c}=%s" for c in usable)
                    cur.execute(f"UPDATE contactos SET {sets} WHERE id=%s", [values[c] for c in usable] + [id_contacto])
                else:
                    names = ", ".join(usable)
                    marks = ", ".join(["%s"] * len(usable))
                    if "identificacion" in cols:
                        cur.execute(
                            f"INSERT INTO contactos ({names}) VALUES ({marks}) "
                            f"ON CONFLICT (identificacion) DO UPDATE SET nombre=EXCLUDED.nombre, tipo=EXCLUDED.tipo",
                            [values[c] for c in usable],
                        )
                    else:
                        cur.execute(f"INSERT INTO contactos ({names}) VALUES ({marks})", [values[c] for c in usable])
        return _redirect("/contactos", mensaje="Contacto+guardado")
    except Exception as exc:
        print(f"[COMPAT] Error guardando contacto: {exc}", flush=True)
        return _redirect("/contactos", error="No+fue+posible+guardar+el+contacto")
    finally:
        _release(conn)


@app.get("/crm")
def crm(request: Request, buscar_inmueble: int | None = None):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            inmuebles = main.cargar_inmuebles_ph()
            conjuntos = sorted({x.get("conjunto_residencial") for x in inmuebles if x.get("conjunto_residencial")})
            filtro = {}
            for item in inmuebles:
                filtro.setdefault(item.get("conjunto_residencial") or "SIN CONJUNTO", []).append({
                    "id": item.get("id"),
                    "nombre": f"{item.get('torre_apto') or ''} - {item.get('nombre') or ''} ({item.get('cedula') or ''})".strip(" -"),
                })
            inmueble_actual = None
            propietarios = []
            historial = []
            if buscar_inmueble:
                inmueble_actual = next((x for x in inmuebles if int(x.get("id")) == int(buscar_inmueble)), None)
                if inmueble_actual:
                    cur.execute(
                        "SELECT identificacion,nombre,telefono,email FROM contactos WHERE identificacion=%s",
                        (inmueble_actual.get("cedula"),),
                    )
                    propietarios = [dict(r) for r in cur.fetchall()]
                    if not propietarios and inmueble_actual.get("cedula"):
                        propietarios = [{"identificacion": inmueble_actual.get("cedula"), "nombre": inmueble_actual.get("nombre")}]
                    cols = _table_columns(cur, "gestiones_crm") if _table_exists(cur, "gestiones_crm") else set()
                    if cols:
                        cur.execute("SELECT * FROM gestiones_crm WHERE inmueble_id=%s ORDER BY fecha DESC LIMIT 200", (buscar_inmueble,))
                        historial = [dict(r) for r in cur.fetchall()]
            return templates.TemplateResponse(
                request=request,
                name="crm.html",
                context={
                    "request": request,
                    "conjuntos": conjuntos,
                    "json_filtro": json.dumps(filtro, ensure_ascii=False),
                    "conjunto_actual": "TODOS",
                    "inmueble_actual": inmueble_actual,
                    "propietarios": propietarios,
                    "historial": historial,
                },
            )
    finally:
        _release(conn)


def _table_exists(cur, table):
    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s)", (table,))
    row = cur.fetchone()
    return bool(row["exists"])


@app.post("/crm/guardar")
def crm_guardar(
    inmueble_id: int = Form(...), identificacion_deudor: str = Form(...),
    tipo_contacto: str = Form(...), resumen: str = Form(...), promesa_pago_fecha: str | None = Form(None),
):
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if not _table_exists(cur, "gestiones_crm"):
                    raise RuntimeError("No existe gestiones_crm; debe crearse la tabla de gestión")
                cols = _table_columns(cur, "gestiones_crm")
                vals = {
                    "inmueble_id": inmueble_id, "identificacion_deudor": identificacion_deudor,
                    "tipo": tipo_contacto, "tipo_contacto": tipo_contacto, "resumen": resumen,
                    "promesa_pago_fecha": promesa_pago_fecha or None, "fecha": date.today(), "usuario": "ERP",
                }
                usable = [c for c in vals if c in cols]
                cur.execute(f"INSERT INTO gestiones_crm ({', '.join(usable)}) VALUES ({', '.join(['%s']*len(usable))})", [vals[c] for c in usable])
        return _redirect("/crm", buscar_inmueble=inmueble_id, mensaje="Gestión+guardada")
    except Exception as exc:
        print(f"[COMPAT] Error CRM: {exc}", flush=True)
        return _redirect("/crm", buscar_inmueble=inmueble_id, error="No+fue+posible+guardar+la+gestión")
    finally:
        _release(conn)


@app.post("/crm/anular")
def crm_anular(gestion_id: int = Form(...), inmueble_id: int = Form(...)):
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if not _table_exists(cur, "gestiones_crm"):
                    raise RuntimeError("No existe gestiones_crm")
                cols = _table_columns(cur, "gestiones_crm")
                if "anulado" in cols:
                    cur.execute("UPDATE gestiones_crm SET anulado=TRUE WHERE id=%s", (gestion_id,))
                elif "estado" in cols:
                    cur.execute("UPDATE gestiones_crm SET estado='ANULADO' WHERE id=%s", (gestion_id,))
                else:
                    raise RuntimeError("gestiones_crm no tiene campo de anulación")
        return _redirect("/crm", buscar_inmueble=inmueble_id, mensaje="Gestión+anulada")
    except Exception as exc:
        print(f"[COMPAT] Error anulando CRM: {exc}", flush=True)
        return _redirect("/crm", buscar_inmueble=inmueble_id, error="No+fue+posible+anular")
    finally:
        _release(conn)


@app.get("/procesos")
def procesos(request: Request):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT identificacion,nombre FROM contactos WHERE tipo='Cliente' ORDER BY nombre LIMIT 500")
            clientes = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT identificacion,nombre FROM contactos WHERE tipo='Contraparte' ORDER BY nombre LIMIT 500")
            contrapartes = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT id,nombre FROM abogados ORDER BY nombre")
            abogados = [dict(r) for r in cur.fetchall()]
        return templates.TemplateResponse(request=request, name="procesos.html", context={
            "request": request, "contactos_clientes": clientes,
            "contactos_contrapartes": contrapartes, "abogados": abogados,
        })
    finally:
        _release(conn)


@app.post("/crear_expediente_completo")
async def crear_expediente_completo(request: Request):
    """Adapta el formulario nuevo al modelo real de procesos/inmuebles/contactos."""
    form = await request.form()
    radicado_rama = str(form.get("radicado_rama", "")).strip()
    naturaleza = str(form.get("naturaleza", "")).strip()
    juzgado = f"{form.get('juzgado_numero','')} {form.get('juzgado_tipo','')} - {form.get('juzgado_ciudad','')}".strip()
    apto = str(form.get("apto", "")).strip()
    pretensiones = str(form.get("pretensiones", "0")).strip() or "0"
    abogado_id = str(form.get("abogado_id", "")).strip() or None
    medidas = str(form.get("medidas_cautelares", "")).strip()

    def split_values(name, new_id, new_name):
        vals = [x.strip() for x in form.getlist(name) if str(x).strip()]
        ids = [x.strip() for x in form.getlist(new_id) if str(x).strip()]
        names = [x.strip() for x in form.getlist(new_name) if str(x).strip()]
        return vals, list(zip(ids, names))

    demandantes, nuevos_dem = split_values("demandantes_existentes", "nuevo_dem_id", "nuevo_dem_nombre")
    demandados, nuevos_ddo = split_values("demandados_existentes", "nuevo_ddo_id", "nuevo_ddo_nombre")
    demandantes += [x[0] for x in nuevos_dem]
    demandados += [x[0] for x in nuevos_ddo]
    if not radicado_rama or not demandantes or not demandados:
        return _redirect("/procesos", error="Debe+seleccionar+al+menos+un+demandante+y+un+demandado")

    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                # Crear/actualizar contactos de las filas nuevas.
                for ident, nombre in nuevos_dem:
                    cur.execute("INSERT INTO contactos (identificacion,nombre,tipo,ciudad) VALUES (%s,%s,'Cliente','PEREIRA') ON CONFLICT (identificacion) DO UPDATE SET nombre=EXCLUDED.nombre", (ident, nombre))
                for ident, nombre in nuevos_ddo:
                    cur.execute("INSERT INTO contactos (identificacion,nombre,tipo,ciudad) VALUES (%s,%s,'Contraparte','PEREIRA') ON CONFLICT (identificacion) DO UPDATE SET nombre=EXCLUDED.nombre", (ident, nombre))
                cur.execute("SELECT 1 FROM procesos WHERE radicado_rama=%s LIMIT 1", (radicado_rama,))
                if cur.fetchone():
                    return _redirect("/procesos", error="El+radicado+Rama+Judicial+ya+existe")
                cliente = demandantes[0]
                cur.execute("SELECT conjunto_residencial FROM inmuebles_ph i JOIN contactos c ON c.id=i.contacto_id WHERE c.identificacion=%s ORDER BY i.id DESC LIMIT 1", (cliente,))
                row = cur.fetchone()
                conjunto = row[0] if row else "SIN CONJUNTO"
                cur.execute("SELECT id FROM contactos WHERE identificacion=%s", (demandados[0],))
                ddo = cur.fetchone()
                if not ddo:
                    raise RuntimeError("Demandado no existe")
                cur.execute("INSERT INTO inmuebles_ph (contacto_id,conjunto_residencial,torre_apto) VALUES (%s,%s,%s) RETURNING id", (ddo[0], conjunto, apto))
                inmueble_id = cur.fetchone()[0]
                cols = _table_columns(cur, "procesos")
                data = {
                    "radicado_interno": radicado_rama, "radicado_rama": radicado_rama,
                    "naturaleza": naturaleza, "juzgado": juzgado, "estado": "Activo",
                    "id_demandado": " | ".join(demandados), "demandado": " | ".join(demandados),
                    "inmueble_id": inmueble_id, "id_cliente": cliente, "pretensiones": pretensiones,
                    "medidas_cautelares": medidas, "abogado_id": abogado_id,
                }
                usable = [c for c in data if c in cols and data[c] is not None]
                cur.execute(f"INSERT INTO procesos ({', '.join(usable)}) VALUES ({', '.join(['%s']*len(usable))})", [data[c] for c in usable])
                if _table_exists(cur, "procesos_litisconsorcio"):
                    lit_cols = _table_columns(cur, "procesos_litisconsorcio")
                    for ident in demandados:
                        payload = {"radicado_interno": radicado_rama, "identificacion_demandado": ident}
                        use = [c for c in payload if c in lit_cols]
                        cur.execute(f"INSERT INTO procesos_litisconsorcio ({', '.join(use)}) VALUES ({', '.join(['%s']*len(use))})", [payload[c] for c in use])
                if _table_exists(cur, "actuaciones"):
                    act_cols = _table_columns(cur, "actuaciones")
                    payload = {"radicado_interno": radicado_rama, "fecha": date.today(), "etapa": "Inicio", "descripcion": "Presentación inicial de la demanda", "usuario": "Sistema", "tipificacion_sugerida": "Radicación"}
                    use = [c for c in payload if c in act_cols]
                    cur.execute(f"INSERT INTO actuaciones ({', '.join(use)}) VALUES ({', '.join(['%s']*len(use))})", [payload[c] for c in use])
        return _redirect("/expedientes", mensaje="Proceso+creado+exitosamente")
    except Exception as exc:
        print(f"[COMPAT] Error creando expediente: {exc}", flush=True)
        return _redirect("/procesos", error="No+fue+posible+crear+el+expediente")
    finally:
        _release(conn)


@app.get("/expediente/{radicado}")
def detalle_expediente(request: Request, radicado: str):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT p.*, c.nombre AS demandante_db, a.nombre AS abogado_asignado,
                         COALESCE(p.demandado, STRING_AGG(DISTINCT c2.nombre, ' | ')) AS demandado,
                         COALESCE(p.id_demandado, STRING_AGG(DISTINCT pl.identificacion_demandado, ' | ')) AS id_demandado
                         FROM procesos p
                         LEFT JOIN contactos c ON c.identificacion=p.id_cliente
                         LEFT JOIN abogados a ON a.id=p.abogado_id
                         LEFT JOIN procesos_litisconsorcio pl ON pl.radicado_interno=p.radicado_interno
                         LEFT JOIN contactos c2 ON c2.identificacion=pl.identificacion_demandado
                         WHERE p.radicado_interno=%s
                         GROUP BY p.radicado_interno,c.nombre,a.nombre""", (radicado,))
            proceso = cur.fetchone()
            if not proceso:
                raise HTTPException(status_code=404, detail="Expediente no encontrado")
            cur.execute("SELECT * FROM actuaciones WHERE radicado_interno=%s ORDER BY fecha DESC, id DESC", (radicado,))
            actuaciones = [dict(r) for r in cur.fetchall()]
        return templates.TemplateResponse(request=request, name="detalle_expediente.html", context={"request": request, "proceso": dict(proceso), "actuaciones": actuaciones})
    finally:
        _release(conn)


@app.post("/actuacion/nueva")
async def nueva_actuacion(request: Request):
    form = await request.form()
    radicado = str(form.get("radicado_interno", "")).strip()
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cols = _table_columns(cur, "actuaciones")
                payload = {
                    "radicado_interno": radicado, "fecha": form.get("fecha") or date.today(),
                    "etapa": form.get("etapa") or "Auto de Trámite / General",
                    "descripcion": form.get("descripcion") or form.get("sub_etapa") or "Actuación registrada",
                    "usuario": "ERP", "tipificacion_sugerida": form.get("sub_etapa") or "Observación",
                }
                use = [c for c in payload if c in cols]
                cur.execute(f"INSERT INTO actuaciones ({', '.join(use)}) VALUES ({', '.join(['%s']*len(use))})", [payload[c] for c in use])
        return _redirect(f"/expediente/{radicado}", mensaje="Actuación+registrada")
    finally:
        _release(conn)


@app.post("/actuacion/eliminar")
def eliminar_actuacion(actuacion_id: int = Form(...), radicado_interno: str = Form(...)):
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM actuaciones WHERE id=%s", (actuacion_id,))
        return _redirect(f"/expediente/{radicado_interno}", mensaje="Actuación+eliminada")
    finally:
        _release(conn)


@app.get("/vencimientos")
def vencimientos(request: Request):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            vencs = []
            if _table_exists(cur, "vencimientos"):
                cur.execute("SELECT * FROM vencimientos ORDER BY fecha_vencimiento ASC LIMIT 500")
                vencs = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT radicado_interno FROM procesos ORDER BY radicado_interno DESC LIMIT 500")
            radicados = [r["radicado_interno"] for r in cur.fetchall()]
        return templates.TemplateResponse(request=request, name="vencimientos.html", context={"request": request, "vencimientos": vencs, "radicados": radicados})
    finally:
        _release(conn)


@app.get("/informes")
def informes(request: Request):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM procesos ORDER BY radicado_interno DESC LIMIT 2000")
            procesos = [dict(r) for r in cur.fetchall()]
        return templates.TemplateResponse(request=request, name="informes.html", context={"request": request, "procesos": procesos})
    finally:
        _release(conn)


print("[COMPAT_ROUTES] Rutas ERP conectadas: /, /contactos, /crm, /procesos, /expediente/{radicado}, /vencimientos, /informes", flush=True)
