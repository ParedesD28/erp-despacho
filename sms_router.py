"""
Módulo de Mensajería y Cobranza SMS para el ERP.

Arquitectura actual:
- Cola persistente en PostgreSQL/Neon.
- Worker Pull en Android/Termux mediante Bearer token.
- Envío real exclusivamente desde la SIM del teléfono.
- Reclamo exclusivo mediante FOR UPDATE SKIP LOCKED.
- Token por reclamo para evitar reportes tardíos.
- Auditoría idempotente en gestiones_crm.
- Soporte de cotitulares mediante inmueble_propietarios.
- Cumplimiento del horario de Ley 2300.
- Bloqueo de reenvío al mismo contacto durante 24 horas.

No existe despacho servidor -> Android. El servidor únicamente coloca mensajes
pendientes y el Worker Android los reclama desde /sms/api/pendientes.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from fastapi import APIRouter, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

import db
import expedientes_service
from sms_saldo_service import actualizar_item_cola, enriquecer_candidatos

try:
    from zoneinfo import ZoneInfo

    TZ_COLOMBIA = ZoneInfo("America/Bogota")
except Exception:
    TZ_COLOMBIA = timezone(timedelta(hours=-5))


router = APIRouter(prefix="/sms", tags=["SMS"])
templates = Jinja2Templates(directory="templates")

raw_wa = os.getenv("WHATSAPP_AGENTE_NUMBER", "573106927812").replace("+", "").strip()
WHATSAPP_AGENTE = raw_wa if raw_wa.startswith("57") else f"57{raw_wa}"

TIMEOUT_RECLAMO_MINUTOS = 10
BLOQUEO_REENVIO_HORAS = 24
CAMPAÑAS_VALIDAS = {"PREJUDICIAL", "COBRO_JURIDICO", "MANDAMIENTO"}
CARTERAS_VALIDAS = {"PREJURIDICO", "JURIDICO"}


def _get_api_token() -> str:
    token = os.getenv("SMS_API_TOKEN")
    if not token or not token.strip():
        raise RuntimeError(
            "CRÍTICO: La variable de entorno 'SMS_API_TOKEN' no está configurada en Render."
        )
    return token.strip()


def _ensure_sms_schema() -> None:
    """Verifica el contrato SMS en solo lectura; las migraciones hacen los cambios estructurales."""
    required = {
        "sms_cola_envios": {
            "id", "inmueble_id", "contacto_id", "identificacion", "telefono",
            "saldo_calculado", "saldo_fuente", "saldo_verificado",
            "saldo_calculado_en", "mensaje_template", "mensaje_texto",
            "tipo_campana", "estado", "fecha_creacion", "fecha_envio",
            "processing_token", "crm_auditado", "crm_auditoria_fecha",
            "obligacion_id",
        },
        "sms_plantillas": {"id", "tipo", "cuerpo_template"},
    }
    import db
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            for table, expected in required.items():
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=%s
                    """,
                    (table,),
                )
                actual = {str(row[0]) for row in cur.fetchall()}
                if not actual:
                    raise RuntimeError(f"[SMS PREFLIGHT] Falta tabla: {table}")
                missing = sorted(expected - actual)
                if missing:
                    raise RuntimeError(
                        f"[SMS PREFLIGHT] Faltan columnas en {table}: {', '.join(missing)}"
                    )
    finally:
        conn.release()


def validar_horario_ley_2300() -> Tuple[bool, str]:
    ahora = datetime.now(TZ_COLOMBIA)
    dia = ahora.weekday()
    hora = ahora.hour + ahora.minute / 60.0
    if dia == 6:
        return False, "Domingo: Prohibida la gestión según Ley 2300 de 2023."
    if 0 <= dia <= 4:
        if 7.0 <= hora < 19.0:
            return True, f"Horario hábil (L-V 7:00 am a 7:00 pm). Hora: {ahora:%H:%M}."
        return False, f"Fuera de horario legal (7:00 am a 7:00 pm). Hora: {ahora:%H:%M}."
    if 8.0 <= hora < 15.0:
        return True, f"Horario hábil sábado (8:00 am a 3:00 pm). Hora: {ahora:%H:%M}."
    return False, f"Fuera de horario legal de sábado (8:00 am a 3:00 pm). Hora: {ahora:%H:%M}."


def normalizar_telefono(raw_tel: str) -> Optional[str]:
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
    digitos = "".join(filter(str.isdigit, str(raw_tel)))
    if len(digitos) == 10 and digitos.startswith("3"):
        return digitos
    if len(digitos) == 12 and digitos.startswith("573"):
        return digitos[2:]
    return None


def _saldo_clause(
    saldo_minimo: float, saldo_maximo: Optional[float]
) -> Tuple[str, List[Any]]:
    clauses = ["s.saldo_total >= %s"]
    params: List[Any] = [saldo_minimo]
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
    conjunto: str = "",
):
    """Devuelve candidatos PH asociados a una obligación canónica activa."""
    cartera = (tipo_cartera or "").upper().strip()
    if cartera and cartera not in CARTERAS_VALIDAS:
        raise ValueError("Tipo de cartera no válido.")

    where = [
        "c.telefono IS NOT NULL",
        "TRIM(c.telefono) <> ''",
        "NOT EXISTS ("
        " SELECT 1 FROM sms_cola_envios prev"
        " WHERE prev.contacto_id = c.id"
        "   AND prev.estado = 'ENVIADO'"
        "   AND prev.fecha_envio >= NOW() - INTERVAL '24 hours'"
        ")",
    ]
    params: List[Any] = []

    if cartera:
        where.append("COALESCE(p.tipo_cartera,'PREJURIDICO')=%s")
        params.append(cartera)
    if ids:
        where.append("c.id = ANY(%s)")
        params.append(ids)
    if conjunto:
        where.append("COALESCE(i.conjunto_residencial,'')=%s")
        params.append(conjunto.strip())

    cur.execute(
        f"""
        SELECT DISTINCT ON (i.id,c.id)
            i.id AS inmueble_id,
            c.id AS contacto_id,
            i.conjunto_residencial,
            i.torre_apto,
            c.identificacion,
            c.nombre,
            c.telefono,
            o.id AS obligacion_id,
            COALESCE(p.tipo_cartera,'PREJURIDICO') AS tipo_cartera,
            p.radicado_interno
        FROM obligaciones o
        JOIN inmuebles_ph i ON i.id=o.inmueble_id
        JOIN contactos c ON c.id=o.deudor_contacto_id
        LEFT JOIN LATERAL (
            SELECT p0.tipo_cartera,p0.radicado_interno
            FROM procesos p0
            JOIN proceso_obligaciones po0
              ON po0.radicado_interno=p0.radicado_interno
             AND po0.obligacion_id=o.id
            ORDER BY po0.es_principal DESC, po0.id
            LIMIT 1
        ) p ON TRUE
        WHERE UPPER(COALESCE(o.fuente_saldo,''))='EXPENSAS_PH'
          AND UPPER(COALESCE(o.estado,'ACTIVA')) NOT IN ('CANCELADA','ANULADA')
          AND {' AND '.join(where)}
        ORDER BY i.id,c.id,o.id DESC
        """,
        params,
    )
    return cur.fetchall()

def _filtrar_saldos_verificados(candidatos, saldo_minimo: float, saldo_maximo: Optional[float]):
    """Aplica el filtro monetario sobre el saldo REAL del liquidador, no sobre el saldo preliminar."""
    salida = []
    minimo = float(saldo_minimo or 0)
    maximo = float(saldo_maximo) if saldo_maximo is not None else None
    for item in candidatos:
        if not item.get("saldo_verificado"):
            continue
        try:
            saldo = float(item.get("saldo_total") or 0)
        except (TypeError, ValueError):
            continue
        if saldo < minimo:
            continue
        if maximo is not None and saldo > maximo:
            continue
        salida.append(item)
    return salida


def _candidatos_liquidados(candidatos):
    """Asegura una sola pasada por el motor de liquidación."""
    if all(isinstance(item, dict) and "saldo_verificado" in item for item in candidatos):
        return list(candidatos)
    return enriquecer_candidatos(candidatos)


def _columnas_existentes_crm(cur) -> Set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name='gestiones_crm' AND table_schema='public';
        """
    )
    return {str(row[0]).lower() for row in cur.fetchall()}


def _registrar_en_crm_idempotente(
    cur,
    item_id: int,
    inmueble_id: Optional[int],
    contacto_id: Optional[int],
    obligacion_id: Optional[int],
    telefono: str,
    mensaje: str,
    tipo_campana: str,
    estado: str,
    error_detalle: str,
    saldo: float,
) -> bool:
    cur.execute(
        "SELECT crm_auditado FROM sms_cola_envios WHERE id=%s FOR UPDATE;",
        (item_id,),
    )
    fila = cur.fetchone()
    if not fila:
        raise RuntimeError(f"No existe sms_cola_envios.id={item_id}.")
    auditado = fila["crm_auditado"] if isinstance(fila, dict) else fila[0]
    if auditado:
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
            "UPDATE sms_cola_envios SET crm_auditado=TRUE, crm_auditoria_fecha=CURRENT_TIMESTAMP WHERE id=%s;",
            (item_id,),
        )
        return False

    data: Dict[str, Any] = {}
    if "inmueble_id" in cols:
        data["inmueble_id"] = inmueble_id
    if "contacto_id" in cols:
        data["contacto_id"] = contacto_id
    if "obligacion_id" in cols:
        if not obligacion_id:
            raise RuntimeError("El SMS no tiene obligación financiera asociada.")
        data["obligacion_id"] = obligacion_id
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
    cur.execute(
        f"INSERT INTO gestiones_crm ({', '.join(columnas)}) VALUES ({', '.join(['%s'] * len(columnas))});",
        valores,
    )
    cur.execute(
        "UPDATE sms_cola_envios SET crm_auditado=TRUE, crm_auditoria_fecha=CURRENT_TIMESTAMP WHERE id=%s;",
        (item_id,),
    )
    return True


def _asentar_resultado_sms_y_crm(
    item_id: int,
    processing_token: str,
    estado_final: Literal["ENVIADO", "FALLIDO"],
    respuesta_gateway: str,
) -> bool:
    conn_sms = db.get_connection()
    item = None
    try:
        with conn_sms:
            with conn_sms.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE sms_cola_envios
                    SET estado=%s,
                        fecha_envio=CURRENT_TIMESTAMP,
                        error_detalle=%s,
                        fecha_proceso=NULL,
                        processing_token=NULL
                    WHERE id=%s
                      AND estado='EN_PROCESO'
                      AND processing_token=%s
                    RETURNING id,inmueble_id,contacto_id,obligacion_id,telefono,
                              mensaje_texto,tipo_campana,saldo_calculado;
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

    conn_crm = db.get_connection()
    try:
        with conn_crm:
            with conn_crm.cursor() as cur:
                _registrar_en_crm_idempotente(
                    cur=cur,
                    item_id=item["id"],
                    inmueble_id=item["inmueble_id"],
                    contacto_id=item["contacto_id"],
                    obligacion_id=item["obligacion_id"],
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
                        SET error_detalle=COALESCE(error_detalle,'')
                          || CASE WHEN COALESCE(error_detalle,'')='' THEN '' ELSE ' | ' END
                          || '[ALERTA CRM: ' || %s || ']'
                        WHERE id=%s;
                        """,
                        (str(exc_crm)[:150], item_id),
                    )
        finally:
            conn_err.release()
        return True
    finally:
        conn_crm.release()
    return True


def _liberar_claims_vencidos(cur) -> None:
    cur.execute(
        f"""
        UPDATE sms_cola_envios
        SET estado='PENDIENTE', fecha_proceso=NULL, processing_token=NULL
        WHERE estado='EN_PROCESO'
          AND fecha_proceso < NOW() - INTERVAL '{TIMEOUT_RECLAMO_MINUTOS} minutes';
        """
    )


def _reclamar_lote(cur, limite: int):
    """Reclama mensajes y verifica el saldo con el liquidador antes de entregarlos."""
    cur.execute(
        """
        SELECT id
        FROM sms_cola_envios
        WHERE estado='PENDIENTE'
          AND COALESCE(saldo_verificado,FALSE)=TRUE
        ORDER BY id ASC
        LIMIT %s
        FOR UPDATE SKIP LOCKED;
        """,
        (limite,),
    )
    filas = cur.fetchall()
    reclamados = []

    for fila in filas:
        fila_id = fila["id"] if isinstance(fila, dict) else fila[0]
        token = secrets.token_urlsafe(32)
        cur.execute(
            """
            UPDATE sms_cola_envios
            SET estado='EN_PROCESO', fecha_proceso=CURRENT_TIMESTAMP, processing_token=%s
            WHERE id=%s AND estado='PENDIENTE'
            RETURNING id,inmueble_id,contacto_id,telefono,mensaje_texto,
                      tipo_campana,saldo_calculado,saldo_fuente,
                      saldo_verificado,processing_token;
            """,
            (token, fila_id),
        )
        row=cur.fetchone()
        if not row:
            continue

        try:
            refreshed=actualizar_item_cola(
                cur,
                dict(row) if isinstance(row,dict) else {
                    "id":row[0],"inmueble_id":row[1],"contacto_id":row[2],
                    "telefono":row[3],"mensaje_texto":row[4],"tipo_campana":row[5],
                },
            )
            if refreshed.get("bloqueado_envio"):
                cur.execute(
                    """
                    UPDATE sms_cola_envios
                    SET estado='FALLIDO',fecha_proceso=NULL,processing_token=NULL
                    WHERE id=%s AND estado='EN_PROCESO'
                    """,
                    (fila_id,),
                )
                continue

            cur.execute(
                """
                UPDATE sms_cola_envios
                SET processing_token=%s, fecha_proceso=CURRENT_TIMESTAMP
                WHERE id=%s AND estado='EN_PROCESO'
                RETURNING id,inmueble_id,contacto_id,telefono,mensaje_texto,
                          tipo_campana,saldo_calculado,saldo_fuente,
                          saldo_verificado,processing_token;
                """,
                (token,fila_id),
            )
            refreshed_row=cur.fetchone()
            if refreshed_row:
                reclamados.append(refreshed_row)
        except Exception as exc:
            cur.execute(
                """
                UPDATE sms_cola_envios
                SET estado='FALLIDO',
                    fecha_proceso=NULL,
                    processing_token=NULL,
                    error_detalle=%s
                WHERE id=%s AND estado='EN_PROCESO'
                """,
                (f"Saldo no pudo ser verificado antes del envío: {str(exc)[:180]}",fila_id),
            )

    return reclamados

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


@router.get("")
@router.get("/")
def vista_sms(
    request: Request,
    mensaje: Optional[str] = None,
    error: Optional[str] = None,
    tipo_cartera: str = "",
    saldo_minimo: float = 0,
    saldo_maximo: str = "",
    conjunto: str = "",
):
    _ensure_sms_schema()
    es_habil, motivo_horario = validar_horario_ley_2300()
    conn = db.get_connection()
    try:
        saldo_max = float(saldo_maximo) if str(saldo_maximo).strip() else None
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT COUNT(*) FILTER (WHERE estado='PENDIENTE') AS pendientes,
                       COUNT(*) FILTER (WHERE estado='EN_PROCESO') AS en_proceso,
                       COUNT(*) FILTER (WHERE estado='ENVIADO') AS enviados,
                       COUNT(*) FILTER (WHERE estado='FALLIDO') AS fallidos,
                       COALESCE(
                           SUM(saldo_calculado) FILTER (WHERE estado IN ('PENDIENTE','EN_PROCESO') AND COALESCE(saldo_verificado,FALSE)=TRUE),
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
                "total_saldo_pendiente": 0,
            }
            cur.execute(
                """
                SELECT id,identificacion,nombre,conjunto_residencial,torre_apto,
                       telefono,saldo_calculado,saldo_fuente,saldo_verificado,
                       saldo_calculado_en,mensaje_texto,tipo_campana,estado,
                       fecha_creacion,fecha_proceso,fecha_envio,error_detalle
                FROM sms_cola_envios
                ORDER BY id DESC LIMIT 50;
                """
            )
            cola = cur.fetchall()
            cur.execute("SELECT id,nombre,tipo,cuerpo_template FROM sms_plantillas ORDER BY id ASC;")
            plantillas = cur.fetchall()
            cur.execute(
                """
                SELECT COUNT(DISTINCT i.id) AS total_mora
                FROM inmuebles_ph i
                JOIN expensas_ph e ON e.inmueble_id=i.id
                WHERE e.valor_capital > 0;
                """
            )
            cand = cur.fetchone()
            total_mora = cand["total_mora"] if cand else 0
            cur.execute("""
                SELECT nombre
                FROM conjuntos_residenciales
                WHERE activo=TRUE
                ORDER BY nombre
            """)
            conjuntos = [str(r["nombre"]) for r in cur.fetchall()]
            candidatos = _filtrar_saldos_verificados(
                _candidatos_liquidados(
                    _candidatos_cartera(
                        cur, tipo_cartera, float(saldo_minimo or 0), saldo_max, None, conjunto
                    )
                ),
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
                "conjunto": conjunto,
                "conjuntos": conjuntos,
                "saldo_minimo": saldo_minimo,
                "saldo_maximo": saldo_maximo,
                "es_habil": es_habil,
                "motivo_horario": motivo_horario,
                "whatsapp_oficial": WHATSAPP_AGENTE,
                "mensaje": mensaje,
                "error": error,
                "bloqueo_reenvio_horas": BLOQUEO_REENVIO_HORAS,
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
    conjunto: str = Form(""),
    seleccionados: List[int] = Form([]),
):
    _ensure_sms_schema()
    tipo_campana = (tipo_campana or "").upper().strip()
    if tipo_campana not in CAMPAÑAS_VALIDAS:
        return RedirectResponse(url="/sms?error=Tipo+de+campana+no+valido", status_code=303)

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

                deudores = _filtrar_saldos_verificados(
                    _candidatos_liquidados(
                        _candidatos_cartera(
                            cur,
                            tipo_cartera,
                            saldo_minimo,
                            saldo_max,
                            seleccionados or None,
                            conjunto,
                        )
                    ),
                    saldo_minimo,
                    saldo_max,
                )
                insertados = 0
                for d in deudores:
                    tel = normalizar_telefono(d["telefono"])
                    if not tel:
                        continue

                    if not d.get("saldo_verificado"):
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
                        INSERT INTO sms_cola_envios(
                            inmueble_id,contacto_id,identificacion,nombre,
                            conjunto_residencial,torre_apto,telefono,
                            saldo_calculado,saldo_fuente,saldo_verificado,
                            saldo_calculado_en,mensaje_template,mensaje_texto,
                            tipo_campana,estado
                        )
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDIENTE')
                        ON CONFLICT(inmueble_id,telefono,tipo_campana)
                        WHERE estado IN ('PENDIENTE','EN_PROCESO') DO NOTHING;
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
                            d.get("saldo_fuente"),
                            True,
                            d.get("saldo_calculado_en"),
                            template,
                            msg,
                            tipo_campana,
                        ),
                    )
                    insertados += cur.rowcount

        if seleccionados and insertados == 0:
            return RedirectResponse(
                url="/sms?error=Los+destinatarios+seleccionados+ya+fueron+enviados+en+las+ultimas+24+horas+o+ya+estan+en+cola.",
                status_code=303,
            )
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


@router.get("/api/pendientes")
def api_obtener_pendientes(
    authorization: Optional[str] = Header(None),
    limite: int = Query(10, ge=1, le=50),
):
    """El Worker Android reclama mensajes exclusivos y recibe processing_token."""
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
            {"status": "ok", "cantidad": len(mensajes), "mensajes": mensajes}
        )
    finally:
        conn.release()


@router.post("/api/reportar")
def api_reportar_envio(
    data: ReporteLote,
    authorization: Optional[str] = Header(None),
):
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
        {"status": "ok", "actualizados": actualizados, "rechazados": rechazados}
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
                    SET estado='CANCELADO', processing_token=NULL, fecha_proceso=NULL
                    WHERE estado IN ('PENDIENTE','EN_PROCESO');
                    """
                )
        return RedirectResponse(
            url="/sms?mensaje=Cola+cancelada+correctamente",
            status_code=303,
        )
    finally:
        conn.release()


@router.post("/plantillas/guardar")
def guardar_plantilla(tipo: str = Form(...), cuerpo: str = Form(...)):
    tipo = (tipo or "").strip().upper()
    cuerpo_limpio = (cuerpo or "").strip()
    if tipo not in CAMPAÑAS_VALIDAS:
        return RedirectResponse(url="/sms?error=Tipo+de+plantilla+no+valido", status_code=303)
    if not cuerpo_limpio:
        return RedirectResponse(url="/sms?error=La+plantilla+no+puede+estar+vacia", status_code=303)

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sms_plantillas SET cuerpo_template=%s, actualizado_en=CURRENT_TIMESTAMP WHERE tipo=%s;",
                    (cuerpo_limpio, tipo),
                )
        return RedirectResponse(
            url="/sms?mensaje=Plantilla+actualizada",
            status_code=303,
        )
    finally:
        conn.release()
