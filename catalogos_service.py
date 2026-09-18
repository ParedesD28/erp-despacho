"""Catálogos maestros del ERP: tipos de proceso y conjuntos residenciales."""

from __future__ import annotations


def listar_tipos_proceso(cur, activos: bool = True) -> list[dict]:
    sql = """
        SELECT id, codigo, nombre, tipo_cartera_default,
               requiere_conjunto, requiere_inmueble, requiere_juzgado,
               requiere_documento, fuente_saldo
        FROM tipos_proceso
    """
    if activos:
        sql += " WHERE activo=TRUE"
    sql += " ORDER BY id"
    cur.execute(sql)
    return [dict(r) for r in cur.fetchall()]


def obtener_tipo_proceso(cur, codigo: str) -> dict | None:
    cur.execute(
        """
        SELECT id, codigo, nombre, tipo_cartera_default,
               requiere_conjunto, requiere_inmueble, requiere_juzgado,
               requiere_documento, fuente_saldo
        FROM tipos_proceso
        WHERE codigo=%s AND activo=TRUE
        LIMIT 1
        """,
        (codigo,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def listar_conjuntos(cur, activos: bool = True) -> list[dict]:
    sql = """
        SELECT c.id, c.nombre, c.contacto_id,
               ct.identificacion AS nit, ct.nombre AS persona_juridica,
               ct.telefono, ct.email, ct.direccion, ct.ciudad
        FROM conjuntos_residenciales c
        LEFT JOIN contactos ct ON ct.id=c.contacto_id
    """
    if activos:
        sql += " WHERE c.activo=TRUE"
    sql += " ORDER BY c.nombre"
    cur.execute(sql)
    return [dict(r) for r in cur.fetchall()]


def obtener_conjunto(cur, conjunto_id: int) -> dict | None:
    cur.execute(
        """
        SELECT c.id, c.nombre, c.contacto_id,
               ct.identificacion AS nit, ct.nombre AS persona_juridica,
               ct.telefono, ct.email, ct.direccion, ct.ciudad
        FROM conjuntos_residenciales c
        LEFT JOIN contactos ct ON ct.id=c.contacto_id
        WHERE c.id=%s
        LIMIT 1
        """,
        (conjunto_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None
