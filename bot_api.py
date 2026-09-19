"""API M2M para integración con el agente inteligente de cobranza (WhatsApp)."""
import os
import io
import time
import secrets
import hashlib
import hmac
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse

import liquidador
import exportaciones

router = APIRouter()

BOT_API_KEY = (os.getenv("LIQUIDADOR_API_KEY") or "").strip().strip('"').strip("'")
_TOKEN_TTL_SECONDS = int(os.getenv("BOT_PDF_URL_TTL", "900"))
_PDF_SECRET = (
    os.getenv("BOT_PDF_PUBLIC_SECRET")
    or BOT_API_KEY
    or ""
).strip().strip('"').strip("'")


def _public_base_url() -> str:
    return (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or ""
    ).rstrip("/")


def _require_api_key(request: Request) -> None:
    expected = BOT_API_KEY
    supplied = request.headers.get("X-API-Key")
    if not expected:
        raise HTTPException(status_code=503, detail="LIQUIDADOR_API_KEY no está configurada")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="No autorizado")


def _sign(filename: str, expires: int) -> str:
    payload = f"{filename}|{expires}".encode("utf-8")
    return hmac.new(_PDF_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_signed_pdf_url(filename: str, base_url: str) -> str:
    if not _PDF_SECRET:
        raise RuntimeError("BOT_PDF_PUBLIC_SECRET/LIQUIDADOR_API_KEY no está configurada")
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    token = _sign(filename, expires)
    return f"{base_url.rstrip('/')}/api/bot/pdf/{filename}?expires={expires}&token={token}"


def _parse_payload(payload: Any) -> tuple[int | None, int | None, date]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="El cuerpo debe ser JSON")

    try:
        obligacion_id = int(payload.get("obligacion_id")) if payload.get("obligacion_id") else None
        inmueble_id = int(payload.get("inmueble_id")) if payload.get("inmueble_id") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="obligacion_id/inmueble_id deben ser enteros")

    if not obligacion_id and not inmueble_id:
        raise HTTPException(status_code=422, detail="Debe indicar obligacion_id o inmueble_id")

    fecha_texto = str(payload.get("fecha_corte") or "")
    try:
        fecha_corte = datetime.strptime(fecha_texto, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="fecha_corte debe tener formato YYYY-MM-DD")

    if fecha_corte > date.today():
        raise HTTPException(status_code=422, detail="fecha_corte no puede ser futura")

    return obligacion_id, inmueble_id, fecha_corte


def _obtener_datos_liquidacion(
    inmueble_id: int,
    fecha_corte: date,
    obligacion_id: int | None = None,
) -> tuple[list[dict], dict, tuple]:
    resultados, resumen, inm_info = liquidador.motor_calculo_judicial(
        inmueble_id,
        "usura",
        2.5,
        23.8,
        0.0,
        fecha_corte,
        obligacion_id=obligacion_id,
    )
    if not inm_info:
        raise HTTPException(status_code=404, detail="No existe información del inmueble")
    if not resultados:
        raise HTTPException(status_code=404, detail="No hay movimientos para liquidar")
    return resultados, resumen, inm_info


def _resumen_tasas(resultados: list[dict]) -> tuple[list[dict], str]:
    tasas = []
    vistos = set()
    for fila in resultados:
        periodo = fila.get("periodo") or fila.get("mes")
        desde = fila.get("desde")
        tasa_ea = fila.get("tasa_ea")
        tasa_mes = fila.get("tasa_mes")
        clave = (str(periodo), str(desde), str(tasa_ea), str(tasa_mes))
        if clave in vistos:
            continue
        vistos.add(clave)
        tasas.append({
            "periodo": periodo or desde or "",
            "fecha_desde": desde or "",
            "tasa_ea": tasa_ea or "",
            "tasa_mensual": tasa_mes or "",
            "fuente": "SFC/Neon validada" if not fila.get("tasa_personalizada") else "Tasa personalizada E.A.",
        })
    fuente = "SFC/Neon validada"
    if any(f.get("tasa_personalizada") for f in resultados):
        fuente = "Tasa personalizada E.A."
    return tasas, fuente


def _generar_pdf_unificado(inmueble_id: int, fecha_corte: date, resultados: list[dict], resumen: dict, inm_info: tuple) -> str:
    base = _public_base_url()
    if not base:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL/RENDER_EXTERNAL_URL no está configurada")
    pdf_path = exportaciones.generar_pdf_liquidacion(inmueble_id, fecha_corte, resultados, resumen, inm_info)
    filename = os.path.basename(pdf_path)
    return build_signed_pdf_url(filename, base)


@router.post("/api/bot/liquidar")
async def liquidar_para_bot(request: Request):
    _require_api_key(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="El cuerpo debe ser JSON válido")

    obligacion_id, inmueble_id, fecha_corte = _parse_payload(payload)

    import db
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            if obligacion_id:
                cur.execute(
                    """
                    SELECT o.id,o.inmueble_id,o.fuente_saldo
                    FROM obligaciones o
                    WHERE o.id=%s
                    LIMIT 1
                    """,
                    (obligacion_id,),
                )
                ob=cur.fetchone()
                if not ob:
                    raise HTTPException(status_code=404, detail="Obligación no encontrada")
                if str(ob["fuente_saldo"] or "").upper() != "EXPENSAS_PH":
                    raise HTTPException(status_code=409, detail="La liquidación PDF de este endpoint requiere una obligación PH.")
                inmueble_id=int(ob["inmueble_id"]) if ob["inmueble_id"] else None
            else:
                cur.execute(
                    """
                    SELECT o.id
                    FROM obligaciones o
                    WHERE o.inmueble_id=%s
                      AND UPPER(COALESCE(o.fuente_saldo,''))='EXPENSAS_PH'
                      AND UPPER(COALESCE(o.estado,'ACTIVA')) NOT IN ('CANCELADA','ANULADA')
                    ORDER BY o.id DESC
                    LIMIT 1
                    """,
                    (inmueble_id,),
                )
                ob=cur.fetchone()
                if not ob:
                    raise HTTPException(status_code=404, detail="No existe una obligación PH activa para el inmueble")
                obligacion_id=int(ob["id"])
    finally:
        conn.release()

    if not inmueble_id:
        raise HTTPException(status_code=404, detail="La obligación no tiene inmueble PH")

    try:
        resultados, resumen, inm_info = _obtener_datos_liquidacion(
            inmueble_id,
            fecha_corte,
            obligacion_id=obligacion_id,
        )
        url_pdf = _generar_pdf_unificado(inmueble_id, fecha_corte, resultados, resumen, inm_info)
        tasas_aplicadas, fuente_tasas = _resumen_tasas(resultados)

        print(
            f"[BOT LIQUIDADOR] inmueble={inmueble_id} corte={fecha_corte} "
            f"capital={float(resumen.get('capital', 0)):.2f} "
            f"intereses={float(resumen.get('intereses', 0)):.2f} "
            f"total={float(resumen.get('gran_total', 0)):.2f} "
            f"fuente_tasas={fuente_tasas}",
            flush=True,
        )

        return JSONResponse({
            "status": "success",
            "mensaje": "Liquidación generada correctamente",
            "datos": {
                "obligacion_id": obligacion_id,
                "inmueble_id": inmueble_id,
                "deudor": inm_info[2],
                "identificacion": inm_info[3],
                "capital": float(resumen["capital"]),
                "intereses": float(resumen["intereses"]),
                "honorarios_pct": float(resumen["honorarios_pct"]),
                "honorarios": float(resumen["honorarios"]),
                "gastos": float(resumen["gastos"]),
                "gran_total": float(resumen["gran_total"]),
                "total_exigible": float(resumen["gran_total"]),
                "tasas_aplicadas": tasas_aplicadas,
                "fuente_tasas": fuente_tasas,
                "detalle_mensual": resultados,
                "url_pdf": url_pdf,
            },
        })
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[BOT LIQUIDADOR] Error generando liquidación: {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="No fue posible generar la liquidación")


@router.get("/api/bot/pdf/{filename}", include_in_schema=False)
def servir_pdf_bot(filename: str, expires: int, token: str):
    if not _PDF_SECRET:
        raise HTTPException(status_code=503, detail="Servicio PDF no configurado")
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Enlace inválido")
    if expires_int < int(time.time()):
        raise HTTPException(status_code=410, detail="Enlace expirado")
    expected = _sign(filename, expires_int)
    if not token or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Enlace no autorizado")

    path = Path("static") / "pdfs" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=filename,
        headers={"Cache-Control": "private, max-age=900"},
    )


@router.post("/api/bot/acuerdo")
async def registrar_acuerdo_bot(request: Request):
    """Registra un acuerdo vinculado a una obligación financiera."""
    _require_api_key(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="El cuerpo debe ser JSON válido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Payload inválido")

    identificacion = str(payload.get("identificacion") or payload.get("cedula") or "").strip()
    if not identificacion:
        raise HTTPException(status_code=422, detail="identificacion es requerida")

    fecha_texto = str(payload.get("fecha_compromiso") or payload.get("fecha_pago") or "")
    try:
        fecha_compromiso = datetime.strptime(fecha_texto, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="fecha_compromiso debe tener formato YYYY-MM-DD")

    try:
        valor = float(payload.get("valor_acordado") or payload.get("valor") or 0.0)
        if valor <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="valor_acordado debe ser mayor que cero")

    try:
        obligacion_id = int(payload.get("obligacion_id")) if payload.get("obligacion_id") else None
        inmueble_id = int(payload.get("inmueble_id")) if payload.get("inmueble_id") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="obligacion_id/inmueble_id deben ser enteros")

    telefono = str(payload.get("telefono") or "").strip()
    nombre_deudor = str(payload.get("nombre_deudor") or payload.get("nombre") or "").strip()
    observaciones = str(payload.get("observaciones") or payload.get("resumen") or "Acuerdo de pago pactado vía WhatsApp con Agente IA").strip()
    numero_cuotas = max(1, min(int(payload.get("numero_cuotas") or 1), 120))
    cuota_actual = max(1, min(int(payload.get("cuota_actual") or 1), numero_cuotas))

    import db
    from psycopg2.extras import RealDictCursor

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if obligacion_id:
                    cur.execute(
                        """
                        SELECT o.id,o.inmueble_id
                        FROM obligaciones o
                        WHERE o.id=%s
                        LIMIT 1
                        """,
                        (obligacion_id,),
                    )
                    ob = cur.fetchone()
                    if not ob:
                        raise HTTPException(status_code=404, detail="Obligación no encontrada")
                    inmueble_id = int(ob["inmueble_id"]) if ob["inmueble_id"] else inmueble_id
                elif inmueble_id:
                    cur.execute(
                        """
                        SELECT o.id
                        FROM obligaciones o
                        WHERE o.inmueble_id=%s
                          AND UPPER(COALESCE(o.estado,'ACTIVA')) NOT IN ('CANCELADA','ANULADA')
                        ORDER BY o.id DESC
                        LIMIT 1
                        """,
                        (inmueble_id,),
                    )
                    ob = cur.fetchone()
                    if ob:
                        obligacion_id = int(ob["id"])
                else:
                    raise HTTPException(status_code=422, detail="Debe indicar obligacion_id o inmueble_id")

                if not obligacion_id:
                    raise HTTPException(status_code=404, detail="No existe obligación activa para la cuenta indicada")

                cur.execute(
                    """
                    SELECT c.identificacion,c.nombre,c.telefono
                    FROM obligaciones o
                    JOIN obligacion_partes op
                      ON op.obligacion_id=o.id
                     AND op.rol='DEUDOR'
                     AND op.es_principal=TRUE
                    JOIN contactos c ON c.id=op.contacto_id
                    WHERE o.id=%s
                    LIMIT 1
                    """,
                    (obligacion_id,),
                )
                deudor = cur.fetchone()
                if not deudor:
                    raise HTTPException(status_code=409, detail="La obligación no tiene un deudor principal válido")

                cur.execute(
                    """
                    SELECT c.identificacion,c.nombre,c.telefono
                    FROM obligacion_partes op
                    JOIN contactos c ON c.id=op.contacto_id
                    WHERE op.obligacion_id=%s
                      AND op.rol='DEUDOR'
                      AND REGEXP_REPLACE(COALESCE(c.identificacion::text,''),'[^0-9]','','g')
                          = REGEXP_REPLACE(%s,'[^0-9]','','g')
                    LIMIT 1
                    """,
                    (obligacion_id, identificacion),
                )
                deudor_reportante = cur.fetchone()
                if not deudor_reportante:
                    raise HTTPException(status_code=409, detail="La identificación no corresponde a un deudor de la obligación")
                if not nombre_deudor:
                    nombre_deudor = str(deudor_reportante["nombre"] or deudor["nombre"] or "")
                if not telefono:
                    telefono = str(deudor_reportante["telefono"] or deudor["telefono"] or "")

                cur.execute(
                    """
                    INSERT INTO acuerdos_pago (
                        inmueble_id,obligacion_id,identificacion_deudor,nombre_deudor,telefono,
                        valor_acordado,numero_cuotas,cuota_actual,fecha_compromiso,
                        estado,origen,observaciones
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDIENTE','ROBOT_IA',%s)
                    RETURNING id
                    """,
                    (
                        inmueble_id,obligacion_id,identificacion,nombre_deudor,telefono,
                        valor,numero_cuotas,cuota_actual,fecha_compromiso,observaciones,
                    ),
                )
                acuerdo_id = int(cur.fetchone()["id"])

                cur.execute(
                    """
                    INSERT INTO gestiones_crm (
                        radicado_interno,inmueble_id,obligacion_id,identificacion_deudor,
                        tipo_contacto,resumen,promesa_pago_fecha,usuario,estado
                    )
                    VALUES (
                        (SELECT po.radicado_interno
                           FROM proceso_obligaciones po
                          WHERE po.obligacion_id=%s
                          ORDER BY po.es_principal DESC,po.id
                          LIMIT 1),
                        %s,%s,%s,'WhatsApp IA - Acuerdo',%s,%s,'Bot Claude','ACTIVO'
                    )
                    """,
                    (
                        obligacion_id,inmueble_id,obligacion_id,identificacion,
                        f"[ACUERDO DE PAGO #{acuerdo_id}] Cuota {cuota_actual}/{numero_cuotas} por ${valor:,.0f} para el {fecha_compromiso}. {observaciones}",
                        fecha_compromiso,
                    ),
                )

                cur.execute(
                    """
                    SELECT po.radicado_interno
                    FROM proceso_obligaciones po
                    WHERE po.obligacion_id=%s
                    ORDER BY po.es_principal DESC,po.id
                    LIMIT 1
                    """,
                    (obligacion_id,),
                )
                p_row = cur.fetchone()
                radicado = p_row["radicado_interno"] if p_row and p_row["radicado_interno"] else "ACUERDO-PAGO"

                cur.execute(
                    """
                    INSERT INTO vencimientos (
                        radicado_interno,obligacion_id,titulo,fecha_vencimiento,observaciones,
                        completado,tipo,valor,inmueble_id,anulado,categoria
                    )
                    VALUES (%s,%s,%s,%s,%s,FALSE,'ACUERDO_PAGO',%s,%s,FALSE,'OTROS')
                    """,
                    (
                        radicado,obligacion_id,
                        f"Cobro Cuota #{cuota_actual} ({nombre_deudor or identificacion}) - ${valor:,.0f}",
                        fecha_compromiso,
                        f"Acuerdo #{acuerdo_id}. Tel: {telefono}. Obs: {observaciones}",
                        valor,inmueble_id,
                    ),
                )

        return JSONResponse({
            "status":"success",
            "mensaje":"Acuerdo de pago registrado y vinculado a la obligación",
            "acuerdo_id":acuerdo_id,
            "obligacion_id":obligacion_id,
            "datos":{
                "identificacion":identificacion,
                "nombre_deudor":nombre_deudor,
                "fecha_compromiso":str(fecha_compromiso),
                "valor_acordado":valor,
                "estado":"PENDIENTE",
            }
        })
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[BOT ACUERDO] Error registrando acuerdo: {exc!r}",flush=True)
        raise HTTPException(status_code=500,detail="Error interno al registrar acuerdo en ERP")
    finally:
        conn.release()

@router.get("/api/bot/recordatorios/pendientes")
def consultar_recordatorios_pendientes(request: Request, dias_anticipacion: int = 1):
    """Consulta acuerdos de pago próximos para disparar recordatorios programados."""
    _require_api_key(request)
    import db
    from psycopg2.extras import RealDictCursor
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            hoy = date.today()
            limite = hoy + timedelta(days=dias_anticipacion)
            cur.execute("""
                SELECT a.id, a.inmueble_id, a.identificacion_deudor, a.nombre_deudor, a.telefono,
                       a.valor_acordado, a.fecha_compromiso, a.cuota_actual, a.numero_cuotas,
                       a.recordatorio_previo_enviado, a.recordatorio_dia_enviado, a.recordatorio_mora_enviado,
                       i.conjunto_residencial, i.torre_apto
                FROM acuerdos_pago a
                LEFT JOIN inmuebles_ph i ON a.inmueble_id = i.id
                WHERE a.estado = 'PENDIENTE'
                  AND a.fecha_compromiso BETWEEN %s AND %s
                ORDER BY a.fecha_compromiso ASC
            """, (hoy, limite))
            acuerdos = [dict(r) for r in cur.fetchall()]
            for a in acuerdos:
                if a.get("valor_acordado") is not None:
                    a["valor_acordado"] = float(a["valor_acordado"])
                if a.get("fecha_compromiso"):
                    a["fecha_compromiso"] = str(a["fecha_compromiso"])
        return JSONResponse({"status": "success", "total": len(acuerdos), "acuerdos": acuerdos})
    finally:
        conn.release()
