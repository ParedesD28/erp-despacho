"""Capa de transición para candidatos SMS por obligación.

El candidato financiero siempre debe identificar una obligación. Se conserva
la consulta PH anterior como fuente de candidatos, pero el saldo se resuelve
posteriormente mediante obligacion_saldo_service.
"""
from __future__ import annotations

from typing import Any, List, Optional

from sms_saldo_service import enriquecer_candidatos


def _install_candidate_query() -> None:
    import sms_router

    original = sms_router._candidatos_cartera

    def _candidatos_cartera_general(
        cur,
        tipo_cartera: str = "",
        saldo_minimo: float = 0,
        saldo_maximo: Optional[float] = None,
        ids: Optional[List[int]] = None,
        conjunto: str = "",
    ):
        cartera = (tipo_cartera or "").upper().strip()
        if cartera and cartera not in sms_router.CARTERAS_VALIDAS:
            raise ValueError("Tipo de cartera no válido.")

        # 1. Fuente PH histórica. La enlazamos explícitamente a su obligación.
        ph_rows = original(cur, cartera, 0, None, ids, conjunto)
        ph_inmuebles = []
        for row in ph_rows:
            data = dict(row) if isinstance(row, dict) else {}
            if data.get("inmueble_id"):
                ph_inmuebles.append(int(data["inmueble_id"]))

        obligation_by_inmueble: dict[int, int] = {}
        if ph_inmuebles:
            cur.execute(
                """
                SELECT DISTINCT ON (o.inmueble_id)
                    o.inmueble_id,
                    o.id AS obligacion_id
                FROM obligaciones o
                JOIN tipos_obligacion tob ON tob.id=o.tipo_obligacion_id
                WHERE o.inmueble_id = ANY(%s)
                  AND tob.codigo='CUOTAS_ADMINISTRACION'
                  AND COALESCE(o.estado,'ACTIVA') NOT IN ('CANCELADA','ANULADA')
                ORDER BY o.inmueble_id, o.id DESC
                """,
                (list(dict.fromkeys(ph_inmuebles)),),
            )
            rows = cur.fetchall()
            for row in rows:
                data = dict(row) if isinstance(row, dict) else {
                    "inmueble_id": row[0],
                    "obligacion_id": row[1],
                }
                obligation_by_inmueble[int(data["inmueble_id"])] = int(data["obligacion_id"])

        params: List[Any] = []
        where = [
            "pp.rol = 'DEMANDADO'",
            "c.telefono IS NOT NULL",
            "TRIM(c.telefono) <> ''",
            "LOWER(TRIM(COALESCE(p.estado,'Activo'))) NOT IN ('cancelado','inactivo','archivado','terminado','suspendido','cerrado','finalizado','anulado')",
            "NOT EXISTS ("
            " SELECT 1 FROM sms_cola_envios prev"
            " WHERE prev.contacto_id = c.id"
            "   AND prev.estado = 'ENVIADO'"
            "   AND prev.fecha_envio >= NOW() - INTERVAL '24 hours'"
            ")",
            "NOT EXISTS ("
            " SELECT 1 FROM sms_cola_envios cola"
            " WHERE cola.contacto_id = c.id"
            "   AND cola.estado IN ('PENDIENTE','EN_PROCESO')"
            ")",
        ]
        if cartera:
            where.append("COALESCE(p.tipo_cartera,'PREJURIDICO') = %s")
            params.append(cartera)
        if ids:
            where.append("c.id = ANY(%s)")
            params.append(ids)
        if conjunto:
            where.append("COALESCE(i.conjunto_residencial, '') = %s")
            params.append(conjunto.strip())

        cur.execute(
            f"""
            SELECT
                pp.radicado_interno,
                pp.contacto_id,
                p.inmueble_id,
                p.radicado_rama,
                p.naturaleza,
                ob.obligacion_id,
                ob.tipo_obligacion,
                COALESCE(p.tipo_cartera,'PREJURIDICO') AS tipo_cartera,
                c.identificacion,
                c.nombre,
                c.telefono,
                i.conjunto_residencial,
                i.torre_apto
            FROM proceso_partes pp
            JOIN procesos p
              ON p.radicado_interno = pp.radicado_interno
            JOIN contactos c
              ON c.id = pp.contacto_id
            LEFT JOIN inmuebles_ph i
              ON i.id = p.inmueble_id
            JOIN LATERAL (
                SELECT
                    o.id AS obligacion_id,
                    tob.codigo AS tipo_obligacion
                FROM proceso_obligaciones po
                JOIN obligaciones o
                  ON o.id=po.obligacion_id
                JOIN tipos_obligacion tob
                  ON tob.id=o.tipo_obligacion_id
                WHERE po.radicado_interno=p.radicado_interno
                  AND COALESCE(o.estado,'ACTIVA') NOT IN ('CANCELADA','ANULADA')
                ORDER BY po.es_principal DESC, po.id
                LIMIT 1
            ) ob ON TRUE
            WHERE {' AND '.join(where)}
            ORDER BY pp.contacto_id, p.radicado_interno DESC;
            """,
            params,
        )
        proceso_rows = cur.fetchall()

        candidatos = {}

        for row in ph_rows:
            data = dict(row) if isinstance(row, dict) else {
                "inmueble_id": row[0],
                "contacto_id": row[1],
                "conjunto_residencial": row[2],
                "torre_apto": row[3],
                "identificacion": row[4],
                "nombre": row[5],
                "telefono": row[6],
                "saldo_total": None,
                "tipo_cartera": row[7] if len(row) > 7 else "PREJURIDICO",
            }
            data["obligacion_id"] = obligation_by_inmueble.get(
                int(data["inmueble_id"])
            ) if data.get("inmueble_id") else None
            data["saldo_total"] = None
            key = data["contacto_id"]
            data["fuente_cobro"] = "PH"
            data["radicado_interno"] = None
            candidatos[key] = data

        for row in proceso_rows:
            data = dict(row) if isinstance(row, dict) else {
                "radicado_interno": row[0],
                "contacto_id": row[1],
                "inmueble_id": row[2],
                "radicado_rama": row[3],
                "naturaleza": row[4],
                "obligacion_id": row[5],
                "tipo_obligacion": row[6],
                "tipo_cartera": row[7],
                "identificacion": row[8],
                "nombre": row[9],
                "telefono": row[10],
                "conjunto_residencial": row[11],
                "torre_apto": row[12],
            }
            candidato = {
                "inmueble_id": data["inmueble_id"],
                "obligacion_id": data["obligacion_id"],
                "contacto_id": data["contacto_id"],
                "conjunto_residencial": data["conjunto_residencial"] or f"PROCESO {data['radicado_interno']}",
                "torre_apto": data["torre_apto"] or (data["naturaleza"] or "Cobranza"),
                "identificacion": data["identificacion"],
                "nombre": data["nombre"],
                "telefono": data["telefono"],
                "saldo_total": None,
                "tipo_cartera": data["tipo_cartera"],
                "fuente_cobro": "PROCESO",
                "radicado_interno": data["radicado_interno"],
                "radicado_rama": data["radicado_rama"],
                "naturaleza": data["naturaleza"],
                "tipo_obligacion": data["tipo_obligacion"],
            }
            key = data["contacto_id"]
            actual = candidatos.get(key)
            if actual is None or candidato["fuente_cobro"] == "PROCESO":
                candidatos[key] = candidato

        resultado = enriquecer_candidatos(list(candidatos.values()))

        salida = []
        for item in resultado:
            if not item.get("saldo_verificado"):
                item["saldo_bloqueado"] = True
                salida.append(item)
                continue

            saldo = float(item.get("saldo_total") or 0)
            if saldo < float(saldo_minimo or 0):
                continue
            if saldo_maximo is not None and saldo > float(saldo_maximo):
                continue

            item["saldo_bloqueado"] = False
            salida.append(item)

        salida.sort(
            key=lambda r: (
                not bool(r.get("saldo_verificado")),
                -float(r.get("saldo_total") or 0),
                r.get("nombre") or "",
            )
        )
        return salida

    sms_router._candidatos_cartera = _candidatos_cartera_general


def install() -> None:
    _install_candidate_query()
