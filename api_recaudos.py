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
    Endpoint M2M invocado por el agente de WhatsApp al detectar un comprobante bancario.
    Asienta el abono contablemente, recalcula saldos y activa la alerta humana si extingue la deuda.
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
        raise HTTPException(status_code=422, detail="obligacion_id/inmueble_id y valor (> 0) son obligatorios")
        
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
                # Resolver la obligación canónica. Este endpoint sigue limitado a PH
                # porque su asiento histórico todavía usa expensas_ph.
                if obligacion_id:
                    cur.execute(
                        """
                        SELECT o.id,o.inmueble_id,o.fuente_saldo,
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
                    if not ob:
                        raise HTTPException(status_code=404, detail="Obligación no encontrada")
                    if str(ob["fuente_saldo"] or "").upper() != "EXPENSAS_PH":
                        raise HTTPException(status_code=409, detail="Este endpoint de recaudo está configurado para obligaciones PH")
                    inmueble_id = int(ob["inmueble_id"]) if ob["inmueble_id"] else None
                elif inmueble_id:
                    cur.execute(
                        """
                        SELECT o.id,o.inmueble_id,o.fuente_saldo,
                               c.identificacion,c.nombre,c.telefono,
                               i.conjunto_residencial,i.torre_apto
                        FROM obligaciones o
                        JOIN contactos c ON c.id=o.deudor_contacto_id
                        LEFT JOIN inmuebles_ph i ON i.id=o.inmueble_id
                        WHERE o.inmueble_id=%s
                          AND UPPER(COALESCE(o.fuente_saldo,''))='EXPENSAS_PH'
                          AND UPPER(COALESCE(o.estado,'ACTIVA')) NOT IN ('CANCELADA','ANULADA')
                        ORDER BY o.id DESC
                        LIMIT 1
                        """,
                        (inmueble_id,),
                    )
                    ob = cur.fetchone()
                    if ob:
                        obligacion_id = int(ob["id"])
                if not obligacion_id or not inmueble_id or not ob:
                    raise HTTPException(status_code=404, detail="No existe una obligación PH activa para la cuenta indicada")

                # 1. Obtener información del inmueble y deudor
                inm = {
                    "id": inmueble_id,
                    "conjunto_residencial": ob["conjunto_residencial"],
                    "torre_apto": ob["torre_apto"],
                    "identificacion": ob["identificacion"],
                    "nombre": ob["nombre"],
                    "telefono": ob["telefono"],
                }

                # 2. Liquidación previa para calcular imputación exacta
                _, resumen_prev, _ = liquidador.motor_calculo_judicial(
                    inmueble_id, "usura", 2.5, 23.8, 0.0, fecha_pago
                )
                saldo_int = float(resumen_prev.get("intereses", 0.0))
                saldo_cap = float(resumen_prev.get("capital", 0.0))
                honorarios_pct = float(resumen_prev.get("honorarios_pct", 23.8))
                
                imputacion = recaudos_service.calcular_imputacion_abono(
                    saldo_int, saldo_cap, honorarios_pct, valor_abono
                )

                # 3. Asentar contablemente en expensas_ph (impacta de inmediato el liquidador)
                cur.execute("""
                    INSERT INTO expensas_ph (
                        inmueble_id, concepto, periodo_mes, periodo_anio,
                        valor_capital, fecha_vencimiento, estado
                    ) VALUES (%s, 'Abono', %s, %s, %s, %s, 'Pagado')
                    RETURNING id;
                """, (inmueble_id, fecha_pago.month, fecha_pago.year, valor_abono, fecha_pago))
                
                # 4. Registrar discriminación de fondos por cuenta en recaudos_contabilidad
                cur.execute("""
                    INSERT INTO recaudos_contabilidad (
                        inmueble_id, obligacion_id, identificacion_deudor, nombre_deudor, fecha_pago,
                        valor_total, abono_intereses, abono_capital, honorarios_cobro,
                        banco_origen, referencia_transaccion, soporte_url, estado_conciliacion
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDIENTE_CONCILIACION')
                    RETURNING id;
                """, (
                    inmueble_id, obligacion_id, inm["identificacion"], inm["nombre"], fecha_pago,
                    valor_abono, imputacion["abono_intereses"], imputacion["abono_capital"],
                    imputacion["honorarios_cobro"], banco, referencia, soporte_url
                ))
                recaudo_id = cur.fetchone()["id"]

                # 5. Asentar en el CRM del ERP
                cur.execute("""
                    INSERT INTO gestiones_crm (
                        inmueble_id, obligacion_id, identificacion_deudor, tipo_contacto,
                        resumen, usuario, estado
                    ) VALUES (%s, %s, %s, 'WhatsApp IA - Comprobante', %s, 'Bot Vision', 'ACTIVO');
                """, (
                    inmueble_id, obligacion_id, inm["identificacion"],
                    f"Abono reportado por IA: ${valor_abono:,.0f} ({banco} Ref: {referencia}). Imputado: Capital ${imputacion['abono_capital']:,.0f}, Intereses ${imputacion['abono_intereses']:,.0f}."
                ))

                # 6. Actualizar acuerdo de pago si existe
                cur.execute("""
                    SELECT id, cuota_actual, numero_cuotas 
                    FROM acuerdos_pago 
                    WHERE obligacion_id = %s AND estado = 'PENDIENTE'
                    ORDER BY id DESC LIMIT 1;
                """, (inmueble_id,))
                ac = cur.fetchone()
                if ac:
                    if ac["cuota_actual"] >= ac["numero_cuotas"]:
                        cur.execute("UPDATE acuerdos_pago SET estado = 'CUMPLIDO' WHERE id = %s;", (ac["id"],))
                    else:
                        cur.execute("UPDATE acuerdos_pago SET cuota_actual = cuota_actual + 1 WHERE id = %s;", (ac["id"],))

        # 7. Re-liquidar deuda post-abono
        _, resumen_post, _ = liquidador.motor_calculo_judicial(
            inmueble_id, "usura", 2.5, 23.8, 0.0, date.today()
        )
        saldo_restante = float(resumen_post.get("gran_total", 0.0))
        
        solicitud_creada = False
        if saldo_restante <= 100.0:
            # Crear solicitud de paz y salvo sujeta a validación humana
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO solicitudes_paz_y_salvo (
                            inmueble_id, obligacion_id, identificacion_deudor, nombre_deudor,
                            conjunto_residencial, torre_apto, recaudo_id, estado
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'PENDIENTE_REVISION');
                    """, (inmueble_id, inm["identificacion"], inm["nombre"], inm["conjunto_residencial"], inm["torre_apto"], recaudo_id))
            solicitud_creada = True

        return JSONResponse({
            "status": "success",
            "mensaje": "Abono asentado exitosamente en contabilidad y liquidador",
            "recaudo_id": recaudo_id,
            "valor_abono": valor_abono,
            "imputacion": imputacion,
            "saldo_restante": max(0.0, saldo_restante),
            "requiere_aprobacion_paz_y_salvo": solicitud_creada
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

