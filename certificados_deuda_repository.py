"""Capa de acceso a Neon para Certificados de Deuda.

Driver real: psycopg2 + ThreadedConnectionPool (`db.get_connection()`).
No hay SQLAlchemy ni asyncpg: las consultas son síncronas y deben
envolverse con `asyncio.to_thread()` desde endpoints FastAPI.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from psycopg2.extras import RealDictCursor

import db


def _norm_key(texto: str) -> str:
    """Normaliza para comparar torre/apto (alfanumérico mayúsculas)."""
    return re.sub(r"[^A-Z0-9]+", "", (texto or "").upper())


def _variantes_torre_apto(
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
) -> list[str]:
    """Candidatos de unidad a partir de torre_apto o bloque+apartamento del PDF."""
    out: list[str] = []
    for raw in (
        torre_apto,
        f"{bloque}-{apartamento}" if bloque and apartamento else "",
        f"{bloque} {apartamento}" if bloque and apartamento else "",
        f"TORRE {bloque} APTO {apartamento}" if bloque and apartamento else "",
        f"BLOQUE {bloque} APTO {apartamento}" if bloque and apartamento else "",
        apartamento or "",
        bloque or "",
    ):
        limpio = " ".join(str(raw or "").strip().split())
        if limpio and limpio not in out:
            out.append(limpio)
    return out


def _titular_desde_fila(row: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    nombre = (row.get("titular_nombre") or "").strip() or None
    cedula = (row.get("titular_cedula") or "").strip() or None
    if not nombre:
        nombre = (row.get("contacto_nombre") or "").strip() or None
    if not cedula:
        cedula = (row.get("contacto_cedula") or "").strip() or None
    return nombre, cedula


def _sql_base() -> str:
    return """
        SELECT
            i.id AS inmueble_id,
            i.torre_apto,
            i.conjunto_id,
            COALESCE(NULLIF(BTRIM(i.conjunto_residencial), ''), cr.nombre) AS conjunto_nombre,
            cr.nombre AS conjunto_catalogo,
            cr.contacto_id AS copropiedad_contacto_id,
            pj.nombre AS copropiedad_nombre,
            pj.identificacion AS copropiedad_nit,
            COALESCE(NULLIF(BTRIM(pj.ciudad), ''), NULLIF(BTRIM(c_legacy.ciudad), '')) AS ciudad,
            tit.nombre AS titular_nombre,
            tit.identificacion AS titular_cedula,
            c_legacy.nombre AS contacto_nombre,
            c_legacy.identificacion AS contacto_cedula
        FROM inmuebles_ph i
        LEFT JOIN conjuntos_residenciales cr ON cr.id = i.conjunto_id
        LEFT JOIN contactos pj ON pj.id = cr.contacto_id
        LEFT JOIN contactos c_legacy ON c_legacy.id = i.contacto_id
        LEFT JOIN LATERAL (
            SELECT c.nombre, c.identificacion
            FROM inmueble_propietarios ip
            JOIN contactos c ON c.id = ip.contacto_id
            WHERE ip.inmueble_id = i.id
            ORDER BY ip.es_principal DESC, c.id ASC
            LIMIT 1
        ) tit ON TRUE
    """


def _map_row(row: dict[str, Any]) -> dict[str, Any]:
    titular_nombre, titular_cedula = _titular_desde_fila(row)
    return {
        "inmueble_id": int(row["inmueble_id"]),
        "torre_apto": (row.get("torre_apto") or "").strip() or None,
        "conjunto_id": row.get("conjunto_id"),
        "conjunto_nombre": (
            (row.get("conjunto_nombre") or row.get("conjunto_catalogo") or "").strip()
            or None
        ),
        "copropiedad_nombre": (row.get("copropiedad_nombre") or "").strip() or None,
        "copropiedad_nit": (row.get("copropiedad_nit") or "").strip() or None,
        "ciudad": (row.get("ciudad") or "").strip() or None,
        "titular_nombre": titular_nombre,
        "titular_cedula": titular_cedula,
    }


def obtener_por_inmueble_id(inmueble_id: int, *, conn=None) -> Optional[dict[str, Any]]:
    """Busca inmueble + copropiedad + titular por PK."""
    owns = conn is None
    if owns:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                _sql_base() + " WHERE i.id = %s LIMIT 1",
                (int(inmueble_id),),
            )
            row = cur.fetchone()
            return _map_row(dict(row)) if row else None
    finally:
        if owns and conn is not None:
            conn.release()


def buscar_por_conjunto_y_unidad(
    *,
    conjunto_id: Optional[int] = None,
    conjunto_nombre: str = "",
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
    conn=None,
) -> Optional[dict[str, Any]]:
    """
    Cruza conjunto (id o nombre) + unidad (torre_apto / bloque+apto).

    `codigo_cuenta` COLON no existe en tablas maestras Neon; no se usa aquí.
    """
    variantes = _variantes_torre_apto(torre_apto, bloque, apartamento)
    if not variantes:
        return None
    if not conjunto_id and not (conjunto_nombre or "").strip():
        return None

    owns = conn is None
    if owns:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where = ["TRUE"]
            params: list[Any] = []
            if conjunto_id:
                where.append("(i.conjunto_id = %s OR cr.id = %s)")
                params.extend([int(conjunto_id), int(conjunto_id)])
            if (conjunto_nombre or "").strip():
                where.append(
                    "("
                    " UPPER(BTRIM(COALESCE(i.conjunto_residencial, ''))) = UPPER(BTRIM(%s))"
                    " OR UPPER(BTRIM(COALESCE(cr.nombre, ''))) = UPPER(BTRIM(%s))"
                    ")"
                )
                nombre = conjunto_nombre.strip()
                params.extend([nombre, nombre])

            cur.execute(
                _sql_base() + f" WHERE {' AND '.join(where)}",
                params,
            )
            filas = [dict(r) for r in cur.fetchall()]
    finally:
        if owns and conn is not None:
            conn.release()

    keys = {_norm_key(v) for v in variantes if _norm_key(v)}
    for fila in filas:
        torre = _norm_key(str(fila.get("torre_apto") or ""))
        if torre and torre in keys:
            return _map_row(fila)
    # Coincidencia parcial: apto contenido en torre_apto o viceversa
    for fila in filas:
        torre = _norm_key(str(fila.get("torre_apto") or ""))
        if not torre:
            continue
        if any(k and (k in torre or torre in k) for k in keys):
            return _map_row(fila)
    return None


def resolver_datos_certificado(
    *,
    inmueble_id: Optional[int] = None,
    conjunto_id: Optional[int] = None,
    conjunto_nombre: str = "",
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
    conn=None,
) -> Optional[dict[str, Any]]:
    """Resuelve datos maestros: prioriza inmueble_id, luego conjunto+unidad."""
    if inmueble_id is not None:
        hallado = obtener_por_inmueble_id(int(inmueble_id), conn=conn)
        if hallado:
            return hallado
    return buscar_por_conjunto_y_unidad(
        conjunto_id=conjunto_id,
        conjunto_nombre=conjunto_nombre,
        torre_apto=torre_apto,
        bloque=bloque,
        apartamento=apartamento,
        conn=conn,
    )
