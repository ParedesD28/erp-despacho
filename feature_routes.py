"""Rutas que faltaban en los templates: informes, Excel y vencimientos."""
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
                    inmueble_id INTEGER NOT NULL,
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
finally:
    release(c)


# Reemplaza el contexto incompleto de /informes.
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
            request=request,
            name="informes.html",
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
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            pd.DataFrame(procesos).to_excel(writer, index=False, sheet_name="Procesos")
            pd.DataFrame(contactos).to_excel(writer, index=False, sheet_name="Contactos")
            pd.DataFrame(inmuebles).to_excel(writer, index=False, sheet_name="Inmuebles")
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=reporte_ejecutivo_erp.xlsx"},
        )
    finally:
        release(c)


@main.app.post("/vencimientos/guardar")
def guardar_vencimiento(
    radicado_interno: str = Form(...), titulo: str = Form(...),
    fecha_vencimiento: date = Form(...), observaciones: str = Form("")
):
    c = conn()
    try:
        with c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO vencimientos (radicado_interno,titulo,fecha_vencimiento,observaciones) VALUES (%s,%s,%s,%s)",
                    (radicado_interno, titulo.strip(), fecha_vencimiento, observaciones.strip()),
                )
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

print("[FEATURE_ROUTES] Informes, Excel ejecutivo y vencimientos conectados", flush=True)
