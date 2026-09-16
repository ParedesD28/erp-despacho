"""Lectura normalizada de partes durante la transición del ERP.

Este adaptador permite que los servicios existentes consuman proceso_partes como
fuente principal sin retirar todavía los campos históricos de procesos.
"""
from __future__ import annotations

import expedientes_service


def _get_demandantes(cur, proceso):
    if not proceso:
        return []
    radicado = str(proceso.get("radicado_interno") or "").strip()
    if not radicado:
        return []

    cur.execute(
        """
        SELECT c.identificacion, c.nombre, c.tipo, c.telefono, c.email,
               c.direccion, c.ciudad, pp.es_principal
        FROM proceso_partes pp
        JOIN contactos c ON c.id = pp.contacto_id
        WHERE pp.radicado_interno = %s
          AND pp.rol = 'DEMANDANTE'
        ORDER BY pp.es_principal DESC, pp.id ASC
        """,
        (radicado,),
    )
    return [
        {
            "identificacion": row[0],
            "nombre": row[1],
            "tipo": row[2],
            "telefono": row[3] or "",
            "email": row[4] or "",
            "direccion": row[5] or "",
            "ciudad": row[6] or "",
            "es_principal": bool(row[7]),
        }
        for row in cur.fetchall()
    ]


def _get_demandados(cur, radicado):
    radicado = str(radicado or "").strip()
    if not radicado:
        return []

    cur.execute(
        """
        SELECT c.identificacion, c.nombre, c.tipo, c.telefono, c.email,
               c.direccion, c.ciudad, pp.es_principal
        FROM proceso_partes pp
        JOIN contactos c ON c.id = pp.contacto_id
        WHERE pp.radicado_interno = %s
          AND pp.rol = 'DEMANDADO'
        ORDER BY pp.es_principal DESC, pp.id ASC
        """,
        (radicado,),
    )
    return [
        {
            "identificacion": row[0],
            "nombre": row[1],
            "tipo": row[2],
            "telefono": row[3] or "",
            "email": row[4] or "",
            "direccion": row[5] or "",
            "ciudad": row[6] or "",
            "es_principal": bool(row[7]),
        }
        for row in cur.fetchall()
    ]


def install() -> None:
    """Activa las lecturas normalizadas sin cambiar firmas públicas del ERP."""
    expedientes_service._get_demandantes = _get_demandantes
    expedientes_service._get_demandados = _get_demandados
