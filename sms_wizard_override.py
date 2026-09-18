"""Flujo de revisión en 3 pasos para campañas SMS.

Mantiene sms_router como motor de cola/worker y añade una confirmación explícita
antes de insertar mensajes en sms_cola_envios.
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

import db
from sms_router import normalizar_telefono, validar_horario_ley_2300


class WizardMensaje(BaseModel):
    contacto_id: int
    mensaje_texto: str = Field(..., min_length=1, max_length=2000)


class WizardConfirmacion(BaseModel):
    tipo_campana: str = Field(default="PREJUDICIAL")
    tipo_cartera: str = Field(default="")
    saldo_minimo: float = Field(default=0, ge=0)
    saldo_maximo: Optional[float] = Field(default=None, ge=0)
    mensajes: List[WizardMensaje] = Field(..., min_items=1, max_items=100)


def _mapa_candidatos(cur, data: WizardConfirmacion) -> dict[int, dict[str, Any]]:
    from sms_router import _candidatos_cartera
    ids = list(dict.fromkeys(int(x.contacto_id) for x in data.mensajes))
    rows = _candidatos_cartera(
        cur,
        data.tipo_cartera,
        data.saldo_minimo,
        data.saldo_maximo,
        ids,
    )
    return {
        int(row["contacto_id"] if isinstance(row, dict) else row[1]): dict(row)
        for row in rows
    }


def _contactos_bloqueados(cur, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    cur.execute(
        """
        SELECT DISTINCT contacto_id
        FROM sms_cola_envios
        WHERE contacto_id = ANY(%s)
          AND (
                (estado='ENVIADO' AND fecha_envio >= NOW() - INTERVAL '24 hours')
                OR estado IN ('PENDIENTE','EN_PROCESO')
          )
        """,
        (ids,),
    )
    return {
        int(row["contacto_id"] if isinstance(row, dict) else row[0])
        for row in cur.fetchall()
    }


def _conflicto_cola(cur, inmueble_id: Optional[int], telefono: str, tipo_campana: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM sms_cola_envios
        WHERE inmueble_id IS NOT DISTINCT FROM %s
          AND telefono=%s
          AND tipo_campana=%s
          AND estado IN ('PENDIENTE','EN_PROCESO')
        LIMIT 1
        """,
        (inmueble_id, telefono, tipo_campana),
    )
    return cur.fetchone() is not None


def _confirmar_cola(data: WizardConfirmacion):
    tipo_campana = data.tipo_campana.strip().upper()
    if tipo_campana not in {"PREJUDICIAL", "COBRO_JURIDICO", "MANDAMIENTO"}:
        raise HTTPException(status_code=400, detail="Tipo de campaña no válido.")

    habilitado, motivo = validar_horario_ley_2300()
    if not habilitado:
        raise HTTPException(status_code=403, detail=motivo)

    ids = list(dict.fromkeys(int(x.contacto_id) for x in data.mensajes))
    conn = db.get_connection()
    insertados: list[int] = []
    omitidos: list[dict[str, Any]] = []

    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id FROM contactos WHERE id = ANY(%s) FOR UPDATE;", (ids,))

                candidatos = _mapa_candidatos(cur, data)
                bloqueados = _contactos_bloqueados(cur, ids)

                for mensaje in data.mensajes:
                    contacto_id = int(mensaje.contacto_id)
                    candidato = candidatos.get(contacto_id)
                    if not candidato:
                        omitidos.append({"contacto_id": contacto_id, "motivo": "Ya no cumple los filtros o el proceso no está vigente."})
                        continue
                    if contacto_id in bloqueados:
                        omitidos.append({"contacto_id": contacto_id, "motivo": "Tiene un SMS enviado en 24h o un mensaje actualmente en cola."})
                        continue

                    telefono = normalizar_telefono(str(candidato.get("telefono") or ""))
                    if not telefono:
                        omitidos.append({"contacto_id": contacto_id, "motivo": "No tiene un número móvil válido."})
                        continue

                    if _conflicto_cola(cur, candidato.get("inmueble_id"), telefono, tipo_campana):
                        omitidos.append({"contacto_id": contacto_id, "motivo": "Ya existe un mensaje activo para ese teléfono/campaña."})
                        continue

                    if not candidato.get("saldo_verificado"):
                        omitidos.append({
                            "contacto_id": contacto_id,
                            "motivo": "El saldo no ha sido liquidado y verificado por el motor de liquidación.",
                        })
                        continue

                    texto = mensaje.mensaje_texto.strip()
                    if not texto:
                        omitidos.append({"contacto_id": contacto_id, "motivo": "El mensaje quedó vacío."})
                        continue

                    cur.execute(
                        """
                        INSERT INTO sms_cola_envios(
                            inmueble_id, contacto_id, identificacion, nombre,
                            conjunto_residencial, torre_apto, telefono,
                            saldo_calculado, saldo_fuente, saldo_verificado,
                            saldo_calculado_en, mensaje_template, mensaje_texto,
                            tipo_campana, estado
                        )
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDIENTE')
                        RETURNING id;
                        """,
                        (
                            candidato.get("inmueble_id"),
                            contacto_id,
                            candidato.get("identificacion"),
                            candidato.get("nombre"),
                            candidato.get("conjunto_residencial"),
                            candidato.get("torre_apto"),
                            telefono,
                            candidato.get("saldo_total"),
                            candidato.get("saldo_fuente"),
                            True,
                            candidato.get("saldo_calculado_en"),
                            texto,
                            texto,
                            tipo_campana,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        insertados.append(int(row["id"] if isinstance(row, dict) else row[0]))

        return {"status": "ok", "insertados": len(insertados), "ids": insertados, "omitidos": omitidos}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.release()


def install(app) -> None:
    """Registra la confirmación del wizard una sola vez."""
    if getattr(app.state, "sms_wizard_installed", False):
        return

    @app.post("/sms/wizard/confirmar-cola")
    def confirmar_cola(data: WizardConfirmacion):
        return _confirmar_cola(data)

    app.state.sms_wizard_installed = True
