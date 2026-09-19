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
    """Crea una obligación; la multiplicidad de deudores vive en obligacion_partes."""

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
            acreedor_contacto_id,
            inmueble_id,
            capital_inicial,
            fuente_saldo
        )
        VALUES (
            %s,%s,%s,%s,NULLIF(%s,''),
            'ACTIVA',
            %s,%s,%s,%s,%s,%s,%s
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


def vincular_partes_obligacion(
    cur,
    *,
    obligacion_id: int,
    contactos_deudores: list[dict],
) -> None:
    """Registra todas las personas obligadas sin perder la multiplicidad."""
    if not contactos_deudores:
        raise ValueError("La obligación requiere al menos un deudor")

    for index, contacto in enumerate(contactos_deudores):
        contacto_id = contacto.get("id")
        if not contacto_id:
            raise ValueError("Todos los deudores deben existir en Contactos")

        cur.execute(
            """
            INSERT INTO obligacion_partes
                (obligacion_id,contacto_id,rol,es_principal)
            VALUES (%s,%s,'DEUDOR',%s)
            ON CONFLICT (obligacion_id,contacto_id,rol)
            DO UPDATE SET es_principal=EXCLUDED.es_principal
            """,
            (int(obligacion_id), int(contacto_id), index == 0),
        )


def sincronizar_deudores_obligacion(
    cur,
    *,
    obligacion_id: int,
    contactos_deudores: list[dict],
) -> None:
    """Sincroniza demandados del expediente con los roles DEUDOR de la obligación.

    Los roles CODEUDOR/GARANTE, si existen en el futuro, no se eliminan aquí.
    Se preserva el deudor principal actual cuando sigue presente; de lo contrario,
    el primer demandado pasa a ser principal por compatibilidad.
    """
    if not contactos_deudores:
        raise ValueError("La obligación requiere al menos un deudor")

    contacto_ids = [int(c["id"]) for c in contactos_deudores if c.get("id")]
    if not contacto_ids:
        raise ValueError("Todos los deudores deben existir en Contactos")

    cur.execute(
        """
        SELECT contacto_id
        FROM obligacion_partes
        WHERE obligacion_id=%s
          AND rol='DEUDOR'
          AND es_principal=TRUE
        LIMIT 1
        """,
        (int(obligacion_id),),
    )
    row = cur.fetchone()
    principal_actual = int(row["contacto_id"]) if isinstance(row, dict) and row else (
        int(row[0]) if row else None
    )
    principal_id = (
        principal_actual
        if principal_actual in contacto_ids
        else contacto_ids[0]
    )

    cur.execute(
        """
        DELETE FROM obligacion_partes
        WHERE obligacion_id=%s
          AND rol='DEUDOR'
        """,
        (int(obligacion_id),),
    )

    for contacto_id in contacto_ids:
        cur.execute(
            """
            INSERT INTO obligacion_partes
                (obligacion_id,contacto_id,rol,es_principal)
            VALUES (%s,%s,'DEUDOR',%s)
            """,
            (int(obligacion_id), contacto_id, contacto_id == principal_id),
        )

    cur.execute(
        """
        SELECT identificacion
        FROM contactos
        WHERE id=%s
        LIMIT 1
        """,
        (principal_id,),
    )
    principal_contacto = cur.fetchone()
    principal_identificacion = (
        principal_contacto["identificacion"]
        if isinstance(principal_contacto, dict)
        else principal_contacto[0] if principal_contacto else None
    )

    cur.execute(
        """
        UPDATE obligaciones
        SET identificacion_deudor=%s
        WHERE id=%s
        """,
        (principal_identificacion, int(obligacion_id)),
    )


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
