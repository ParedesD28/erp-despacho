"""Startup y arranque productivo del ERP Gestión Judicial.

Incluye una capa de agenda centralizada que se coloca delante de las rutas
heredadas de main.py para no duplicar información ni romper el módulo existente.
"""
from __future__ import annotations

import io
import os
import re
import threading
import time
from calendar import monthrange
from datetime import date, timedelta

from dotenv import load_dotenv
import openpyxl
from openpyxl.styles import Font
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
import uvicorn

load_dotenv()

import db
import security
import liquidador
import tasas
import main
from observability import log_msg


def _sumar_meses(fecha: date, meses: int) -> date:
    base = fecha.month - 1 + meses
    anio = fecha.year + base // 12
    mes = base % 12 + 1
    dia = min(fecha.day, monthrange(anio, mes)[1])
    return date(anio, mes, dia)


def _ensure_agenda_schema() -> None:
    """Esquema adicional para cuotas, anulaciones, propietarios y auditoría."""
    main._ensure_crm_and_vencimientos_schema()
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE vencimientos ADD COLUMN IF NOT EXISTS anulado BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE vencimientos ADD COLUMN IF NOT EXISTS categoria TEXT NOT NULL DEFAULT 'TERMINO'")
                cur.execute("ALTER TABLE vencimientos ADD COLUMN IF NOT EXISTS abogado_id TEXT")
                cur.execute("ALTER TABLE acuerdos_pago ADD COLUMN IF NOT EXISTS abogado_id TEXT")
                cur.execute("ALTER TABLE acuerdos_pago ADD COLUMN IF NOT EXISTS frecuencia TEXT NOT NULL DEFAULT 'MENSUAL'")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS acuerdos_pago_cuotas (
                        id BIGSERIAL PRIMARY KEY,
                        acuerdo_id BIGINT NOT NULL REFERENCES acuerdos_pago(id) ON DELETE CASCADE,
                        numero_cuota INTEGER NOT NULL,
                        fecha_vencimiento DATE NOT NULL,
                        valor_cuota NUMERIC(14,2) NOT NULL DEFAULT 0,
                        estado TEXT NOT NULL DEFAULT 'PENDIENTE',
                        anulado BOOLEAN NOT NULL DEFAULT FALSE,
                        abogado_id TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(acuerdo_id, numero_cuota)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_acuerdos_cuotas_fecha ON acuerdos_pago_cuotas(fecha_vencimiento, estado, anulado)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agenda_auditoria (
                        id BIGSERIAL PRIMARY KEY,
                        fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        abogado_id TEXT,
                        abogado_nombre TEXT NOT NULL DEFAULT 'Sistema',
                        accion TEXT NOT NULL,
                        tipo TEXT NOT NULL,
                        registro_id BIGINT,
                        radicado_interno TEXT,
                        identificacion_deudor TEXT,
                        nombre_deudor TEXT,
                        inmueble_id INTEGER,
                        detalle TEXT
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_agenda_auditoria_fecha ON agenda_auditoria(fecha DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_agenda_auditoria_abogado ON agenda_auditoria(abogado_id, fecha DESC)")

                cur.execute("""
                    INSERT INTO acuerdos_pago_cuotas (acuerdo_id, numero_cuota, fecha_vencimiento, valor_cuota, abogado_id)
                    SELECT a.id, 1, a.fecha_compromiso, a.valor_acordado, a.abogado_id
                    FROM acuerdos_pago a
                    WHERE NOT EXISTS (
                        SELECT 1 FROM acuerdos_pago_cuotas c WHERE c.acuerdo_id = a.id
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS inmueble_propietarios (
                        id BIGSERIAL PRIMARY KEY,
                        inmueble_id INTEGER NOT NULL,
                        contacto_id INTEGER NOT NULL,
                        es_principal BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(inmueble_id, contacto_id)
                    )
                """)
                cur.execute("""
                    INSERT INTO inmueble_propietarios (inmueble_id, contacto_id, es_principal)
                    SELECT i.id, i.contacto_id, TRUE
                    FROM inmuebles_ph i
                    WHERE i.contacto_id IS NOT NULL
                    ON CONFLICT (inmueble_id, contacto_id) DO UPDATE
                    SET es_principal = inmueble_propietarios.es_principal OR EXCLUDED.es_principal
                """)
                cur.execute("""
                    INSERT INTO inmueble_propietarios (inmueble_id, contacto_id, es_principal)
                    SELECT DISTINCT p.inmueble_id, c.id, FALSE
                    FROM procesos p
                    JOIN procesos_litisconsorcio pl ON pl.radicado_interno = p.radicado_interno
                    JOIN contactos c ON c.identificacion = pl.identificacion_demandado
                    WHERE p.inmueble_id IS NOT NULL
                    ON CONFLICT (inmueble_id, contacto_id) DO NOTHING
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_inmueble_propietarios_inmueble ON inmueble_propietarios(inmueble_id)")
    finally:
        conn.release()


def _audit(cur, request: Request, accion: str, tipo: str, registro_id=None,
           radicado_interno=None, identificacion=None, nombre=None,
           inmueble_id=None, detalle="") -> str:
    user_id = str(getattr(request.state, "user_id", "") or "").strip() or None
    abogado = "Sistema"
    if user_id:
        cur.execute("SELECT nombre FROM abogados WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if row:
            abogado = row["nombre"] if isinstance(row, dict) else row[0]
    cur.execute("""
        INSERT INTO agenda_auditoria
            (abogado_id, abogado_nombre, accion, tipo, registro_id, radicado_interno,
             identificacion_deudor, nombre_deudor, inmueble_id, detalle)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (user_id, abogado, accion, tipo, registro_id, radicado_interno,
          identificacion, nombre, inmueble_id, detalle))
    return abogado


def _crear_router_agenda() -> APIRouter:
    router = APIRouter()

    @router.get("/vencimientos", name="agenda_vencimientos")
    def agenda_vencimientos(request: Request):
        _ensure_agenda_schema()
        conn = db.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT radicado_interno FROM procesos ORDER BY radicado_interno DESC LIMIT 500")
                radicados = [r["radicado_interno"] for r in cur.fetchall()]

                cur.execute("""
                    SELECT * FROM vencimientos
                    WHERE completado=FALSE
                      AND COALESCE(anulado,FALSE)=FALSE
                      AND COALESCE(tipo,'PROCESAL') <> 'ACUERDO_PAGO'
                    ORDER BY fecha_vencimiento ASC, id ASC
                """)
                vencimientos = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT c.*, a.identificacion_deudor, a.nombre_deudor, a.telefono,
                           a.observaciones, a.inmueble_id, a.numero_cuotas,
                           a.estado AS acuerdo_estado,
                           i.conjunto_residencial, i.torre_apto
                    FROM acuerdos_pago_cuotas c
                    JOIN acuerdos_pago a ON a.id=c.acuerdo_id
                    LEFT JOIN inmuebles_ph i ON i.id=a.inmueble_id
                    WHERE COALESCE(c.anulado,FALSE)=FALSE
                      AND UPPER(COALESCE(c.estado,'PENDIENTE')) <> 'ANULADO'
                      AND UPPER(COALESCE(a.estado,'PENDIENTE')) <> 'ANULADO'
                    ORDER BY c.fecha_vencimiento ASC, c.id ASC
                """)
                cuotas = [dict(r) for r in cur.fetchall()]

                cur.execute("SELECT identificacion,nombre,telefono FROM contactos ORDER BY nombre ASC LIMIT 2000")
                contactos = [dict(r) for r in cur.fetchall()]

            eventos = []
            for v in vencimientos:
                categoria = str(v.get("categoria") or "TERMINO").upper()
                eventos.append({
                    "id": f"v-{v['id']}",
                    "tipo": "OTRO" if categoria == "OTROS" else "TERMINO_JUDICIAL",
                    "titulo": v.get("titulo") or "Vencimiento",
                    "fecha": str(v.get("fecha_vencimiento")),
                    "radicado": v.get("radicado_interno") or "",
                    "observaciones": v.get("observaciones") or "",
                    "categoria": categoria,
                })

            for c in cuotas:
                n = int(c.get("numero_cuotas") or 1)
                nombre = c.get("nombre_deudor") or c.get("identificacion_deudor") or "Persona"
                eventos.append({
                    "id": f"c-{c['id']}",
                    "tipo": "ACUERDO_PAGO",
                    "titulo": f"Cuota {c.get('numero_cuota')}/{n} - {nombre}",
                    "fecha": str(c.get("fecha_vencimiento")),
                    "valor": float(c.get("valor_cuota") or 0),
                    "estado": c.get("estado") or "PENDIENTE",
                    "deudor": nombre,
                    "identificacion": c.get("identificacion_deudor") or "",
                    "telefono": c.get("telefono") or "",
                    "inmueble": f"{c.get('conjunto_residencial') or ''} {c.get('torre_apto') or ''}".strip(),
                    "observaciones": c.get("observaciones") or "",
                    "cuota_id": c.get("id"),
                    "acuerdo_id": c.get("acuerdo_id"),
                    "numero_cuota": c.get("numero_cuota"),
                    "numero_cuotas": n,
                })

            return main.render_template("vencimientos.html", {
                "request": request,
                "radicados": radicados,
                "vencimientos": vencimientos,
                "contactos": contactos,
                "json_eventos": __import__("json").dumps(eventos, ensure_ascii=False, default=str),
            })
        finally:
            conn.release()

    @router.post("/acuerdos/guardar", name="agenda_guardar_acuerdo")
    def agenda_guardar_acuerdo(
        request: Request,
        identificacion_deudor: str = Form(...),
        fecha_compromiso: str = Form(...),
        valor_acordado: float = Form(0.0),
        nombre_deudor: str = Form(""),
        telefono: str = Form(""),
        inmueble_id: int | None = Form(None),
        observaciones: str = Form(""),
        numero_cuotas: int = Form(1),
        frecuencia: str = Form("MENSUAL"),
        radicado_interno: str | None = Form(None),
    ):
        _ensure_agenda_schema()
        conn = db.get_connection()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    ident = str(identificacion_deudor or "").strip().split(" - ", 1)[0]
                    cur.execute("""
                        SELECT identificacion,nombre,telefono FROM contactos
                        WHERE identificacion=%s
                           OR REGEXP_REPLACE(COALESCE(identificacion::text,''),'[^0-9]','','g')=%s
                        LIMIT 1
                    """, (ident, re.sub(r"\D", "", ident)))
                    persona = cur.fetchone()
                    if not persona:
                        raise ValueError("Seleccione una persona existente del directorio de Contactos.")

                    ident = str(persona["identificacion"])
                    nombre_real = str(persona["nombre"] or nombre_deudor or ident)
                    telefono_real = str(persona["telefono"] or telefono or "")
                    if not inmueble_id:
                        cur.execute("""
                            SELECT i.id FROM inmuebles_ph i
                            JOIN contactos c ON c.id=i.contacto_id
                            WHERE c.identificacion=%s ORDER BY i.id DESC LIMIT 1
                        """, (ident,))
                        r_inm = cur.fetchone()
                        inmueble_id = int(r_inm["id"]) if r_inm else None

                    n = max(1, min(int(numero_cuotas or 1), 120))
                    frecuencia = str(frecuencia or "MENSUAL").upper()
                    if frecuencia not in {"MENSUAL", "QUINCENAL", "SEMANAL"}:
                        frecuencia = "MENSUAL"
                    total = round(float(valor_acordado or 0), 2)
                    primera = date.fromisoformat(str(fecha_compromiso))
                    abogado_id = str(getattr(request.state, "user_id", "") or "") or None

                    cur.execute("""
                        INSERT INTO acuerdos_pago
                            (inmueble_id,identificacion_deudor,nombre_deudor,telefono,
                             valor_acordado,numero_cuotas,cuota_actual,fecha_compromiso,
                             estado,origen,observaciones,abogado_id,frecuencia)
                        VALUES (%s,%s,%s,%s,%s,%s,1,%s,'PENDIENTE','ABOGADO_HUMANO',%s,%s,%s)
                        RETURNING id
                    """, (inmueble_id,ident,nombre_real,telefono_real,total,n,primera,
                          observaciones.strip(),abogado_id,frecuencia))
                    acuerdo_id = int(cur.fetchone()["id"])

                    cuota_base = round(total / n, 2)
                    acumulado = 0.0
                    for numero in range(1, n + 1):
                        valor_cuota = cuota_base if numero < n else round(total - acumulado, 2)
                        if numero < n:
                            acumulado += valor_cuota
                        if frecuencia == "SEMANAL":
                            fecha_cuota = primera + timedelta(days=7*(numero-1))
                        elif frecuencia == "QUINCENAL":
                            fecha_cuota = primera + timedelta(days=15*(numero-1))
                        else:
                            fecha_cuota = _sumar_meses(primera, numero-1)
                        cur.execute("""
                            INSERT INTO acuerdos_pago_cuotas
                                (acuerdo_id,numero_cuota,fecha_vencimiento,valor_cuota,abogado_id)
                            VALUES (%s,%s,%s,%s,%s)
                        """, (acuerdo_id,numero,fecha_cuota,valor_cuota,abogado_id))

                    if main.expedientes_service._table_exists(cur, "gestiones_crm"):
                        cur.execute("""
                            INSERT INTO gestiones_crm
                                (inmueble_id,identificacion_deudor,tipo_contacto,resumen,promesa_pago_fecha,usuario,estado)
                            VALUES (%s,%s,'Acuerdo Manual',%s,%s,'Abogado ERP','ACTIVO')
                        """, (inmueble_id,ident,
                              f"[ACUERDO DE PAGO #{acuerdo_id}] {n} cuota(s) {frecuencia} por ${total:,.0f}.",
                              primera))

                    _audit(cur, request, "CREAR_ACUERDO", "ACUERDO_PAGO", acuerdo_id,
                           radicado_interno, ident, nombre_real, inmueble_id,
                           f"Total ${total:,.2f}; {n} cuota(s); {frecuencia}; primera {primera}")
            return main._redirect("/vencimientos", mensaje="Acuerdo+registrado+exitosamente")
        except Exception as exc:
            print(f"[AGENDA] Error creando acuerdo: {exc!r}", flush=True)
            return main._redirect("/vencimientos", error="No+fue+posible+registrar+el+acuerdo")
        finally:
            conn.release()

    @router.post("/acuerdos/cumplir", name="agenda_cumplir_acuerdo")
    def agenda_cumplir_acuerdo(request: Request, acuerdo_id: int = Form(...), cuota_id: int | None = Form(None)):
        _ensure_agenda_schema()
        conn = db.get_connection()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if cuota_id:
                        cur.execute("UPDATE acuerdos_pago_cuotas SET estado='CUMPLIDO',updated_at=CURRENT_TIMESTAMP WHERE id=%s AND acuerdo_id=%s", (cuota_id,acuerdo_id))
                        cur.execute("""
                            SELECT c.numero_cuota,c.identificacion_deudor,c.nombre_deudor,a.inmueble_id
                            FROM acuerdos_pago_cuotas c JOIN acuerdos_pago a ON a.id=c.acuerdo_id WHERE c.id=%s
                        """, (cuota_id,))
                        c = cur.fetchone()
                        cur.execute("""
                            SELECT COUNT(*) total, COUNT(*) FILTER (WHERE estado='CUMPLIDO' AND NOT COALESCE(anulado,FALSE)) cumplidas
                            FROM acuerdos_pago_cuotas WHERE acuerdo_id=%s
                        """, (acuerdo_id,))
                        st = cur.fetchone()
                        if st and int(st["total"] or 0) == int(st["cumplidas"] or 0) and int(st["total"] or 0) > 0:
                            cur.execute("UPDATE acuerdos_pago SET estado='CUMPLIDO',cuota_actual=numero_cuotas,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (acuerdo_id,))
                        else:
                            cur.execute("""
                                UPDATE acuerdos_pago SET cuota_actual=LEAST(numero_cuotas,COALESCE((SELECT MAX(numero_cuota) FROM acuerdos_pago_cuotas WHERE acuerdo_id=%s AND estado='CUMPLIDO'),0)+1),updated_at=CURRENT_TIMESTAMP WHERE id=%s
                            """, (acuerdo_id,acuerdo_id))
                        if c:
                            _audit(cur, request, "CUMPLIR_CUOTA", "ACUERDO_PAGO", cuota_id,
                                   None,c["identificacion_deudor"],c["nombre_deudor"],c["inmueble_id"],f"Cuota {c['numero_cuota']} cumplida")
                    else:
                        cur.execute("UPDATE acuerdos_pago SET estado='CUMPLIDO',cuota_actual=numero_cuotas,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (acuerdo_id,))
                        cur.execute("UPDATE acuerdos_pago_cuotas SET estado='CUMPLIDO',updated_at=CURRENT_TIMESTAMP WHERE acuerdo_id=%s AND NOT COALESCE(anulado,FALSE)", (acuerdo_id,))
                        _audit(cur, request, "CUMPLIR_ACUERDO", "ACUERDO_PAGO", acuerdo_id, None,None,None,None,"Acuerdo completo cumplido")
            return main._redirect("/vencimientos", mensaje="Pago+registrado")
        finally:
            conn.release()

    @router.post("/acuerdos/anular", name="agenda_anular_acuerdo")
    def agenda_anular_acuerdo(request: Request, acuerdo_id: int = Form(...)):
        _ensure_agenda_schema()
        conn = db.get_connection()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT identificacion_deudor,nombre_deudor,inmueble_id FROM acuerdos_pago WHERE id=%s", (acuerdo_id,))
                    a = cur.fetchone()
                    if not a:
                        raise HTTPException(status_code=404, detail="Acuerdo no encontrado")
                    cur.execute("UPDATE acuerdos_pago SET estado='ANULADO',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (acuerdo_id,))
                    cur.execute("UPDATE acuerdos_pago_cuotas SET estado='ANULADO',anulado=TRUE,updated_at=CURRENT_TIMESTAMP WHERE acuerdo_id=%s", (acuerdo_id,))
                    cur.execute("UPDATE vencimientos SET anulado=TRUE WHERE tipo='ACUERDO_PAGO' AND observaciones ILIKE %s", (f"%Acuerdo #{acuerdo_id}%",))
                    _audit(cur, request, "ANULAR_ACUERDO", "ACUERDO_PAGO", acuerdo_id,None,a["identificacion_deudor"],a["nombre_deudor"],a["inmueble_id"],"Acuerdo y cuotas anulados")
            return main._redirect("/vencimientos", mensaje="Acuerdo+anulado")
        finally:
            conn.release()

    @router.post("/vencimientos/guardar", name="agenda_guardar_vencimiento")
    def agenda_guardar_vencimiento(
        request: Request,
        radicado_interno: str = Form(...),
        titulo: str = Form(...),
        fecha_vencimiento: date = Form(...),
        observaciones: str = Form(""),
        categoria: str = Form("TERMINO"),
        inmueble_id: int | None = Form(None),
    ):
        _ensure_agenda_schema()
        categoria = str(categoria or "TERMINO").upper()
        if categoria not in {"TERMINO","OTROS"}:
            categoria = "TERMINO"
        conn = db.get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    abogado_id = str(getattr(request.state,"user_id","") or "") or None
                    cur.execute("""
                        INSERT INTO vencimientos
                            (radicado_interno,titulo,fecha_vencimiento,observaciones,completado,tipo,valor,inmueble_id,anulado,categoria,abogado_id)
                        VALUES (%s,%s,%s,%s,FALSE,%s,0,%s,FALSE,%s,%s) RETURNING id
                    """, (radicado_interno.strip(),titulo.strip(),fecha_vencimiento,observaciones.strip(),categoria,inmueble_id,categoria,abogado_id))
                    registro_id = cur.fetchone()[0]
                    _audit(cur,request,"CREAR_VENCIMIENTO",categoria,registro_id,radicado_interno,None,None,inmueble_id,titulo.strip())
            return main._redirect("/vencimientos", mensaje="Vencimiento+registrado")
        except Exception as exc:
            print(f"[AGENDA] Error creando vencimiento: {exc!r}", flush=True)
            return main._redirect("/vencimientos", error="No+fue+posible+registrar+el+vencimiento")
        finally:
            conn.release()

    @router.post("/vencimientos/completar", name="agenda_completar_vencimiento")
    def agenda_completar_vencimiento(request: Request, vencimiento_id: int = Form(...)):
        _ensure_agenda_schema()
        conn = db.get_connection()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT radicado_interno,titulo,inmueble_id FROM vencimientos WHERE id=%s", (vencimiento_id,))
                    v = cur.fetchone()
                    if not v:
                        raise HTTPException(status_code=404, detail="Vencimiento no encontrado")
                    cur.execute("UPDATE vencimientos SET completado=TRUE WHERE id=%s", (vencimiento_id,))
                    _audit(cur,request,"COMPLETAR_VENCIMIENTO","VENCIMIENTO",vencimiento_id,v["radicado_interno"],None,None,v["inmueble_id"],v["titulo"])
            return main._redirect("/vencimientos", mensaje="Vencimiento+completado")
        finally:
            conn.release()

    @router.post("/vencimientos/anular", name="agenda_anular_vencimiento")
    def agenda_anular_vencimiento(request: Request, vencimiento_id: int = Form(...)):
        _ensure_agenda_schema()
        conn = db.get_connection()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT radicado_interno,titulo,inmueble_id,categoria FROM vencimientos WHERE id=%s", (vencimiento_id,))
                    v = cur.fetchone()
                    if not v:
                        raise HTTPException(status_code=404, detail="Vencimiento no encontrado")
                    cur.execute("UPDATE vencimientos SET anulado=TRUE WHERE id=%s", (vencimiento_id,))
                    _audit(cur,request,"ANULAR_VENCIMIENTO",str(v["categoria"] or "TERMINO"),vencimiento_id,v["radicado_interno"],None,None,v["inmueble_id"],v["titulo"])
            return main._redirect("/vencimientos", mensaje="Vencimiento+anulado")
        finally:
            conn.release()

    @router.get("/__interno/agenda-log.xlsx", include_in_schema=False, name="agenda_log_xlsx")
    def agenda_log_xlsx(request: Request):
        if not getattr(request.state,"user_id",None):
            raise HTTPException(status_code=401, detail="No autorizado")
        _ensure_agenda_schema()
        conn = db.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT fecha,abogado_id,abogado_nombre,accion,tipo,registro_id,radicado_interno,
                           identificacion_deudor,nombre_deudor,inmueble_id,detalle
                    FROM agenda_auditoria ORDER BY fecha DESC,id DESC
                """)
                rows=[dict(r) for r in cur.fetchall()]
            wb=openpyxl.Workbook(); ws=wb.active; ws.title="Auditoria Agenda"
            headers=["Fecha","Abogado ID","Abogado","Accion","Tipo","Registro ID","Radicado","Identificacion","Deudor","Inmueble ID","Detalle"]
            ws.append(headers)
            for c in ws[1]: c.font=Font(bold=True)
            for r in rows: ws.append([r.get(k) for k in ["fecha","abogado_id","abogado_nombre","accion","tipo","registro_id","radicado_interno","identificacion_deudor","nombre_deudor","inmueble_id","detalle"]])
            ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width=min(max(len(str(x.value or "")) for x in col)+2,45)
            output=io.BytesIO(); wb.save(output); output.seek(0)
            return StreamingResponse(output,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":"attachment; filename=agenda_auditoria_interna.xlsx","Cache-Control":"no-store"})
        finally:
            conn.release()

    return router


def _instalar_agenda_avanzada() -> None:
    """Coloca las rutas nuevas delante de las rutas heredadas equivalentes."""
    _ensure_agenda_schema()
    router = _crear_router_agenda()
    main.app.include_router(router)
    prefixed = []
    normal = []
    target_names = {"agenda_vencimientos","agenda_guardar_acuerdo","agenda_cumplir_acuerdo","agenda_anular_acuerdo","agenda_guardar_vencimiento","agenda_completar_vencimiento","agenda_anular_vencimiento","agenda_log_xlsx"}
    for route in main.app.router.routes:
        if getattr(getattr(route, "endpoint", None), "__name__", "") in target_names:
            prefixed.append(route)
        else:
            normal.append(route)
    main.app.router.routes = prefixed + normal


def _migrar_propietarios_multiples():
    try:
        _ensure_agenda_schema()
        log_msg("🏠 [PROPIETARIOS]", "Vínculos múltiples de propietarios sincronizados")
    except Exception as exc:
        log_msg("⚠️ [PROPIETARIOS]", f"Aviso sincronizando propietarios: {exc}")


def _ejecutar_mantenimiento_segundo_plano():
    time.sleep(1)
    log_msg("⚙️ [BACKGROUND]", "Iniciando tareas de verificación y sincronización...")

    try:
        migrated = security.migrate_legacy_passwords(db.POOL)
        if migrated:
            log_msg("🔑 [SEGURIDAD]", f"Migradas {migrated} contraseñas heredadas a bcrypt")
    except Exception as exc:
        log_msg("⚠️ [SEGURIDAD]", f"Aviso en migración de contraseñas: {exc}")

    try:
        liquidador.dedupe_expensas()
        tasas.asegurar_tabla_tasas()
        _migrar_propietarios_multiples()
        tasas.prueba_conexion_sfc()
        log_msg("✅ [BACKGROUND]", "Mantenimiento inicial completado")
    except Exception as exc:
        log_msg("⚠️ [BACKGROUND]", f"Aviso en mantenimiento inicial: {exc}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    db.install_psycopg2_pool()
    _instalar_agenda_avanzada()
    hilo_mantenimiento = threading.Thread(target=_ejecutar_mantenimiento_segundo_plano, daemon=True)
    hilo_mantenimiento.start()
    log_msg("🚀 [ARRANQUE]", f"Iniciando ERP en puerto {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port)
