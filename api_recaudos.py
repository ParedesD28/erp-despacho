"""
Router FastAPI para integración de Recaudos por IA, Conciliación de Pagos,
Aprobación Humana de Paz y Salvo y Reportes Contables.
"""
import os
import secrets
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, FileResponse
from psycopg2.extras import RealDictCursor

import db
import liquidador
import recaudos_service

router = APIRouter(prefix="/api/recaudos", tags=["Recaudos y Paz y Salvo"])

BOT_API_KEY = (os.getenv("LIQUIDADOR_API_KEY") or "").strip().strip('"').strip("'")
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

def _require_bot_auth(request: Request):
    expected = BOT_API_KEY
    supplied = request.headers.get("X-API-Key")
    if not expected:
        raise HTTPException(status_code=503, detail="LIQUIDADOR_API_KEY no configurada")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="No autorizado")

@router.post("/bot/abono")
async def reportar_abono_agente(request: Request):
    """
    Registra un recaudo contra la obligación canónica.

    - EXPENSAS_PH conserva su asiento histórico en expensas_ph para que el
      liquidador PH siga siendo la fuente de saldo.
    - Las demás obligaciones registran un movimiento ABONO en
      obligacion_movimientos.
    Nunca modifica procesos.pretensiones.
    """
    _require_bot_auth(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Cuerpo de petición inválido (JSON esperado)")

    try:
        obligacion_id = int(data.get("obligacion_id")) if data.get("obligacion_id") else None
        inmueble_id = int(data.get("inmueble_id")) if data.get("inmueble_id") else None
        valor_abono = float(data.get("valor"))
        if valor_abono <= 0 or (not obligacion_id and not inmueble_id):
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="obligacion_id/inmueble_id y valor (> 0) son obligatorios",
        )

    fecha_pago_raw = str(data.get("fecha_pago") or date.today().isoformat())
    try:
        fecha_pago = datetime.strptime(fecha_pago_raw, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="fecha_pago debe tener formato YYYY-MM-DD")

    identificacion_reportante = str(
        data.get("identificacion_deudor")
        or data.get("identificacion")
        or data.get("cedula")
        or ""
    ).strip()
    banco = str(data.get("banco") or "Transferencia bancaria").strip()
    referencia = str(data.get("referencia") or "Comprobante IA").strip()
    soporte_url = str(data.get("soporte_url") or "").strip()

    import obligacion_saldo_service

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if obligacion_id:
                    cur.execute(
                        """
                        SELECT o.id,o.inmueble_id,o.fuente_saldo,o.estado,
                               c.identificacion,c.nombre,c.telefono,
                               i.conjunto_residencial,i.torre_apto
                        FROM obligaciones o
                        JOIN contactos c ON c.id=o.deudor_contacto_id
                        LEFT JOIN inmuebles_ph i ON i.id=o.inmueble_id
                        WHERE o.id=%s
                        LIMIT 1
                        """,
                        (obligacion_id,),
                    )
                    ob = cur.fetchone()
                else:
                    cur.execute(
                        """
                        SELECT o.id,o.inmueble_id,o.fuente_saldo,o.estado
                        FROM obligaciones o
                        WHERE o.inmueble_id=%s
                          AND UPPER(COALESCE(o.estado,'ACTIVA')) NOT IN ('CANCELADA','ANULADA')
                        ORDER BY CASE WHEN UPPER(COALESCE(o.fuente_saldo,''))='EXPENSAS_PH' THEN 0 ELSE 1 END,
                                 o.id DESC
                        LIMIT 2
                        """,
                        (inmueble_id,),
                    )
                    candidates = cur.fetchall()
                    if not candidates:
                        ob = None
                    elif len(candidates) > 1:
                        raise HTTPException(
                            status_code=409,
                            detail="Debe indicar obligacion_id cuando existen varias obligaciones activas para el inmueble",
                        )
                    else:
                        ob = candidates[0]

                    if ob:
                        cur.execute(
                            """
                            SELECT c.identificacion,c.nombre,c.telefono,
                                   i.conjunto_residencial,i.torre_apto
                            FROM obligaciones o
                            JOIN contactos c ON c.id=o.deudor_contacto_id
                            LEFT JOIN inmuebles_ph i ON i.id=o.inmueble_id
                            WHERE o.id=%s
                            LIMIT 1
                            """,
                            (ob["id"],),
                        )
                        ob = dict(ob) | dict(cur.fetchone() or {})

                ob = dict(ob) if ob else None
                if not ob:
                    raise HTTPException(status_code=404, detail="Obligación no encontrada")

                obligacion_id = int(ob["id"])
                inmueble_id = int(ob["inmueble_id"]) if ob["inmueble_id"] else inmueble_id
                fuente = str(ob["fuente_saldo"] or "").upper()

                if str(ob["estado"] or "ACTIVA").upper() in {"CANCELADA", "ANULADA"}:
                    raise HTTPException(status_code=409, detail="La obligación no está activa")

                if identificacion_reportante:
                    cur.execute(
                        """
                        SELECT 1
                        FROM obligacion_partes op
                        JOIN contactos c ON c.id=op.contacto_id
                        WHERE op.obligacion_id=%s
                          AND op.rol='DEUDOR'
                          AND REGEXP_REPLACE(COALESCE(c.identificacion::text,''),'[^0-9]','','g')
                              = REGEXP_REPLACE(%s,'[^0-9]','','g')
                        LIMIT 1
                        """,
                        (obligacion_id,identificacion_reportante),
                    )
                    if not cur.fetchone():
                        raise HTTPException(
                            status_code=409,
                            detail="La identificación reportante no corresponde a un deudor de la obligación",
                        )

                inm = {
                    "id": inmueble_id,
                    "conjunto_residencial": ob["conjunto_residencial"] or "Obligación",
                    "torre_apto": ob["torre_apto"] or "",
                    "identificacion": ob["identificacion"],
                    "nombre": ob["nombre"],
                    "telefono": ob["telefono"],
                }

                if fuente == "EXPENSAS_PH":
                    if not inmueble_id:
                        raise HTTPException(status_code=409, detail="La obligación PH no tiene inmueble")

                    _, resumen_prev, _ = liquidador.motor_calculo_judicial(
                        inmueble_id, "usura", 2.5, 23.8, 0.0, fecha_pago,
                        obligacion_id=obligacion_id,
                    )
                    saldo_int = float(resumen_prev.get("intereses", 0.0))
                    saldo_cap = float(resumen_prev.get("capital", 0.0))
                    honorarios_pct = float(resumen_prev.get("honorarios_pct", 23.8))
                    imputacion = recaudos_service.calcular_imputacion_abono(
                        saldo_int, saldo_cap, honorarios_pct, valor_abono
                    )

                    cur.execute(
                        """
                        INSERT INTO expensas_ph (
                            inmueble_id,concepto,periodo_mes,periodo_anio,
                            valor_capital,fecha_vencimiento,estado,obligation_id
                        ) VALUES (%s,'Abono',%s,%s,%s,%s,'Pagado',%s)
                        RETURNING id
                        """,
                        (
                            inmueble_id,fecha_pago.month,fecha_pago.year,
                            valor_abono,fecha_pago,obligacion_id,
                        ),
                    )
                    recaudo_params = (
                        inmueble_id,obligacion_id,inm["identificacion"],inm["nombre"],fecha_pago,
                        valor_abono,imputacion["abono_intereses"],imputacion["abono_capital"],
                        imputacion["honorarios_cobro"],banco,referencia,soporte_url,
                    )
                    cur.execute(
                        """
                        INSERT INTO recaudos_contabilidad (
                            inmueble_id,obligacion_id,identificacion_deudor,nombre_deudor,fecha_pago,
                            valor_total,abono_intereses,abono_capital,honorarios_cobro,
                            banco_origen,referencia_transaccion,soporte_url,estado_conciliacion
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDIENTE_CONCILIACION')
                        RETURNING id
                        """,
                        recaudo_params,
                    )
                    recaudo_id = int(cur.fetchone()["id"])
                    saldo_post = None
                    imputacion_resultado = imputacion
                else:
                    saldo_pre = obligacion_saldo_service.calcular_saldo_obligacion(
                        obligacion_id,fecha_corte=fecha_pago,conn=conn
                    )
                    if not saldo_pre.get("saldo_verificado"):
                        raise HTTPException(
                            status_code=409,
                            detail="No existe un saldo verificable para la obligación",
                        )
                    saldo_actual = float(saldo_pre.get("saldo_total") or 0)
                    if valor_abono > saldo_actual + 0.01:
                        raise HTTPException(
                            status_code=409,
                            detail="El abono excede el saldo verificable de la obligación",
                        )

                    cur.execute(
                        """
                        INSERT INTO recaudos_contabilidad (
                            inmueble_id,obligacion_id,identificacion_deudor,nombre_deudor,fecha_pago,
                            valor_total,abono_intereses,abono_capital,honorarios_cobro,
                            banco_origen,referencia_transaccion,soporte_url,estado_conciliacion
                        ) VALUES (%s,%s,%s,%s,%s,%s,0,%s,0,%s,%s,%s,'PENDIENTE_CONCILIACION')
                        RETURNING id
                        """,
                        (
                            inmueble_id,obligacion_id,inm["identificacion"],inm["nombre"],fecha_pago,
                            valor_abono,valor_abono,banco,referencia,soporte_url,
                        ),
                    )
                    recaudo_id = int(cur.fetchone()["id"])

                    cur.execute(
                        """
                        INSERT INTO obligacion_movimientos (
                            obligacion_id,tipo,concepto,valor,fecha,observaciones,recaudo_id
                        ) VALUES (%s,'ABONO','Recaudo recibido',%s,%s,%s,%s)
                        """,
                        (
                            obligacion_id,valor_abono,fecha_pago,
                            f"Banco: {banco}; referencia: {referencia}",
                            recaudo_id,
                        ),
                    )
                    saldo_post = obligacion_saldo_service.calcular_saldo_obligacion(
                        obligacion_id,fecha_corte=fecha_pago,conn=conn
                    )
                    imputacion_resultado = {
                        "abono_intereses": 0.0,
                        "abono_capital": valor_abono,
                        "honorarios_cobro": 0.0,
                    }

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
                radicado = p_row["radicado_interno"] if p_row else None

                cur.execute(
                    """
                    INSERT INTO gestiones_crm (
                        radicado_interno,inmueble_id,obligacion_id,identificacion_deudor,
                        tipo_contacto,resumen,usuario,estado
                    ) VALUES (%s,%s,%s,%s,'WhatsApp IA - Comprobante',%s,'Bot Vision','ACTIVO')
                    """,
                    (
                        radicado,inmueble_id,obligacion_id,inm["identificacion"],
                        f"Abono reportado por IA: ${valor_abono:,.0f} ({banco} Ref: {referencia}).",
                    ),
                )

                cur.execute(
                    """
                    SELECT id,cuota_actual,numero_cuotas
                    FROM acuerdos_pago
                    WHERE obligacion_id=%s AND estado='PENDIENTE'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (obligacion_id,),
                )
                ac = cur.fetchone()
                if ac:
                    if int(ac["cuota_actual"] or 0) >= int(ac["numero_cuotas"] or 0):
                        cur.execute(
                            "UPDATE acuerdos_pago SET estado='CUMPLIDO' WHERE id=%s",
                            (ac["id"],),
                        )
                    else:
                        cur.execute(
                            "UPDATE acuerdos_pago SET cuota_actual=cuota_actual+1 WHERE id=%s",
                            (ac["id"],),
                        )

                solicitud_creada = False
                saldo_restante = None
                if fuente == "EXPENSAS_PH":
                    _, resumen_post_ph, _ = liquidador.motor_calculo_judicial(
                        inmueble_id,"usura",2.5,23.8,0.0,date.today(),
                        obligacion_id=obligacion_id,
                    )
                    saldo_restante = float(resumen_post_ph.get("gran_total",0.0))
                    if saldo_restante <= 100.0:
                        cur.execute(
                            """
                            INSERT INTO solicitudes_paz_y_salvo (
                                inmueble_id,obligacion_id,identificacion_deudor,nombre_deudor,
                                conjunto_residencial,torre_apto,recaudo_id,estado
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,'PENDIENTE_REVISION')
                            """,
                            (
                                inmueble_id,obligacion_id,inm["identificacion"],inm["nombre"],
                                inm["conjunto_residencial"],inm["torre_apto"],recaudo_id,
                            ),
                        )
                        solicitud_creada = True
                    saldo_payload = max(0.0,saldo_restante)
                else:
                    saldo_payload = max(0.0,float((saldo_post or {}).get("saldo_total") or 0))

        return JSONResponse({
            "status":"success",
            "mensaje":"Abono asentado contra la obligación canónica",
            "recaudo_id":recaudo_id,
            "obligacion_id":obligacion_id,
            "valor_abono":valor_abono,
            "imputacion":imputacion_resultado,
            "saldo_restante":saldo_payload,
            "requiere_aprobacion_paz_y_salvo":solicitud_creada,
        })
    finally:
        conn.release()

@router.post("/solicitudes/{solicitud_id}/aprobar")
def aprobar_paz_y_salvo_humano(solicitud_id: int, request: Request, usuario_aprobador: str = "Abogado Administrador"):
    """
    Aprobación humana: verifica el pago en banco, genera el Certificado oficial en PDF y finaliza el trámite.
    """
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.*, r.id as rec_id
                    FROM solicitudes_paz_y_salvo s
                    LEFT JOIN recaudos_contabilidad r ON s.recaudo_id = r.id
                    WHERE s.id = %s;
                """, (solicitud_id,))
                sol = cur.fetchone()
                if not sol:
                    raise HTTPException(status_code=404, detail="Solicitud no encontrada")
                if sol["estado"] == "APROBADO":
                    return JSONResponse({"status": "warning", "mensaje": "Esta solicitud ya fue aprobada previamente", "url_pdf": sol["url_pdf_paz_y_salvo"]})

                # Generar PDF oficial firmado
                pdf_path, codigo_verif = recaudos_service.generar_pdf_paz_y_salvo(
                    sol["inmueble_id"],
                    sol["conjunto_residencial"],
                    sol["torre_apto"],
                    sol["nombre_deudor"],
                    sol["identificacion_deudor"],
                    date.today(),
                    abogado_firmante=usuario_aprobador
                )
                
                filename = os.path.basename(pdf_path)
                url_publica = f"{PUBLIC_BASE_URL}/static/pdfs/{filename}"

                # Actualizar estado de la solicitud y conciliación del recaudo
                cur.execute("""
                    UPDATE solicitudes_paz_y_salvo 
                    SET estado = 'APROBADO',
                        url_pdf_paz_y_salvo = %s,
                        codigo_verificacion = %s,
                        verificado_por = %s,
                        fecha_verificacion = NOW()
                    WHERE id = %s;
                """, (url_publica, codigo_verif, usuario_aprobador, solicitud_id))

                if sol["rec_id"]:
                    cur.execute("""
                        UPDATE recaudos_contabilidad
                        SET estado_conciliacion = 'CONCILIADO',
                            aprobado_por = %s,
                            fecha_aprobacion = NOW()
                        WHERE id = %s;
                    """, (usuario_aprobador, sol["rec_id"]))

                # Asentar en CRM
                cur.execute("""
                    INSERT INTO gestiones_crm (
                        inmueble_id, obligacion_id, identificacion_deudor,
                        tipo_contacto, resumen, usuario, estado
                    ) VALUES (%s, %s, %s, 'Paz y Salvo Aprobado', %s, %s, 'FINALIZADO');
                """, (
                    sol["inmueble_id"], sol["obligacion_id"], sol["identificacion_deudor"],
                    f"Paz y Salvo APROBADO por {usuario_aprobador}. Código verificación: {codigo_verif}",
                    usuario_aprobador,
                ))

        return JSONResponse({
            "status": "success",
            "mensaje": "Paz y Salvo aprobado y generado exitosamente",
            "codigo_verificacion": codigo_verif,
            "url_pdf": url_publica
        })
    finally:
        conn.release()

@router.get("/solicitudes/pendientes")
def listar_solicitudes_pendientes(request: Request):
    """Consulta la cola de paz y salvos pendientes para el panel de control del abogado."""
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.id, s.inmueble_id, s.obligacion_id, s.identificacion_deudor, s.nombre_deudor,
                       s.conjunto_residencial, s.torre_apto, s.fecha_solicitud,
                       r.valor_total, r.banco_origen, r.referencia_transaccion, r.soporte_url
                FROM solicitudes_paz_y_salvo s
                LEFT JOIN recaudos_contabilidad r ON s.recaudo_id = r.id
                WHERE s.estado = 'PENDIENTE_REVISION'
                ORDER BY s.id ASC;
            """)
            return JSONResponse({"status": "success", "solicitudes": [dict(r) for r in cur.fetchall()]})
    finally:
        conn.release()

@router.get("/informes/exportar-excel")
def descargar_informe_recaudo_excel(
    request: Request,
    conjunto: Optional[str] = None,
    fecha_inicio: Optional[str] = None,
    fecha_fin: Optional[str] = None
):
    """Descarga el Excel consolidado con la discriminación de dineros por cuenta."""
    f_ini = datetime.strptime(fecha_inicio, "%Y-%m-%d").date() if fecha_inicio else None
    f_fin = datetime.strptime(fecha_fin, "%Y-%m-%d").date() if fecha_fin else None
    
    conn = db.get_connection()
    try:
        ruta = recaudos_service.exportar_informe_contable_excel(conn, conjunto, f_ini, f_fin)
        return FileResponse(
            path=ruta,
            filename=os.path.basename(ruta),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    finally:
        conn.release()

