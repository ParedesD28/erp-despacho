"""Módulo de Mensajería y Cobranza SMS para el ERP.

Arquitectura:
    FastAPI (sms_router) -> db.get_connection() -> Neon PostgreSQL
                         -> Android Gateway / SIM local
"""

from __future__ import annotations

import os
import time
import random
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional

import requests
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

# Zona horaria nativa de Colombia sin dependencias externas
try:
    from zoneinfo import ZoneInfo
    TZ_COLOMBIA = ZoneInfo("America/Bogota")
except Exception:
    TZ_COLOMBIA = timezone(timedelta(hours=-5))

import db

router = APIRouter(prefix="/sms", tags=["SMS"])
templates = Jinja2Templates(directory="templates")

# Configuración del Gateway y WhatsApp oficial
ANDROID_GATEWAY_URL = os.getenv("ANDROID_GATEWAY_URL", "http://192.168.1.92:8080/send-sms")
raw_wa = os.getenv("WHATSAPP_AGENTE_NUMBER", "573106927812").replace("+", "").strip()
WHATSAPP_AGENTE = raw_wa if raw_wa.startswith("57") else f"57{raw_wa}"


# =============================================================================
# AUTO-INICIALIZACIÓN DE TABLAS EN NEON
# =============================================================================

def _ensure_sms_schema():
    """Crea las tablas de plantillas y cola si no existen en la base de datos."""
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sms_cola_envios (
                        id BIGSERIAL PRIMARY KEY,
                        inmueble_id INT REFERENCES inmuebles_ph(id) ON DELETE SET NULL,
                        identificacion VARCHAR(50) NOT NULL,
                        nombre VARCHAR(255),
                        conjunto_residencial VARCHAR(255),
                        torre_apto VARCHAR(100),
                        telefono VARCHAR(20) NOT NULL,
                        saldo_calculado NUMERIC(14, 2) DEFAULT 0.0,
                        mensaje_texto TEXT NOT NULL,
                        tipo_campana VARCHAR(50) DEFAULT 'PREJUDICIAL',
                        estado VARCHAR(30) DEFAULT 'PENDIENTE',
                        fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        fecha_envio TIMESTAMP,
                        error_detalle TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_sms_cola_estado ON sms_cola_envios(estado);
                    CREATE INDEX IF NOT EXISTS idx_sms_cola_telefono ON sms_cola_envios(telefono);

                    CREATE TABLE IF NOT EXISTS sms_plantillas (
                        id SERIAL PRIMARY KEY,
                        nombre VARCHAR(100) NOT NULL,
                        tipo VARCHAR(50) NOT NULL UNIQUE,
                        cuerpo_template TEXT NOT NULL,
                        es_predeterminada BOOLEAN DEFAULT FALSE,
                        actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    INSERT INTO sms_plantillas (nombre, tipo, cuerpo_template, es_predeterminada)
                    VALUES 
                    (
                        'Acuerdo Prejudicial Amistoso', 
                        'PREJUDICIAL', 
                        '{nombre}, presenta saldo en mora de ${saldo} en {conjunto} {unidad}. Evite cobro judicial y acuerde su pago al WhatsApp {telefono_wa}.', 
                        TRUE
                    ),
                    (
                        'Aviso de Inicio de Cobro Jurídico', 
                        'COBRO_JURIDICO', 
                        'Aviso Juridico: {nombre}, se iniciara proceso ejecutivo por mora de ${saldo} en {conjunto} {unidad}. Evite embargo y acuerde pago al WhatsApp {telefono_wa}.', 
                        FALSE
                    ),
                    (
                        'Alerta de Mandamiento de Pago', 
                        'MANDAMIENTO', 
                        'Urgente: {nombre}, mandamiento de pago en tramite para {conjunto} {unidad} (${saldo}). Comuniquese al WhatsApp {telefono_wa} antes de medidas cautelares.', 
                        FALSE
                    )
                    ON CONFLICT (tipo) DO NOTHING;
                """)
    except Exception as e:
        print(f"[SMS SCHEMA] Error asegurando tablas: {e}", flush=True)
    finally:
        conn.release()


# =============================================================================
# REGLAS LEGALES (LEY 2300 DE 2023) Y HELPERS
# =============================================================================

def validar_horario_ley_2300() -> Tuple[bool, str]:
    """Valida los horarios de cobranza para Colombia según la Ley 2300 de 2023."""
    ahora = datetime.now(TZ_COLOMBIA)
    dia = ahora.weekday()  # 0: Lunes ... 5: Sábado, 6: Domingo
    hora = ahora.hour + (ahora.minute / 60.0)

    if dia == 6:
        return False, "Domingo: Prohibida la gestión según Ley 2300 de 2023."
    if 0 <= dia <= 4:
        if 7.0 <= hora < 19.0:
            return True, f"Horario hábil (L-V 7:00 am a 7:00 pm). Hora: {ahora.strftime('%H:%M')}."
        return False, f"Fuera de horario legal (7:00 am a 7:00 pm). Hora: {ahora.strftime('%H:%M')}."
    if dia == 5:
        if 8.0 <= hora < 15.0:
            return True, f"Horario hábil sábado (8:00 am a 3:00 pm). Hora: {ahora.strftime('%H:%M')}."
        return False, f"Fuera de horario legal de sábado (8:00 am a 3:00 pm). Hora: {ahora.strftime('%H:%M')}."
    return False, "Fuera de horario legal."


def normalizar_telefono(raw_tel: str) -> Optional[str]:
    """Extrae un celular válido de 10 dígitos para Colombia."""
    if not raw_tel:
        return None
    digitos = "".join(filter(str.isdigit, str(raw_tel)))
    if len(digitos) == 10 and digitos.startswith("3"):
        return digitos
    if len(digitos) == 12 and digitos.startswith("573"):
        return digitos[2:]
    return None


def enviar_sms_gateway(telefono_10_digitos: str, mensaje: str) -> Tuple[bool, str]:
    """Despacha al Gateway Android local."""
    destinatario = f"+57{telefono_10_digitos}"
    payload = {"to": destinatario, "message": mensaje}
    try:
        r = requests.post(ANDROID_GATEWAY_URL, json=payload, timeout=12)
        if r.status_code == 200:
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            exito = data.get("success", False) or "successfully" in str(r.text).lower() or data.get("status") == "sent"
            return exito, str(data or r.text)
        return False, f"HTTP {r.status_code}: {r.text[:100]}"
    except Exception as exc:
        return False, f"Gateway offline: {exc}"


# =============================================================================
# VISTA PRINCIPAL
# =============================================================================

@router.get("")
@router.get("/")
def vista_sms(request: Request, mensaje: str = None, error: str = None):
    _ensure_sms_schema()
    es_habil, motivo_horario = validar_horario_ley_2300()
    
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    COUNT(*) FILTER (WHERE estado = 'PENDIENTE') AS pendientes,
                    COUNT(*) FILTER (WHERE estado = 'ENVIADO') AS enviados,
                    COUNT(*) FILTER (WHERE estado = 'FALLIDO') AS fallidos,
                    COALESCE(SUM(saldo_calculado) FILTER (WHERE estado = 'PENDIENTE'), 0) AS total_saldo_pendiente
                FROM sms_cola_envios;
            """)
            metricas = cur.fetchone() or {"pendientes": 0, "enviados": 0, "fallidos": 0, "total_saldo_pendiente": 0}

            cur.execute("""
                SELECT id, identificacion, nombre, conjunto_residencial, torre_apto, 
                       telefono, saldo_calculado, mensaje_texto, tipo_campana, estado, 
                       fecha_creacion, fecha_envio, error_detalle
                FROM sms_cola_envios
                ORDER BY id DESC LIMIT 50;
            """)
            cola = cur.fetchall()

            cur.execute("SELECT id, nombre, tipo, cuerpo_template FROM sms_plantillas ORDER BY id ASC;")
            plantillas = cur.fetchall()

            cur.execute("""
                SELECT COUNT(DISTINCT i.id) AS total_mora
                FROM inmuebles_ph i
                JOIN expensas_ph e ON e.inmueble_id = i.id
                WHERE e.valor_capital > 0;
            """)
            cand = cur.fetchone()
            total_mora = cand["total_mora"] if cand else 0

        return templates.TemplateResponse(
            "sms_campanas.html",
            {
                "request": request,
                "metricas": metricas,
                "cola": cola,
                "plantillas": plantillas,
                "total_mora": total_mora,
                "es_habil": es_habil,
                "motivo_horario": motivo_horario,
                "gateway_url": ANDROID_GATEWAY_URL,
                "whatsapp_oficial": WHATSAPP_AGENTE,
                "mensaje": mensaje,
                "error": error,
            }
        )
    finally:
        conn.release()


# =============================================================================
# ACCIONES DE COLA Y DESPACHO
# =============================================================================

@router.post("/generar-cola")
def generar_cola(
    tipo_campana: str = Form("PREJUDICIAL"),
    saldo_minimo: float = Form(50000.0),
):
    """Consulta deudores en expensas_ph y llena la cola sin duplicados."""
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT cuerpo_template FROM sms_plantillas WHERE tipo = %s LIMIT 1;", (tipo_campana,))
                row_t = cur.fetchone()
                template = row_t["cuerpo_template"] if row_t else "{nombre}, saldo mora en {conjunto} {unidad}. WhatsApp: {telefono_wa}"

                query_deudores = """
                WITH saldos AS (
                    SELECT inmueble_id, 
                           SUM(CASE WHEN LOWER(COALESCE(concepto, '')) != 'abono' THEN valor_capital ELSE -valor_capital END) AS saldo_total
                    FROM expensas_ph
                    GROUP BY inmueble_id
                    HAVING SUM(CASE WHEN LOWER(COALESCE(concepto, '')) != 'abono' THEN valor_capital ELSE -valor_capital END) >= %s
                )
                SELECT DISTINCT ON (c.telefono)
                    i.id AS inmueble_id,
                    i.conjunto_residencial,
                    i.torre_apto,
                    c.identificacion,
                    c.nombre,
                    c.telefono,
                    s.saldo_total
                FROM saldos s
                JOIN inmuebles_ph i ON i.id = s.inmueble_id
                JOIN contactos c ON c.id = i.contacto_id
                WHERE c.telefono IS NOT NULL AND TRIM(c.telefono) != ''
                ORDER BY c.telefono, s.saldo_total DESC;
                """
                cur.execute(query_deudores, (saldo_minimo,))
                deudores = cur.fetchall()

                insertados = 0
                for d in deudores:
                    tel = normalizar_telefono(d["telefono"])
                    if not tel:
                        continue

                    nombre_corto = (d["nombre"] or "Propietario").strip().title()
                    conjunto = (d["conjunto_residencial"] or "Copropiedad").strip()
                    unidad = (d["torre_apto"] or "").strip()
                    saldo_formato = f"{int(d['saldo_total']):,}".replace(",", ".")

                    msg = template.format(
                        nombre=nombre_corto,
                        conjunto=conjunto,
                        unidad=unidad,
                        saldo=saldo_formato,
                        telefono_wa=f"+{WHATSAPP_AGENTE}"
                    )

                    cur.execute("""
                        INSERT INTO sms_cola_envios (
                            inmueble_id, identificacion, nombre, conjunto_residencial, 
                            torre_apto, telefono, saldo_calculado, mensaje_texto, 
                            tipo_campana, estado
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDIENTE')
                    """, (
                        d["inmueble_id"], d["identificacion"], nombre_corto, conjunto,
                        unidad, tel, d["saldo_total"], msg, tipo_campana
                    ))
                    insertados += 1

        return RedirectResponse(url=f"/sms?mensaje=Se+cargaron+{insertados}+mensajes+en+la+cola", status_code=303)
    except Exception as exc:
        return RedirectResponse(url=f"/sms?error=Error+generando+cola:+{exc}", status_code=303)
    finally:
        conn.release()


def _tarea_despacho_background(limite: int = 50, dry_run: bool = False):
    """Procesa el lote con pausas prudentes para la SIM."""
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, telefono, mensaje_texto
                FROM sms_cola_envios
                WHERE estado = 'PENDIENTE'
                ORDER BY id ASC LIMIT %s;
            """, (limite,))
            pendientes = cur.fetchall()

        for item in pendientes:
            msg_id = item["id"]
            tel = item["telefono"]
            texto = item["mensaje_texto"]

            if dry_run:
                exito, respuesta = True, "SIMULADO_OK (Dry-Run)"
            else:
                exito, respuesta = enviar_sms_gateway(tel, texto)

            with conn:
                with conn.cursor() as cur:
                    nuevo_estado = 'ENVIADO' if exito else 'FALLIDO'
                    cur.execute("""
                        UPDATE sms_cola_envios
                        SET estado = %s, fecha_envio = CURRENT_TIMESTAMP, error_detalle = %s
                        WHERE id = %s;
                    """, (nuevo_estado, respuesta[:200], msg_id))

            if not dry_run:
                time.sleep(random.uniform(3.0, 5.0))
    finally:
        conn.release()


@router.post("/despachar")
def despachar_campana(
    background_tasks: BackgroundTasks,
    limite: int = Form(50),
    forzar_horario: bool = Form(False),
    dry_run: bool = Form(False),
):
    es_habil, motivo = validar_horario_ley_2300()
    if not es_habil and not forzar_horario and not dry_run:
        return RedirectResponse(url=f"/sms?error=Detenido+por+Ley+2300:+{motivo}", status_code=303)

    background_tasks.add_task(_tarea_despacho_background, limite, dry_run)
    modo = "Simulación" if dry_run else "Despacho real"
    return RedirectResponse(url=f"/sms?mensaje={modo}+iniciado+en+segundo+plano", status_code=303)


@router.post("/cola/limpiar")
def limpiar_cola():
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE sms_cola_envios SET estado = 'CANCELADO' WHERE estado = 'PENDIENTE';")
        return RedirectResponse(url="/sms?mensaje=Cola+cancelada+correctamente", status_code=303)
    finally:
        conn.release()


@router.post("/plantillas/guardar")
def guardar_plantilla(tipo: str = Form(...), cuerpo: str = Form(...)):
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE sms_plantillas SET cuerpo_template = %s, actualizado_en = CURRENT_TIMESTAMP WHERE tipo = %s;", (cuerpo.strip(), tipo.strip()))
        return RedirectResponse(url="/sms?mensaje=Plantilla+actualizada", status_code=303)
    finally:
        conn.release()
