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
import obligacion_saldo_service

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
    """Registra un abono sobre la obligación canónica."""
    _require_bot_auth(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Cuerpo de petición inválido (JSON esperado)")

    try:
        valor_abono = float(data.get("valor"))
        if valor_abono <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="valor (> 0) es obligatorio")

    obligacion_id_raw = data.get("obligacion_id")
    inmueble_id_raw = data.get("inmueble_id")
    try:
        obligacion_id = int(obligacion_id_raw) if obligacion_id_raw else None
    except (TypeError, ValueError):
        obligacion_id = None
    try:
        inmueble_id = int(inmueble_id_raw) if inmueble_id_raw else None
    except (TypeError, ValueError):
        inmueble_id = None

    fecha_pago_raw = str(data.get("fecha_pago") or date.today().isoformat())
    try:
        fecha_pago = datetime.strptime(fecha_pago_raw, "%Y-%m-%d").date()
    except ValueError:
        fecha_pago = date.today()

    banco = str(data.get("banco") or "Transferencia bancaria").strip()
    referencia = str(data.get("referencia") or "Comprobante IA").strip()
    soporte_url = str(data.get("soporte_url") or "").strip()

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if not obligacion_id and inmueble_id:
                    cur.execute(
                        """
                        SELECT o.id
                        FROM obligaciones o
                        JOIN tipos_obligacion tob ON tob.id=o.tipo_obligacion_id
                        WHERE o.inmueble_id=%s
                          AND tob.codigo='CUOTAS_ADMINISTRACION'
                          AND COALESCE(o.estado,'ACTIVA') NOT IN ('CANCELADA','ANULADA')
                        ORDER BY o.id DESC
                        LIMIT 1
                        """,
                        (inmueble_id,),
                    )
                    row = cur.fetchone()
                    obligacion_id = int(row["id"]) if row else None

                if not obligacion_id:
                    raise HTTPException(status_code=422, detail="obligacion_id es obligatorio (inmueble_id solo sirve para compatibilidad PH)")

                cur.execute(
                    """
                    SELECT
                        o.id, o.inmueble_id, o.fuente_saldo, o.identificacion_deudor,
                        tob.codigo AS tipo_obligacion,
                        c.identificacion AS identificacion_contacto, c.nombre AS nombre_contacto,
                        c.telefono AS telefono_contacto,
                        i.conjunto_residencial, i.torre_apto
                    FROM obligaciones o
                    JOIN tipos_obligacion tob ON tob.id=o.tipo_obligacion_id
                    LEFT JOIN contactos c ON c.id=o.deudor_contacto_id
                    LEFT JOIN inmuebles_ph i ON i.id=o.inmueble_id
                    WHERE o.id=%s
                    LIMIT 1
                    """,
                    (obligacion_id,),
                )
                obligacion = cur.fetchone()
                if not obligacion:
                    raise HTTPException(status_code=404, detail="Obligación no encontrada")

                identificacion = obligacion["identificacion_contacto"] or obligacion["identificacion_deudor"] or ""
                nombre = obligacion["nombre_contacto"] or ""
                inmueble_id = obligacion["inmueble_id"] or inmueble_id
                fuente = str(obligacion["fuente_saldo"] or "").upper()
                imputacion = {"abono_intereses": 0.0, "abono_capital": valor_abono, "honorarios_cobro": 0.0}

                if fuente == "EXPENSAS_PH" or obligacion["tipo_obligacion"] == "CUOTAS_ADMINISTRACION":
                    if not inmueble_id:
                        raise HTTPException(status_code=422, detail="La obligación PH no tiene inmueble asociado")
                    _, resumen_prev, _ = liquidador.motor_calculo_judicial(
                        int(inmueble_id), "usura", 2.5, 23.8, 0.0, fecha_pago
                    )
                    imputacion = recaudos_service.calcular_imputacion_abono(
                        float(resumen_prev.get("intereses", 0.0)),
                        float(resumen_prev.get("capital", 0.0)),
                        float(resumen_prev.get("honorarios_pct", 23.8)),
                        valor_abono,
                    )
                    cur.execute(
                        """
                        INSERT INTO expensas_ph (
                            inmueble_id, concepto, periodo_mes, periodo_anio,
                            valor_capital, fecha_vencimiento, estado, obligation_id
                        ) VALUES (%s, 'Abono', %s, %s, %s, %s, 'Pagado', %s)
                        RETURNING id
                        """,
                        (inmueble_id, fecha_pago.month, fecha_pago.year, valor_abono, fecha_pago, obligacion_id),
                    )
                elif fuente == "OBLIGACION":
                    pass
                else:
                    raise HTTPException(status_code=422, detail="La fuente financiera de la obligación no está habilitada para recaudos")

                cur.execute(
                    """
                    INSERT INTO recaudos_contabilidad (
                        inmueble_id, obligacion_id, identificacion_deudor,
                        nombre_deudor, fecha_pago, valor_total,
                        abono_intereses, abono_capital, honorarios_cobro,
                        banco_origen, referencia_transaccion, soporte_url,
                        estado_conciliacion
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDIENTE_CONCILIACION')
                    RETURNING id
                    """,
                    (inmueble_id, obligacion_id, identificacion, nombre, fecha_pago, valor_abono,
                     imputacion["abono_intereses"], imputacion["abono_capital"], imputacion["honorarios_cobro"],
                     banco, referencia, soporte_url),
                )
                recaudo_id = int(cur.fetchone()["id"])

                if fuente == "OBLIGACION":
                    cur.execute(
                        """
                        INSERT INTO obligacion_movimientos (
                            obligacion_id, tipo, concepto, valor, fecha, observaciones, recaudo_id
                        ) VALUES (%s,'ABONO','Recaudo reportado por agente',%s,%s,%s,%s)
                        """,
                        (obligacion_id, valor_abono, fecha_pago, f"{banco} · Ref. {referencia}", recaudo_id),
                    )

                cur.execute(
                    """
                    INSERT INTO gestiones_crm (
                        inmueble_id, obligacion_id, identificacion_deudor,
                        tipo_contacto, resumen, usuario, estado
                    ) VALUES (%s,%s,%s,'WhatsApp IA - Comprobante',%s,'Bot Vision','ACTIVO')
                    """,
                    (inmueble_id, obligacion_id, identificacion,
                     f"Abono reportado por IA: ${valor_abono:,.0f} ({banco} Ref: {referencia}). Imputado: Capital ${imputacion['abono_capital']:,.0f}, Intereses ${imputacion['abono_intereses']:,.0f}."),
                )

        saldo_info = obligacion_saldo_service.calcular_saldo_obligacion(obligacion_id, fecha_corte=date.today())
        saldo_restante = float(saldo_info["saldo_total"]) if saldo_info.get("saldo_verificado") else None

        solicitud_creada = False
        if saldo_restante is not None and saldo_restante <= 100.0:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO solicitudes_paz_y_salvo (
                            inmueble_id, obligacion_id, identificacion_deudor, nombre_deudor,
                            conjunto_residencial, torre_apto, recaudo_id, estado
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,'PENDIENTE_REVISION')
                        """,
                        (inmueble_id, obligacion_id, identificacion, nombre,
                         obligacion["conjunto_residencial"], obligacion["torre_apto"], recaudo_id),
                    )
            solicitud_creada = True

        return JSONResponse({
            "status": "success",
            "mensaje": "Abono asentado exitosamente",
            "recaudo_id": recaudo_id,
            "obligacion_id": obligacion_id,
            "valor_abono": valor_abono,
            "imputacion": imputacion,
            "saldo_restante": saldo_restante,
            "saldo_verificado": bool(saldo_info.get("saldo_verificado")),
            "requiere_aprobacion_paz_y_salvo": solicitud_creada,
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
                        inmueble_id, identificacion_deudor, tipo_contacto, resumen, usuario, estado
                    ) VALUES (%s, %s, 'Paz y Salvo Aprobado', %s, %s, 'FINALIZADO');
                """, (sol["inmueble_id"], sol["identificacion_deudor"], f"Paz y Salvo APROBADO por {usuario_aprobador}. Código verificación: {codigo_verif}", usuario_aprobador))

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
                SELECT s.id, s.inmueble_id, s.identificacion_deudor, s.nombre_deudor,
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

