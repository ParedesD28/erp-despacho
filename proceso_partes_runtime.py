"""Lectura normalizada de partes durante la transición del ERP.

Este adaptador permite que los servicios existentes consuman proceso_partes como
fuente principal sin retirar todavía los campos históricos de procesos.
"""
from __future__ import annotations

import expedientes_service


def _value(row, key, index, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        value = row.get(key, default)
        return default if value is None else value
    try:
        return row[index]
    except (KeyError, IndexError, TypeError):
        return default


def _build_party(row):
    return {
        "identificacion": _value(row, "identificacion", 0),
        "nombre": _value(row, "nombre", 1),
        "tipo": _value(row, "tipo", 2),
        "telefono": _value(row, "telefono", 3, "") or "",
        "email": _value(row, "email", 4, "") or "",
        "direccion": _value(row, "direccion", 5, "") or "",
        "ciudad": _value(row, "ciudad", 6, "") or "",
        "es_principal": bool(_value(row, "es_principal", 7, False)),
    }


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
    return [_build_party(row) for row in cur.fetchall()]


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
    return [_build_party(row) for row in cur.fetchall()]


def install() -> None:
    """Activa las lecturas normalizadas sin cambiar firmas públicas del ERP."""
    expedientes_service._get_demandantes = _get_demandantes
    expedientes_service._get_demandados = _get_demandados
