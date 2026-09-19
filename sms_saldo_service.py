"""Fuente única de saldo para SMS.

Para propiedad horizontal la cifra comunicable se obtiene del mismo motor
que utiliza la vista /liquidador: liquidador.motor_calculo_judicial().
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import liquidador
from obligacion_saldo_service import calcular_saldo_obligacion

TZ_COLOMBIA = ZoneInfo("America/Bogota")
TIPO_TASA_SMS = "Máxima Legal"
TASA_FIJA_SMS = 2.5
HONORARIOS_PCT_SMS = 23.8
GASTOS_SMS = 0.0


def ahora_colombia() -> datetime:
    return datetime.now(TZ_COLOMBIA)


def calcular_saldo_ph(inmueble_id: Optional[int], fecha_corte: Optional[date] = None) -> Dict[str, Any]:
    if not inmueble_id:
        return {
            "saldo_total": None,
            "saldo_verificado": False,
            "saldo_fuente": "SIN_OBLIGACION",
            "saldo_calculado_en": ahora_colombia(),
        }

    # Compatibilidad: resolver primero la obligación PH activa del inmueble.
    import db
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
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
                (int(inmueble_id),),
            )
            row = cur.fetchone()
            obligacion_id = int(row[0]) if row else None
    finally:
        conn.release()

    return calcular_saldo_obligacion(obligacion_id, fecha_corte=fecha_corte)


def enriquecer_candidatos(candidatos):
    cache: Dict[int, Dict[str, Any]] = {}
    salida = []

    for candidato in candidatos:
        item = dict(candidato)
        obligation_id = item.get("obligacion_id")

        if obligation_id:
            oid = int(obligation_id)
            if oid not in cache:
                cache[oid] = calcular_saldo_obligacion(oid)
            item.update(cache[oid])
        else:
            item.update({
                "saldo_total": None,
                "saldo_verificado": False,
                "saldo_fuente": "SIN_OBLIGACION",
                "saldo_calculado_en": ahora_colombia(),
            })

        salida.append(item)

    return salida


def _normalizar_nombre(nombre: Any) -> str:
    return str(nombre or "Propietario").strip().title()


def actualizar_item_cola(cur, item: Dict[str, Any]) -> Dict[str, Any]:
    """Recalcula y actualiza el SMS justo antes de entregarlo al worker."""
    cur.execute(
        """
        SELECT id, inmueble_id, contacto_id, obligacion_id, identificacion, nombre,
               conjunto_residencial, torre_apto, telefono,
               mensaje_texto, tipo_campana
        FROM sms_cola_envios
        WHERE id=%s
        FOR UPDATE
        """,
        (int(item["id"]),),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"No existe sms_cola_envios.id={item['id']}")

    data = dict(row) if isinstance(row, dict) else {
        "id": row[0], "inmueble_id": row[1], "contacto_id": row[2], "obligacion_id": row[3],
        "identificacion": row[4], "nombre": row[5],
        "conjunto_residencial": row[6], "torre_apto": row[7],
        "telefono": row[8], "mensaje_texto": row[9], "tipo_campana": row[10],
    }

    saldo = calcular_saldo_obligacion(
        int(data["obligacion_id"]) if data.get("obligacion_id") else None
    )

    if not saldo.get("saldo_verificado"):
        cur.execute(
            """
            UPDATE sms_cola_envios
            SET saldo_verificado=FALSE,
                saldo_fuente=%s,
                saldo_calculado_en=CURRENT_TIMESTAMP,
                error_detalle=%s
            WHERE id=%s
            """,
            (
                saldo.get("saldo_fuente") or "SIN_LIQUIDADOR",
                "Envío bloqueado: no existe liquidación verificable para esta cuenta.",
                data["id"],
            ),
        )
        data.update(saldo)
        data["bloqueado_envio"] = True
        return data

    cur.execute(
        "SELECT cuerpo_template FROM sms_plantillas WHERE tipo=%s LIMIT 1",
        (data.get("tipo_campana"),),
    )
    tpl = cur.fetchone()
    cuerpo = tpl["cuerpo_template"] if isinstance(tpl, dict) and tpl else (
        "{nombre}, registra una deuda de COP {saldo} en {conjunto} {unidad}. "
        "WhatsApp: {telefono_wa}"
    )

    saldo_formato = f"{int(round(float(saldo['saldo_total']))):,}".replace(",", ".")
    mensaje = cuerpo.format(
        nombre=_normalizar_nombre(data.get("nombre")),
        conjunto=(data.get("conjunto_residencial") or "Copropiedad").strip(),
        unidad=(data.get("torre_apto") or "").strip(),
        saldo=saldo_formato,
        telefono_wa=_get_whatsapp_agent(),
    )

    cur.execute(
        """
        UPDATE sms_cola_envios
        SET saldo_calculado=%s,
            saldo_fuente=%s,
            saldo_verificado=TRUE,
            saldo_calculado_en=CURRENT_TIMESTAMP,
            mensaje_texto=%s,
            error_detalle=NULL
        WHERE id=%s
        """,
        (
            saldo["saldo_total"],
            saldo["saldo_fuente"],
            mensaje,
            data["id"],
        ),
    )

    data.update(saldo)
    data["mensaje_texto"] = mensaje
    data["bloqueado_envio"] = False
    return data


def _get_whatsapp_agent() -> str:
    try:
        import sms_router
        return f"+{sms_router.WHATSAPP_AGENTE}"
    except Exception:
        return ""
