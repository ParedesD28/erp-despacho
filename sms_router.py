"""
Módulo de Mensajería y Cobranza SMS para el ERP.

Características:
- Cola PostgreSQL persistente.
- Idempotencia estricta en generación de cola.
- Claim exclusivo mediante SKIP LOCKED.
- Token de reclamación por ejecución para evitar reportes tardíos.
- Recuperación de mensajes EN_PROCESO abandonados.
- Soporte de cotitulares mediante inmueble_propietarios.
- Auditoría exclusivamente en gestiones_crm.
- Auditoría CRM idempotente por SMS.
- Cumplimiento de Ley 2300.
- Bypass web únicamente para roles autorizados.
- API móvil protegida mediante Bearer token.
- Dry-run sin alterar auditoría real.
"""

from __future__ import annotations

import os
import time
import random
import secrets
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional, List, Literal, Set, Dict, Any

import requests
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor
from fastapi import (
    APIRouter,
    Request,
    Form,
    BackgroundTasks,
    Query,
    Header,
    HTTPException,
    status,
)
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

try:
    from zoneinfo import ZoneInfo
    TZ_COLOMBIA = ZoneInfo("America/Bogota")
except Exception:
    TZ_COLOMBIA = timezone(timedelta(hours=-5))

import db
import expedientes_service

router = APIRouter(prefix="/sms", tags=["SMS"])
templates = Jinja2Templates(directory="templates")

ANDROID_GATEWAY_URL = os.getenv("ANDROID_GATEWAY_URL", "").strip()
raw_wa = os.getenv("WHATSAPP_AGENTE_NUMBER", "573106927812").replace("+", "").strip()
WHATSAPP_AGENTE = raw_wa if raw_wa.startswith("57") else f"57{raw_wa}"

TIMEOUT_RECLAMO_MINUTOS = 10
CAMPAÑAS_VALIDAS = {"PREJUDICIAL", "COBRO_JURIDICO", "MANDAMIENTO"}
CARTERAS_VALIDAS = {"PREJURIDICO", "JURIDICO"}
ROLES_FORZAR_HORARIO = {
    "admin",
    "administrador",
    "abogado_director",
    "director",
    "superadmin",
}


def _get_api_token() -> str:
    token = os.getenv("SMS_API_TOKEN")
    if not token or not token.strip():
        raise RuntimeError(
            "CRÍTICO: La variable de entorno 'SMS_API_TOKEN' no está configurada en Render."
        )
    return token.strip()


def _validar_permiso_forzar_horario(request: Request) -> None:
    """Valida el rol real del usuario autenticado por el middleware del ERP."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No existe una sesión de usuario válida.",
        )

    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'abogados'
                  AND column_name IN ('rol', 'role', 'perfil', 'cargo', 'tipo_usuario')
                ORDER BY CASE column_name
                    WHEN 'rol' THEN 1
                    WHEN 'role' THEN 2
                    WHEN 'perfil' THEN 3
                    WHEN 'cargo' THEN 4
                    WHEN 'tipo_usuario' THEN 5
                    ELSE 99
                END
                LIMIT 1;
                """
            )
            columna = cur.fetchone()
            if not columna:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No existe un campo de rol/perfil autorizado en la tabla de abogados.",
                )

            nombre_columna = columna["column_name"]
            if nombre_columna not in {"rol", "role", "perfil", "cargo", "tipo_usuario"}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Campo de autorización no permitido.",
                )

            cur.execute(
                f"SELECT {nombre_columna} AS rol FROM abogados WHERE id = %s LIMIT 1;",
                (user_id,),
            )
            usuario = cur.fetchone()
            if not usuario:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Usuario no encontrado.",
                )

            rol = str(usuario.get("rol") or "").strip().lower()
            if rol not in ROLES_FORZAR_HORARIO:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Solo el Abogado Director o Administrador pueden forzar envíos fuera del horario legal."
                    ),
                )
    finally:
        conn.release()


def _ensure_sms_schema() -> None:
    """Asegura el esquema SMS y sus migraciones sin borrar información existente."""
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                expedientes_service._cols(cur, "procesos")

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sms_cola_envios (
                        id BIGSERIAL PRIMARY KEY,
                        inmueble_id INT REFERENCES inmuebles_ph(id) ON DELETE SET NULL,
                        contacto_id INT REFERENCES contactos(id) ON DELETE SET NULL,
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
                        fecha_proceso TIMESTAMP,
                        fecha_envio TIMESTAMP,
                        error_detalle TEXT,
                        processing_token VARCHAR(128),
                        crm_auditado BOOLEAN NOT NULL DEFAULT FALSE,
                        crm_auditoria_fecha TIMESTAMP
                    );
                    """
                )

                cur.execute(
                    """
                    ALTER TABLE sms_cola_envios
                    ADD COLUMN IF NOT EXISTS contacto_id INT REFERENCES contactos(id) ON DELETE SET NULL;
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE sms_cola_envios
                    ADD COLUMN IF NOT EXISTS fecha_proceso TIMESTAMP;
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE sms_cola_envios
                    ADD COLUMN IF NOT EXISTS processing_token VARCHAR(128);
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE sms_cola_envios
                    ADD COLUMN IF NOT EXISTS crm_auditado BOOLEAN NOT NULL DEFAULT FALSE;
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE sms_cola_envios
                    ADD COLUMN IF NOT EXISTS crm_auditoria_fecha TIMESTAMP;
                    """
                )

                cur.execute(
                    """
                    UPDATE sms_cola_envios s
                    SET contacto_id = i.contacto_id
                    FROM inmuebles_ph i
                    WHERE s.inmueble_id = i.id
                      AND s.contacto_id IS NULL;
                    """
                )

                cur.execute(
                    """
                    UPDATE sms_cola_envios
                    SET estado = 'PENDIENTE',
                        fecha_proceso = NULL,
                        processing_token = NULL
                    WHERE estado = 'EN_PROCESO'
                      AND processing_token IS NULL;
                    """
                )

                cur.execute(
                    """
                    WITH duplicados AS (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY inmueble_id, telefono, tipo_campana
                                   ORDER BY id DESC
                               ) AS pos
                        FROM sms_cola_envios
                        WHERE estado IN ('PENDIENTE', 'EN_PROCESO')
                    )
                    UPDATE sms_cola_envios
                    SET estado = 'CANCELADO',
                        processing_token = NULL,
                        error_detalle =
                            COALESCE(error_detalle, '')
                            || CASE WHEN COALESCE(error_detalle, '') = '' THEN '' ELSE ' | ' END
                            || 'Auto-cancelado por deduplicación histórica.'
                    WHERE id IN (
                        SELECT id FROM duplicados WHERE pos > 1
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sms_cola_estado
                    ON sms_cola_envios(estado);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sms_cola_proceso
                    ON sms_cola_envios(estado, fecha_proceso);
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sms_cola_telefono
                    ON sms_cola_envios(telefono);
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_cola_inm_tel_campana_activo
                    ON sms_cola_envios (inmueble_id, telefono, tipo_campana)
                    WHERE estado IN ('PENDIENTE', 'EN_PROCESO');
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sms_plantillas (
                        id SERIAL PRIMARY KEY,
                        nombre VARCHAR(100) NOT NULL,
                        tipo VARCHAR(50) NOT NULL UNIQUE,
                        cuerpo_template TEXT NOT NULL,
                        es_predeterminada BOOLEAN DEFAULT FALSE,
                        actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

                cur.execute(
                    """
                    INSERT INTO sms_plantillas (nombre, tipo, cuerpo_template, es_predeterminada)
                    VALUES
                    ('Acuerdo Prejudicial Amistoso', 'PREJUDICIAL',
                     'Prejuridico: {nombre}, registra mora de ${saldo} en {conjunto} {unidad}. Evite cobro judicial y acuerde su pago al WhatsApp {telefono_wa}.', TRUE),
                    ('Aviso de Inicio de Cobro Jurídico', 'COBRO_JURIDICO',
                     'Aviso Juridico: {nombre}, inicio de proceso ejecutivo por mora de ${saldo} en {conjunto} {unidad}. Evite embargo y concilie al WhatsApp {telefono_wa}.', FALSE),
                    ('Alerta de Mandamiento de Pago', 'MANDAMIENTO',
                     'Urgente: {nombre}, mandamiento de pago en tramite para {conjunto} {unidad} (${saldo}). Comuniquese al WhatsApp {telefono_wa} antes de medidas cautelares.', FALSE)
                    ON CONFLICT (tipo) DO NOTHING;
                    """
                )
    except Exception as exc:
        raise RuntimeError(
            f"[SMS SCHEMA ERROR] Falló la migración del esquema SMS en Neon: {exc}"
        ) from exc
    finally:
        conn.release()


def _columnas_existentes_crm(cur) -> Set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'gestiones_crm'
          AND table_schema = 'public';
        """
    )
    return {str(row[0]).lower() for row in cur.fetchall()}


def _registrar_en_crm_idempotente(
    cur,
    item_id: int,
    inmueble_id: Optional[int],
    contacto_id: Optional[int],
    telefono: str,
    mensaje: str,
    tipo_campana: str,
    estado: str,
    error_detalle: str,
    saldo: float,
) -> bool:
    """Registra el SMS en gestiones_crm sin duplicar la auditoría."""
    cur.execute(
        """
        SELECT crm_auditado
        FROM sms_cola_envios
        WHERE id = %s
        FOR UPDATE;
        """,
        (item_id,),
    )
    fila = cur.fetchone()
    if not fila:
        raise RuntimeError(f"No existe sms_cola_envios.id={item_id}.")
    if fila[0]:
        return False

    cols = _columnas_existentes_crm(cur)
    if not cols:
        raise RuntimeError("La tabla canónica 'gestiones_crm' no existe.")

    texto_col = next(
        (col for col in ("observaciones", "resumen", "descripcion") if col in cols),
        None,
    )
    if not texto_col:
        raise RuntimeError(
            "gestiones_crm no posee una columna textual compatible "
            "(observaciones/resumen/descripcion)."
        )

    marker = f"[SMS_ID:{item_id}]"
    cur.execute(
        f"SELECT 1 FROM gestiones_crm WHERE {texto_col} LIKE %s LIMIT 1;",
        (f"{marker}%",),
    )
    if cur.fetchone():
        cur.execute(
            """
            UPDATE sms_cola_envios
            SET crm_auditado = TRUE,
                crm_auditoria_fecha = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (item_id,),
        )
        return False

    data: Dict[str, Any] = {}
    if "inmueble_id" in cols:
        data["inmueble_id"] = inmueble_id
    if "contacto_id" in cols:
        data["contacto_id"] = contacto_id

    data[texto_col] = f"{marker} [SMS - {tipo_campana}] {mensaje}"

    if "canal" in cols:
        data["canal"] = "SMS"
    elif "medio" in cols:
        data["medio"] = "SMS"

    tipo_gestion = (
        "COBRANZA_JURIDICA"
        if tipo_campana in ("COBRO_JURIDICO", "MANDAMIENTO")
        else "COBRANZA_PREJUDICIAL"
    )
    if "tipo_gestion" in cols:
        data["tipo_gestion"] = tipo_gestion
    elif "tipo" in cols:
        data["tipo"] = tipo_gestion

    resultado = (
        "ENVIADO_OPERADOR"
        if estado == "ENVIADO"
        else f"FALLIDO: {error_detalle[:100]}"
    )
    if "resultado" in cols:
        data["resultado"] = resultado
    elif "estado" in cols:
        data["estado"] = resultado

    if "telefono_contacto" in cols:
        data["telefono_contacto"] = telefono
    elif "telefono" in cols:
        data["telefono"] = telefono

    if "saldo_gestion" in cols:
        data["saldo_gestion"] = saldo
    elif "saldo" in cols:
        data["saldo"] = saldo

    if "usuario" in cols:
        data["usuario"] = "SISTEMA_SMS"
    elif "creado_por" in cols:
        data["creado_por"] = "SISTEMA_SMS"

    if "fecha_gestion" in cols:
        data["fecha_gestion"] = datetime.now(TZ_COLOMBIA)
    elif "fecha" in cols:
        data["fecha"] = datetime.now(TZ_COLOMBIA)

    columnas = list(data.keys())
    valores = list(data.values())
    placeholders = ", ".join(["%s"] * len(columnas))

    cur.execute(
        f"INSERT INTO gestiones_crm ({', '.join(columnas)}) VALUES ({placeholders});",
        valores,
    )

    cur.execute(
        """
        UPDATE sms_cola_envios
        SET crm_auditado = TRUE,
            crm_auditoria_fecha = CURRENT_TIMESTAMP
        WHERE id = %s;
        """,
        (item_id,),
    )
    return True


def _asentar_resultado_sms_y_crm(
    item_id: int,
    processing_token: str,
    estado_final: Literal["ENVIADO", "FALLIDO", "SIMULADO"],
    respuesta_gateway: str,
) -> bool:
    """Asienta el SMS y luego la auditoría CRM sin revertir el envío."""
    conn_sms = db.get_connection()
    item = None
    try:
        with conn_sms:
            with conn_sms.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE sms_cola_envios
                    SET estado = %s,
                        fecha_envio = CURRENT_TIMESTAMP,
                        error_detalle = %s,
                        fecha_proceso = NULL,
                        processing_token = NULL
                    WHERE id = %s
                      AND estado = 'EN_PROCESO'
                      AND processing_token = %s
                    RETURNING id, inmueble_id, contacto_id, telefono,
                              mensaje_texto, tipo_campana, saldo_calculado;
                    """,
                    (
                        estado_final,
                        str(respuesta_gateway)[:200],
                        item_id,
                        processing_token,
                    ),
                )
                item = cur.fetchone()
                if not item:
                    return False
    except Exception as exc:
        print(
            f"[ERROR CRÍTICO] No fue posible asentar SMS id={item_id}: {exc}",
            flush=True,
        )
        return False
    finally:
        conn_sms.release()

    if estado_final == "SIMULADO":
        return True

    conn_crm = db.get_connection()
    try:
        with conn_crm:
            with conn_crm.cursor() as cur:
                _registrar_en_crm_idempotente(
                    cur=cur,
                    item_id=item["id"],
                    inmueble_id=item["inmueble_id"],
                    contacto_id=item["contacto_id"],
                    telefono=item["telefono"],
                    mensaje=item["mensaje_texto"],
                    tipo_campana=item["tipo_campana"],
                    estado=estado_final,
                    error_detalle=str(respuesta_gateway),
                    saldo=float(item["saldo_calculado"] or 0),
                )
    except Exception as exc_crm:
        print(
            f"[ALERTA AUDITORÍA CRM] SMS id={item_id} asentado como {estado_final}, "
            f"pero CRM falló: {exc_crm}",
            flush=True,
        )
        conn_err = db.get_connection()
        try:
            with conn_err:
                with conn_err.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE sms_cola_envios
                        SET error_detalle =
                            COALESCE(error_detalle, '')
                            || CASE WHEN COALESCE(error_detalle, '') = '' THEN '' ELSE ' | ' END
                            || '[ALERTA CRM: ' || %s || ']'
                        WHERE id = %s;
                        """,
                        (str(exc_crm)[:150], item_id),
                    )
        finally:
            conn_err.release()
        return True
    finally:
        conn_crm.release()

    return True


def validar_horario_ley_2300() -> Tuple[bool, str]:
    ahora = datetime.now(TZ_COLOMBIA)
    dia = ahora.weekday()
    hora = ahora.hour + (ahora.minute / 60.0)
    if dia == 6:
        return False, "Domingo: Prohibida la gestión según Ley 2300 de 2023."
    if 0 <= dia <= 4:
        if 7.0 <= hora < 19.0:
            return True, f"Horario hábil (L-V 7:00 am a 7:00 pm). Hora: {ahora.strftime('%H:%M')}."
        return False, f"Fuera de horario legal (7:00 am a 7:00 pm). Hora: {ahora.strftime('%H:%M')}."
    if 8.0 <= hora < 15.0:
        return True, f"Horario hábil sábado (8:00 am a 3:00 pm). Hora: {ahora.strftime('%H:%M')}."
    return False, f"Fuera de horario legal de sábado (8:00 am a 3:00 pm). Hora: {ahora.strftime('%H:%M')}."


def normalizar_telefono(raw_tel: str) -> Optional[str]:
    """Devuelve únicamente un celular colombiano válido de 10 dígitos."""
    if not raw_tel:
        return None
    partes = (
        str(raw_tel)
        .replace("/", " ")
        .replace("-", " ")
        .replace(",", " ")
        .replace(";", " ")
        .split()
    )
    for parte in partes:
        digitos = "".join(filter(str.isdigit, parte))
        if len(digitos) == 10 and digitos.startswith("3"):
            return digitos
        if len(digitos) == 12 and digitos.startswith("573"):
            return digitos[2:]
    digitos_puros = "".join(filter(str.isdigit, str(raw_tel)))
    if len(digitos_puros) == 10 and digitos_puros.startswith("3"):
        return digitos_puros
    if len(digitos_puros) == 12 and digitos_puros.startswith("573"):
        return digitos_puros[2:]
    return None


def enviar_sms_gateway(telefono_10_digitos: str, mensaje: str) -> Tuple[bool, str]:
    if not ANDROID_GATEWAY_URL:
        return False, "Gateway URL no configurada en variables de entorno."
    destinatario = f"+57{telefono_10_digitos}"
    payload = {"to": destinatario, "message": mensaje}
    try:
        response = requests.post(ANDROID_GATEWAY_URL, json=payload, timeout=12)
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}: {response.text[:150]}"
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            try:
                data = response.json()
            except Exception:
                data = {}
        else:
            data = {}
        texto = str(data or response.text)
        exito = (
            data.get("success", False)
            or data.get("status") == "sent"
            or "successfully" in texto.lower()
        )
        return exito, texto
    except Exception as exc:
        return False, f"Gateway offline: {exc}"


def _saldo_clause(saldo_minimo: float, saldo_maximo: Optional[float]):
    clauses = ["s.saldo_total >= %s"]
    params = [saldo_minimo]
    if saldo_maximo is not None:
        clauses.append("s.saldo_total <= %s")
        params.append(saldo_maximo)
    return " AND ".join(clauses), params


def _candidatos_cartera(
    cur,
    tipo_cartera: str = "",
    saldo_minimo: float = 0,
    saldo_maximo: Optional[float] = None,
    ids: Optional[List[int]] = None,
):
    """Consulta cartera con cotitulares y fallback al contacto principal."""
    cartera = (tipo_cartera or "").upper().strip()
    if cartera and cartera not in CARTERAS_VALIDAS:
        raise ValueError("Tipo de cartera no válido.")

    saldo_where, params = _saldo_clause(saldo_minimo, saldo_maximo)
    where = [saldo_where, "c.telefono IS NOT NULL", "TRIM(c.telefono) <> ''"]

    if cartera:
        where.append("COALESCE(p.tipo_cartera, 'PREJURIDICO') = %s")
        params.append(cartera)
    if ids:
        where.append("i.id = ANY(%s)")
        params.append(ids)

    cur.execute("SELECT to_regclass('public.inmueble_propietarios') AS tabla;")
    fila_tabla = cur.fetchone()
    if isinstance(fila_tabla, dict):
        tiene_tabla_multiple = fila_tabla.get("tabla") is not None
    else:
        tiene_tabla_multiple = fila_tabla is not None and fila_tabla[0] is not None

    if tiene_tabla_multiple:
        propietarios_cte = """
            propietarios AS (
                SELECT ip.inmueble_id, ip.contacto_id
                FROM inmueble_propietarios ip
                UNION ALL
                SELECT i.id AS inmueble_id, i.contacto_id
                FROM inmuebles_ph i
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM inmueble_propietarios ip2
                    WHERE ip2.inmueble_id = i.id
                )
            )
        """
    else:
        propietarios_cte = """
            propietarios AS (
                SELECT i.id AS inmueble_id, i.contacto_id
                FROM inmuebles_ph i
            )
        """

    query = f"""
        WITH {propietarios_cte},
        saldos AS (
            SELECT inmueble_id,
                   SUM(
                       CASE
                           WHEN LOWER(COALESCE(concepto, '')) != 'abono'
                           THEN valor_capital
                           ELSE -valor_capital
                       END
                   ) AS saldo_total
            FROM expensas_ph
            GROUP BY inmueble_id
            HAVING SUM(
                CASE
                    WHEN LOWER(COALESCE(concepto, '')) != 'abono'
                    THEN valor_capital
                    ELSE -valor_capital
                END
            ) > 0
        )
        SELECT DISTINCT ON (i.id, c.id)
            i.id AS inmueble_id,
            c.id AS contacto_id,
            i.conjunto_residencial,
            i.torre_apto,
            c.identificacion,
            c.nombre,
            c.telefono,
            s.saldo_total,
            COALESCE(p.tipo_cartera, 'PREJURIDICO') AS tipo_cartera
        FROM saldos s
        JOIN inmuebles_ph i ON i.id = s.inmueble_id
        JOIN propietarios pr ON pr.inmueble_id = i.id
        JOIN contactos c ON c.id = pr.contacto_id
        LEFT JOIN LATERAL (
            SELECT tipo_cartera
            FROM procesos p0
            WHERE p0.inmueble_id = i.id
            ORDER BY
                CASE WHEN p0.tipo_cartera = 'JURIDICO' THEN 0 ELSE 1 END,
                p0.radicado_interno DESC
            LIMIT 1
        ) p ON TRUE
        WHERE {' AND '.join(where)}
        ORDER BY i.id, c.id, s.saldo_total DESC;
    """
    cur.execute(query, params)
    return cur.fetchall()


@router.get("")
@router.get("/")
def vista_sms(
    request: Request,
    mensaje: str = None,
    error: str = None,
    tipo_cartera: str = "",
    saldo_minimo: float = 0,
    saldo_maximo: str = "",
):
    _ensure_sms_schema()
    es_habil, motivo_horario = validar_horario_ley_2300()
    conn = db.get_connection()
    try:
        saldo_max = float(saldo_maximo) if str(saldo_maximo).strip() else None
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE estado = 'PENDIENTE') AS pendientes,
                    COUNT(*) FILTER (WHERE estado = 'EN_PROCESO') AS en_proceso,
                    COUNT(*) FILTER (WHERE estado = 'ENVIADO') AS enviados,
                    COUNT(*) FILTER (WHERE estado = 'FALLIDO') AS fallidos,
                    COUNT(*) FILTER (WHERE estado = 'SIMULADO') AS simulados,
                    COALESCE(
                        SUM(saldo_calculado) FILTER (WHERE estado IN ('PENDIENTE', 'EN_PROCESO')),
                        0
                    ) AS total_saldo_pendiente
                FROM sms_cola_envios;
                """
            )
            metricas = cur.fetchone() or {
                "pendientes": 0,
                "en_proceso": 0,
                "enviados": 0,
                "fallidos": 0,
                "simulados": 0,
                "total_saldo_pendiente": 0,
            }

            cur.execute(
                """
                SELECT id, identificacion, nombre, conjunto_residencial, torre_apto,
                       telefono, saldo_calculado, mensaje_texto, tipo_campana, estado,
                       fecha_creacion, fecha_proceso, fecha_envio, error_detalle
                FROM sms_cola_envios
                ORDER BY id DESC LIMIT 50;
                """
            )
            cola = cur.fetchall()

            cur.execute(
                "SELECT id, nombre, tipo, cuerpo_template FROM sms_plantillas ORDER BY id ASC;"
            )
            plantillas = cur.fetchall()

            cur.execute(
                """
                SELECT COUNT(DISTINCT i.id) AS total_mora
                FROM inmuebles_ph i
                JOIN expensas_ph e ON e.inmueble_id = i.id
                WHERE e.valor_capital > 0;
                """
            )
            cand = cur.fetchone()
            total_mora = cand["total_mora"] if cand else 0

            candidatos = _candidatos_cartera(
                cur,
                tipo_cartera,
                float(saldo_minimo or 0),
                saldo_max,
            )

        return templates.TemplateResponse(
            request,
            "sms_campanas.html",
            {
                "request": request,
                "metricas": metricas,
                "cola": cola,
                "plantillas": plantillas,
                "total_mora": total_mora,
                "candidatos": candidatos,
                "tipo_cartera": (tipo_cartera or "").upper(),
                "saldo_minimo": saldo_minimo,
                "saldo_maximo": saldo_maximo,
                "es_habil": es_habil,
                "motivo_horario": motivo_horario,
                "gateway_url": ANDROID_GATEWAY_URL,
                "whatsapp_oficial": WHATSAPP_AGENTE,
                "mensaje": mensaje,
                "error": error,
            },
        )
    finally:
        conn.release()


@router.post("/generar-cola")
def generar_cola(
    tipo_campana: str = Form("PREJUDICIAL"),
    saldo_minimo: float = Form(50000.0),
    saldo_maximo: str = Form(""),
    tipo_cartera: str = Form(""),
    seleccionados: List[int] = Form([]),
):
    _ensure_sms_schema()
    tipo_campana = (tipo_campana or "").upper().strip()
    if tipo_campana not in CAMPAÑAS_VALIDAS:
        return RedirectResponse(
            url="/sms?error=Tipo+de+campana+no+valido",
            status_code=303,
        )

    conn = db.get_connection()
    try:
        saldo_max = float(saldo_maximo) if saldo_maximo.strip() else None
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT cuerpo_template FROM sms_plantillas WHERE tipo=%s LIMIT 1;",
                    (tipo_campana,),
                )
                row_t = cur.fetchone()
                template = (
                    row_t["cuerpo_template"]
                    if row_t
                    else "{nombre}, mora en {conjunto} {unidad}. WhatsApp: {telefono_wa}"
                )

                deudores = _candidatos_cartera(
                    cur,
                    tipo_cartera,
                    saldo_minimo,
                    saldo_max,
                    seleccionados or None,
                )
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
                        telefono_wa=f"+{WHATSAPP_AGENTE}",
                    )

                    cur.execute(
                        """
                        INSERT INTO sms_cola_envios (
                            inmueble_id, contacto_id, identificacion, nombre,
                            conjunto_residencial, torre_apto, telefono,
                            saldo_calculado, mensaje_texto, tipo_campana, estado
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDIENTE'
                        )
                        ON CONFLICT (inmueble_id, telefono, tipo_campana)
                        WHERE estado IN ('PENDIENTE', 'EN_PROCESO')
                        DO NOTHING;
                        """,
                        (
                            d["inmueble_id"],
                            d.get("contacto_id"),
                            d["identificacion"],
                            nombre_corto,
                            conjunto,
                            unidad,
                            tel,
                            d["saldo_total"],
                            msg,
                            tipo_campana,
                        ),
                    )
                    insertados += cur.rowcount

        return RedirectResponse(
            url=f"/sms?mensaje=Se+cargaron+{insertados}+mensajes+nuevos+en+cola",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/sms?error=Error+generando+cola:+{str(exc)[:160]}",
            status_code=303,
        )
    finally:
        conn.release()


def _liberar_claims_vencidos(cur) -> None:
    cur.execute(
        f"""
        UPDATE sms_cola_envios
        SET estado = 'PENDIENTE',
            fecha_proceso = NULL,
            processing_token = NULL
        WHERE estado = 'EN_PROCESO'
          AND fecha_proceso < NOW() - INTERVAL '{TIMEOUT_RECLAMO_MINUTOS} minutes';
        """
    )


def _reclamar_lote(cur, limite: int):
    cur.execute(
        """
        SELECT id
        FROM sms_cola_envios
        WHERE estado = 'PENDIENTE'
        ORDER BY id ASC
        LIMIT %s
        FOR UPDATE SKIP LOCKED;
        """,
        (limite,),
    )
    filas = cur.fetchall()
    reclamados = []

    for fila in filas:
        token = secrets.token_urlsafe(32)
        cur.execute(
            """
            UPDATE sms_cola_envios
            SET estado = 'EN_PROCESO',
                fecha_proceso = CURRENT_TIMESTAMP,
                processing_token = %s
            WHERE id = %s
              AND estado = 'PENDIENTE'
            RETURNING id, inmueble_id, contacto_id, telefono, mensaje_texto,
                      tipo_campana, saldo_calculado, processing_token;
            """,
            (token, fila[0]),
        )
        row = cur.fetchone()
        if row:
            reclamados.append(row)

    return reclamados


def _tarea_despacho_background(limite: int = 50, dry_run: bool = False):
    """Despacho local sin mantener conexiones abiertas durante red/esperas."""
    conn = db.get_connection()
    pendientes = []
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _liberar_claims_vencidos(cur)
                pendientes = _reclamar_lote(cur, limite)
    except Exception as exc:
        print(f"[SMS DESPACHO] Error reclamando lote: {exc}", flush=True)
        return
    finally:
        conn.release()

    if not pendientes:
        return

    for item in pendientes:
        token = item["processing_token"]
        if dry_run:
            exito = True
            respuesta = "SIMULADO_OK (no se envió SMS y no se registró auditoría CRM)"
            estado_final = "SIMULADO"
        else:
            exito, respuesta = enviar_sms_gateway(
                item["telefono"],
                item["mensaje_texto"],
            )
            estado_final = "ENVIADO" if exito else "FALLIDO"

        _asentar_resultado_sms_y_crm(
            item_id=item["id"],
            processing_token=token,
            estado_final=estado_final,
            respuesta_gateway=str(respuesta),
        )

        if not dry_run:
            time.sleep(random.uniform(3.0, 5.0))


@router.post("/despachar")
def despachar_campana(
    request: Request,
    background_tasks: BackgroundTasks,
    limite: int = Form(50),
    forzar_horario: bool = Form(False),
    dry_run: bool = Form(False),
):
    limite_acotado = max(1, min(int(limite or 50), 100))
    es_habil, motivo = validar_horario_ley_2300()

    if not es_habil and not dry_run:
        if forzar_horario:
            _validar_permiso_forzar_horario(request)
        else:
            return RedirectResponse(
                url=f"/sms?error=Detenido+por+Ley+2300:+{motivo}",
                status_code=303,
            )

    if not ANDROID_GATEWAY_URL and not dry_run:
        return RedirectResponse(
            url="/sms?error=ANDROID_GATEWAY_URL+no+esta+configurada.",
            status_code=303,
        )

    background_tasks.add_task(
        _tarea_despacho_background,
        limite_acotado,
        dry_run,
    )

    modo = "Simulacion" if dry_run else "Despacho+Gateway"
    return RedirectResponse(
        url=f"/sms?mensaje={modo}+iniciado",
        status_code=303,
    )


# =============================================================================
# API MÓVIL
# =============================================================================

def _autenticar_bearer(authorization: Optional[str] = Header(None)) -> None:
    token_esperado = _get_api_token()

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Se requiere Authorization: Bearer <TOKEN>.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_recibido = authorization[7:].strip()
    if not token_recibido or not secrets.compare_digest(token_recibido, token_esperado):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de acceso inválido.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class ReporteItem(BaseModel):
    id: int
    processing_token: str = Field(..., min_length=16, max_length=128)
    estado: Literal["ENVIADO", "FALLIDO"]
    error_detalle: Optional[str] = Field(default="OK", max_length=200)


class ReporteLote(BaseModel):
    reportes: List[ReporteItem] = Field(..., min_items=1, max_items=100)


@router.get("/api/pendientes")
def api_obtener_pendientes(
    authorization: Optional[str] = Header(None),
    limite: int = Query(10, ge=1, le=50),
):
    """El móvil reclama mensajes exclusivos y recibe un processing_token por mensaje."""
    _autenticar_bearer(authorization)

    es_habil, motivo = validar_horario_ley_2300()
    if not es_habil:
        return JSONResponse(
            {"status": "bloqueado_horario", "motivo": motivo, "mensajes": []}
        )

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _liberar_claims_vencidos(cur)
                filas = _reclamar_lote(cur, limite)

        mensajes = [
            {
                "id": f["id"],
                "processing_token": f["processing_token"],
                "to": f"+57{f['telefono']}",
                "message": f["mensaje_texto"],
            }
            for f in filas
        ]
        return JSONResponse(
            {
                "status": "ok",
                "cantidad": len(mensajes),
                "mensajes": mensajes,
            }
        )
    finally:
        conn.release()


@router.post("/api/reportar")
def api_reportar_envio(
    data: ReporteLote,
    authorization: Optional[str] = Header(None),
):
    """El móvil reporta el resultado validando id + estado + processing_token."""
    _autenticar_bearer(authorization)

    actualizados = 0
    rechazados = 0

    for rep in data.reportes:
        confirmado = _asentar_resultado_sms_y_crm(
            item_id=rep.id,
            processing_token=rep.processing_token,
            estado_final=rep.estado,
            respuesta_gateway=rep.error_detalle or "Reportado por cliente móvil",
        )
        if confirmado:
            actualizados += 1
        else:
            rechazados += 1

    return JSONResponse(
        {
            "status": "ok",
            "actualizados": actualizados,
            "rechazados": rechazados,
        }
    )


@router.post("/cola/limpiar")
def limpiar_cola():
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sms_cola_envios
                    SET estado = 'CANCELADO',
                        processing_token = NULL,
                        fecha_proceso = NULL
                    WHERE estado IN ('PENDIENTE', 'EN_PROCESO');
                    """
                )
        return RedirectResponse(
            url="/sms?mensaje=Cola+cancelada+correctamente",
            status_code=303,
        )
    finally:
        conn.release()


@router.post("/plantillas/guardar")
def guardar_plantilla(
    tipo: str = Form(...),
    cuerpo: str = Form(...),
):
    tipo = (tipo or "").strip().upper()
    cuerpo_limpio = (cuerpo or "").strip()

    if tipo not in CAMPAÑAS_VALIDAS:
        return RedirectResponse(
            url="/sms?error=Tipo+de+plantilla+no+valido",
            status_code=303,
        )

    if not cuerpo_limpio:
        return RedirectResponse(
            url="/sms?error=La+plantilla+no+puede+estar+vacia",
            status_code=303,
        )

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sms_plantillas
                    SET cuerpo_template = %s,
                        actualizado_en = CURRENT_TIMESTAMP
                    WHERE tipo = %s;
                    """,
                    (cuerpo_limpio, tipo),
                )
        return RedirectResponse(
            url="/sms?mensaje=Plantilla+actualizada",
            status_code=303,
        )
    finally:
        conn.release()
