"""Rutas que faltaban en los templates: informes, Excel, CRM y vencimientos."""
import io
from datetime import date

import pandas as pd
import main
from fastapi import Request, Form
from fastapi.responses import RedirectResponse, StreamingResponse
from psycopg2.extras import RealDictCursor


def conn():
    return main.db_pool.getconn()


def release(c):
    main.db_pool.putconn(c)


def table_exists(cur, table):
    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s)", (table,))
    return bool(cur.fetchone()[0])


c = conn()
try:
    with c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gestiones_crm (
                    id BIGSERIAL PRIMARY KEY,
                    inmueble_id INTEGER,
                    identificacion_deudor TEXT,
                    tipo_contacto TEXT,
                    resumen TEXT NOT NULL,
                    promesa_pago_fecha DATE,
                    fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    usuario TEXT NOT NULL DEFAULT 'ERP',
                    anulado BOOLEAN NOT NULL DEFAULT FALSE,
                    estado TEXT NOT NULL DEFAULT 'ACTIVO'
                )
            """)
            cur.execute("ALTER TABLE gestiones_crm ALTER COLUMN inmueble_id DROP NOT NULL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vencimientos (
                    id BIGSERIAL PRIMARY KEY,
                    radicado_interno TEXT NOT NULL,
                    titulo TEXT NOT NULL,
                    fecha_vencimiento DATE NOT NULL,
                    observaciones TEXT,
                    completado BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vencimientos_fecha ON vencimientos (fecha_vencimiento, completado)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gestiones_crm_inmueble ON gestiones_crm (inmueble_id, fecha DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gestiones_crm_identificacion ON gestiones_crm (identificacion_deudor, fecha DESC)")
finally:
    release(c)

# Informes.
main.app.router.routes[:] = [
    r for r in main.app.router.routes
    if not (getattr(r, "path", None) == "/informes" and "GET" in getattr(r, "methods", set()))
]


@main.app.get("/informes")
def informes_corregidos(request: Request):
    c = conn()
    try:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM procesos")
            total = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM contactos")
            contactos = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM inmuebles_ph")
            inmuebles = int(cur.fetchone()["n"])
        return main.templates.TemplateResponse(
            request=request, name="informes.html",
            context={"request": request, "total_procesos": total, "total_contactos": contactos, "total_inmuebles": inmuebles},
        )
    finally:
        release(c)


@main.app.get("/descargar-excel")
def descargar_excel():
    c = conn()
    try:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM procesos ORDER BY radicado_interno DESC")
            procesos = [dict(x) for x in cur.fetchall()]
            cur.execute("SELECT * FROM contactos ORDER BY nombre ASC")
            contactos = [dict(x) for x in cur.fetchall()]
            cur.execute("SELECT * FROM inmuebles_ph ORDER BY id")
            inmuebles = [dict(x) for x in cur.fetchall()]
            if table_exists(cur, "gestiones_crm"):
                cur.execute("SELECT * FROM gestiones_crm ORDER BY fecha DESC")
                gestiones = [dict(x) for x in cur.fetchall()]
            else:
                gestiones = []
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            pd.DataFrame(procesos).to_excel(writer, index=False, sheet_name="Procesos")
            pd.DataFrame(contactos).to_excel(writer, index=False, sheet_name="Contactos")
            pd.DataFrame(inmuebles).to_excel(writer, index=False, sheet_name="Inmuebles")
            pd.DataFrame(gestiones).to_excel(writer, index=False, sheet_name="CRM")
        output.seek(0)
        return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=reporte_ejecutivo_erp.xlsx"})
    finally:
        release(c)


# CRM: reemplaza la ruta de compatibilidad para leer también la tabla histórica
# gestiones_cartera, que es donde el agente ya está escribiendo actualmente.
main.app.router.routes[:] = [
    r for r in main.app.router.routes
    if not (getattr(r, "path", None) == "/crm" and "GET" in getattr(r, "methods", set()))
]


@main.app.get("/crm")
def crm_unificado(request: Request, buscar_inmueble: int | None = None):
    c = conn()
    try:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            inmuebles = main.cargar_inmuebles_ph()
            conjuntos = sorted({x.get("conjunto_residencial") for x in inmuebles if x.get("conjunto_residencial")})
            filtro = {}
            for item in inmuebles:
                filtro.setdefault(item.get("conjunto_residencial") or "SIN CONJUNTO", []).append({
                    "id": item.get("id"),
                    "nombre": f"{item.get('torre_apto') or ''} - {item.get('nombre') or ''} ({item.get('cedula') or ''})".strip(" -"),
                })
            inmueble_actual = next((x for x in inmuebles if buscar_inmueble and int(x.get("id")) == int(buscar_inmueble)), None)
            propietarios, historial = [], []
            if inmueble_actual:
                cedula = inmueble_actual.get("cedula")
                cur.execute("SELECT identificacion,nombre,telefono,email FROM contactos WHERE identificacion=%s", (cedula,))
                propietarios = [dict(r) for r in cur.fetchall()]
                if not propietarios and cedula:
                    propietarios = [{"identificacion": cedula, "nombre": inmueble_actual.get("nombre")}]
                if table_exists(cur, "gestiones_crm"):
                    cur.execute("SELECT id,tipo_contacto AS tipo,identificacion_deudor AS deudor_nombre,resumen,promesa_pago_fecha AS promesa,usuario,fecha FROM gestiones_crm WHERE inmueble_id=%s AND COALESCE(anulado,FALSE)=FALSE ORDER BY fecha DESC LIMIT 200", (buscar_inmueble,))
                    historial = [dict(r) for r in cur.fetchall()]
                if table_exists(cur, "gestiones_cartera") and cedula:
                    cur.execute("SELECT * FROM gestiones_cartera WHERE REGEXP_REPLACE(COALESCE(identificacion_deudor::text,''),'[^0-9]','','g')=%s ORDER BY fecha DESC LIMIT 200", (str(cedula).replace(".", ""),))
                    for r in cur.fetchall():
                        d = dict(r)
                        historial.append({"id": d.get("id"), "tipo": d.get("tipo_contacto") or "WhatsApp IA", "deudor_nombre": d.get("identificacion_deudor") or cedula, "resumen": d.get("resumen", ""), "promesa": d.get("promesa_pago_fecha"), "usuario": d.get("usuario", "Bot Claude"), "fecha": d.get("fecha")})
                    historial.sort(key=lambda x: str(x.get("fecha") or ""), reverse=True)
            return main.templates.TemplateResponse(request=request, name="crm.html", context={
                "request": request, "conjuntos": conjuntos, "json_filtro": __import__("json").dumps(filtro, ensure_ascii=False),
                "conjunto_actual": "TODOS", "inmueble_actual": inmueble_actual, "propietarios": propietarios, "historial": historial,
            })
    finally:
        release(c)


@main.app.post("/vencimientos/guardar")
def guardar_vencimiento(radicado_interno: str = Form(...), titulo: str = Form(...), fecha_vencimiento: date = Form(...), observaciones: str = Form("")):
    c = conn()
    try:
        with c:
            with c.cursor() as cur:
                cur.execute("INSERT INTO vencimientos (radicado_interno,titulo,fecha_vencimiento,observaciones) VALUES (%s,%s,%s,%s)", (radicado_interno, titulo.strip(), fecha_vencimiento, observaciones.strip()))
        return RedirectResponse("/vencimientos", status_code=303)
    finally:
        release(c)


@main.app.post("/vencimientos/completar")
def completar_vencimiento(vencimiento_id: int = Form(...)):
    c = conn()
    try:
        with c:
            with c.cursor() as cur:
                cur.execute("UPDATE vencimientos SET completado=TRUE WHERE id=%s", (vencimiento_id,))
        return RedirectResponse("/vencimientos", status_code=303)
    finally:
        release(c)

print("[FEATURE_ROUTES] Informes, Excel ejecutivo, CRM unificado y vencimientos conectados", flush=True)
