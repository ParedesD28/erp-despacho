"""Servicios canónicos para obligaciones financieras.

La obligación es la entidad económica. El proceso es la entidad procedural.
Durante la transición se mantienen columnas legacy de obligaciones, pero las
nuevas rutas deben consumir este servicio en lugar de insertar directamente.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any


def listar_tipos_obligacion(cur, activos: bool = True) -> list[dict]:
    sql = """
        SELECT id, codigo, nombre, activo,
               requiere_documento, requiere_conjunto,
               requiere_inmueble, fuente_saldo
        FROM tipos_obligacion
    """
    if activos:
        sql += " WHERE activo=TRUE"
    sql += " ORDER BY id"
    cur.execute(sql)
    return [dict(r) for r in cur.fetchall()]


def obtener_tipo_obligacion(cur, codigo: str) -> dict | None:
    cur.execute(
        """
        SELECT id, codigo, nombre, activo,
               requiere_documento, requiere_conjunto,
               requiere_inmueble, fuente_saldo
        FROM tipos_obligacion
        WHERE codigo=%s AND activo=TRUE
        LIMIT 1
        """,
        (str(codigo or "").strip().upper(),),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def crear_obligacion(
    cur,
    *,
    radicado_interno: str,
    tipo_proceso_id: int,
    tipo_obligacion: dict,
    deudor: dict,
    acreedor: dict | None,
    inmueble_id: int | None,
    numero_documento: str | None,
    capital_inicial: Decimal | int | float | None,
    fecha_exigibilidad: date | str | None,
) -> int:
    """Crea una obligación y conserva compatibilidad con el esquema legacy."""

    if not tipo_obligacion:
        raise ValueError("Tipo de obligación no válido")

    if not deudor or not deudor.get("id"):
        raise ValueError("La obligación requiere un deudor válido")

    if tipo_obligacion.get("requiere_inmueble") and not inmueble_id:
        raise ValueError(
            f"La obligación {tipo_obligacion['codigo']} requiere inmueble"
        )

    if tipo_obligacion.get("requiere_documento") and not str(
        numero_documento or ""
    ).strip():
        raise ValueError(
            f"La obligación {tipo_obligacion['codigo']} requiere número de documento"
        )

    monto = Decimal(str(capital_inicial or 0))
    fecha_texto = ""
    if fecha_exigibilidad:
        fecha_texto = fecha_exigibilidad.isoformat() if hasattr(
            fecha_exigibilidad, "isoformat"
        ) else str(fecha_exigibilidad)

    cur.execute(
        """
        INSERT INTO obligaciones (
            identificacion_deudor,
            tipo_titulo,
            numero_documento,
            capital,
            fecha_exigibilidad,
            estado,
            proceso_id,
            tipo_proceso_id,
            tipo_obligacion_id,
            deudor_contacto_id,
            acreedor_contacto_id,
            inmueble_id,
            capital_inicial,
            fuente_saldo
        )
        VALUES (
            %s,%s,%s,%s,NULLIF(%s,''),
            'ACTIVA',
            %s,%s,%s,%s,%s,%s,%s,%s
        )
        RETURNING id
        """,
        (
            deudor.get("identificacion"),
            tipo_obligacion["codigo"],
            str(numero_documento or "").strip() or None,
            monto,
            fecha_texto,
            radicado_interno,
            tipo_proceso_id,
            tipo_obligacion["id"],
            deudor["id"],
            (acreedor or {}).get("id"),
            inmueble_id,
            monto,
            tipo_obligacion.get("fuente_saldo"),
        ),
    )
    row = cur.fetchone()
    if isinstance(row, dict):
        return int(row["id"])
    return int(row[0])


def vincular_obligacion_a_proceso(
    cur,
    *,
    radicado_interno: str,
    obligacion_id: int,
    es_principal: bool = True,
) -> None:
    cur.execute(
        """
        INSERT INTO proceso_obligaciones
            (radicado_interno,obligacion_id,es_principal)
        VALUES (%s,%s,%s)
        ON CONFLICT (radicado_interno,obligacion_id)
        DO UPDATE SET es_principal=EXCLUDED.es_principal
        """,
        (radicado_interno, obligacion_id, es_principal),
    )


def obtener_obligaciones_proceso(cur, radicado_interno: str) -> list[dict]:
    cur.execute(
        """
        SELECT
            o.id,
            o.tipo_obligacion_id,
            tob.codigo AS tipo_obligacion_codigo,
            tob.nombre AS tipo_obligacion_nombre,
            o.identificacion_deudor,
            o.deudor_contacto_id,
            o.acreedor_contacto_id,
            o.inmueble_id,
            o.numero_documento,
            o.capital_inicial,
            o.fecha_exigibilidad,
            o.estado,
            o.fuente_saldo,
            po.es_principal
        FROM proceso_obligaciones po
        JOIN obligaciones o ON o.id=po.obligacion_id
        LEFT JOIN tipos_obligacion tob ON tob.id=o.tipo_obligacion_id
        WHERE po.radicado_interno=%s
        ORDER BY po.es_principal DESC, o.id
        """,
        (radicado_interno,),
    )
    return [dict(r) for r in cur.fetchall()]


def obtener_obligacion_principal(cur, radicado_interno: str) -> dict | None:
    rows = obtener_obligaciones_proceso(cur, radicado_interno)
    return rows[0] if rows else None
