"""Capa de transición para candidatos SMS multicanal/multiproceso.

Mantiene la ruta SMS existente, pero cambia la fuente de candidatos para que
no dependa exclusivamente de inmueble_propietarios/expensas_ph. Prioriza una
deuda activa de proceso para los demandados de proceso_partes y mantiene el
flujo PH como respaldo cuando no existe proceso con pretensión pendiente.

No cambia el worker Android ni el contrato HTTP de la cola.
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
        """Combina cartera PH y cartera por proceso, una fila por contacto."""
        cartera = (tipo_cartera or "").upper().strip()
        if cartera and cartera not in sms_router.CARTERAS_VALIDAS:
            raise ValueError("Tipo de cartera no válido.")

        # 1) Conservamos íntegramente el comportamiento probado de PH.
        ph_rows = original(cur, cartera, 0, None, ids, conjunto)

        # 2) Añadimos demandados de procesos, aunque no tengan inmueble.
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
                COALESCE(p.tipo_cartera,'PREJURIDICO') AS tipo_cartera,
                pob.obligacion_id,
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
            LEFT JOIN LATERAL (
                SELECT po.obligacion_id
                FROM proceso_obligaciones po
                JOIN obligaciones o ON o.id=po.obligacion_id
                WHERE po.radicado_interno=p.radicado_interno
                  AND UPPER(COALESCE(o.estado,'ACTIVA')) NOT IN ('CANCELADA','ANULADA')
                ORDER BY po.es_principal DESC, po.id
                LIMIT 1
            ) pob ON TRUE
            WHERE pob.obligacion_id IS NOT NULL
              AND {' AND '.join(where)}
            ORDER BY pp.contacto_id, p.radicado_interno DESC;
            """,
            params,
        )
        proceso_rows = cur.fetchall()

        candidatos = {}

        for row in ph_rows:
            key = row["contacto_id"] if isinstance(row, dict) else row[1]
            candidatos[key] = dict(row)
            candidatos[key]["fuente_cobro"] = "PH"
            candidatos[key]["radicado_interno"] = None

        for row in proceso_rows:
            data = dict(row) if isinstance(row, dict) else {
                "radicado_interno": row[0],
                "contacto_id": row[1],
                "inmueble_id": row[2],
                "radicado_rama": row[3],
                "naturaleza": row[4],
                "tipo_cartera": row[5],
                "obligacion_id": row[6],
                "identificacion": row[7],
                "nombre": row[8],
                "telefono": row[9],
                "conjunto_residencial": row[10],
                "torre_apto": row[11],
            }
            key = data["contacto_id"]
            candidato = {
                "inmueble_id": data["inmueble_id"],
                "contacto_id": data["contacto_id"],
                "conjunto_residencial": data["conjunto_residencial"] or f"PROCESO {data['radicado_interno']}",
                "torre_apto": data["torre_apto"] or (data["naturaleza"] or "Cobranza"),
                "identificacion": data["identificacion"],
                "nombre": data["nombre"],
                "telefono": data["telefono"],
                "tipo_cartera": data["tipo_cartera"],
                "obligacion_id": data["obligacion_id"],
                "fuente_cobro": "PROCESO",
                "radicado_interno": data["radicado_interno"],
                "radicado_rama": data["radicado_rama"],
                "naturaleza": data["naturaleza"],
            }
            actual = candidatos.get(key)
            # Una deuda procesal activa tiene prioridad sobre la vista PH si
            # ambas fuentes apuntan al mismo contacto.
            if actual is None or candidato["fuente_cobro"] == "PROCESO":
                candidatos[key] = candidato

        resultado = enriquecer_candidatos(list(candidatos.values()))

        # Los límites se aplican al saldo liquidado, no a pretensiones/capital.
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
