"""Compatibilidad SMS sobre el motor único de saldo por obligación."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from obligacion_saldo_service import (
    calcular_saldo_obligacion,
    enriquecer_candidatos as enriquecer_candidatos_obligaciones,
)

TZ_COLOMBIA = ZoneInfo("America/Bogota")


def ahora_colombia() -> datetime:
    return datetime.now(TZ_COLOMBIA)


def calcular_saldo_ph(
    inmueble_id: Optional[int],
    fecha_corte: Optional[date] = None,
) -> Dict[str, Any]:
    """Compatibilidad temporal: resuelve la obligación PH por inmueble."""
    if not inmueble_id:
        return {
            "saldo_total": None,
            "saldo_verificado": False,
            "saldo_fuente": "SIN_LIQUIDADOR",
            "saldo_calculado_en": ahora_colombia(),
        }

    conn = None
    try:
        import db

        conn = db.get_connection()
        with conn.cursor() as cur:
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
                (int(inmueble_id),),
            )
            row = cur.fetchone()
            if not row:
                return {
                    "saldo_total": None,
                    "saldo_verificado": False,
                    "saldo_fuente": "SIN_OBLIGACION_PH",
                    "saldo_calculado_en": ahora_colombia(),
                }
            oid = row[0] if not isinstance(row, dict) else row["id"]

        return calcular_saldo_obligacion(
            int(oid),
            fecha_corte=fecha_corte,
        )
    except Exception as exc:
        return {
            "saldo_total": None,
            "saldo_verificado": False,
            "saldo_fuente": "ERROR_SALDO",
            "saldo_calculado_en": ahora_colombia(),
            "saldo_error": str(exc)[:200],
        }
    finally:
        if conn is not None:
            conn.release()


def enriquecer_candidatos(candidatos):
    return enriquecer_candidatos_obligaciones(candidatos)


def _normalizar_nombre(nombre: Any) -> str:
    return str(nombre or "Propietario").strip().title()


def actualizar_item_cola(cur, item: Dict[str, Any]) -> Dict[str, Any]:
    """Recalcula el saldo de la obligación justo antes de entregar el SMS."""
    cur.execute(
        """
        SELECT
            id,
            inmueble_id,
            obligacion_id,
            contacto_id,
            identificacion,
            nombre,
            conjunto_residencial,
            torre_apto,
            telefono,
            mensaje_texto,
            tipo_campana
        FROM sms_cola_envios
        WHERE id=%s
        FOR UPDATE
        """,
        (int(item["id"]),),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"No existe sms_cola_envios.id={item['id']}")

    if isinstance(row, dict):
        data = dict(row)
    else:
        data = {
            "id": row[0],
            "inmueble_id": row[1],
            "obligacion_id": row[2],
            "contacto_id": row[3],
            "identificacion": row[4],
            "nombre": row[5],
            "conjunto_residencial": row[6],
            "torre_apto": row[7],
            "telefono": row[8],
            "mensaje_texto": row[9],
            "tipo_campana": row[10],
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
                saldo.get("saldo_fuente") or "SIN_OBLIGACION",
                "Envío bloqueado: no existe saldo verificable para la obligación.",
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
