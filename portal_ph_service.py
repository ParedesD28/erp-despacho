"""Portal Cliente PH: unidades (torre/apto + demandados) por conjuntos habilitados."""
from __future__ import annotations

import re
from typing import Any, Optional

from psycopg2.extras import RealDictCursor

import db
from certificados_deuda_repository import partir_bloque_apto


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema='public' AND table_name=%s
        LIMIT 1
        """,
        (table,),
    )
    return cur.fetchone() is not None


def ensure_usuario_conjuntos_schema(conn=None) -> bool:
    """Crea usuario_conjuntos si falta. Idempotente."""
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "abogados") or not _table_exists(
                cur, "conjuntos_residenciales"
            ):
                return False
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS usuario_conjuntos (
                    usuario_id INTEGER NOT NULL REFERENCES abogados(id) ON DELETE CASCADE,
                    conjunto_id INTEGER NOT NULL REFERENCES conjuntos_residenciales(id) ON DELETE CASCADE,
                    PRIMARY KEY (usuario_id, conjunto_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_usuario_conjuntos_conjunto
                ON usuario_conjuntos (conjunto_id)
                """
            )
            if _table_exists(cur, "schema_migrations"):
                cur.execute(
                    """
                    INSERT INTO schema_migrations(version)
                    VALUES ('20261006_usuario_conjuntos_portal_ph')
                    ON CONFLICT (version) DO NOTHING
                    """
                )
        if not external:
            conn.commit()
        return True
    except Exception:
        if not external:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if not external:
            conn.release()


def _celda(row, key, idx=0, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[idx]
    except Exception:
        return default


def listar_conjunto_ids_usuario(usuario_id: int | str, conn=None) -> list[int]:
    ensure_usuario_conjuntos_schema(conn=conn)
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "usuario_conjuntos"):
                return []
            cur.execute(
                """
                SELECT conjunto_id
                FROM usuario_conjuntos
                WHERE usuario_id=%s
                ORDER BY conjunto_id
                """,
                (int(usuario_id),),
            )
            return [int(_celda(r, "conjunto_id", 0)) for r in cur.fetchall() or []]
    finally:
        if not external:
            conn.release()


def listar_conjuntos_habilitados(usuario_id: int | str, conn=None) -> list[dict]:
    """Conjuntos activos vinculados al usuario."""
    ensure_usuario_conjuntos_schema(conn=conn)
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "usuario_conjuntos"):
                return []
            cur.execute(
                """
                SELECT c.id, c.nombre
                FROM usuario_conjuntos uc
                JOIN conjuntos_residenciales c ON c.id = uc.conjunto_id
                WHERE uc.usuario_id=%s
                  AND COALESCE(c.activo, TRUE) = TRUE
                ORDER BY c.nombre
                """,
                (int(usuario_id),),
            )
            return [dict(r) for r in cur.fetchall() or []]
    finally:
        if not external:
            conn.release()


def reemplazar_conjuntos_usuario(
    usuario_id: int | str,
    conjunto_ids: list[Any],
    *,
    conn=None,
) -> list[int]:
    """Reemplaza los conjuntos habilitados para el usuario. Devuelve ids guardados."""
    ensure_usuario_conjuntos_schema(conn=conn)
    uid = int(usuario_id)
    ids: list[int] = []
    for raw in conjunto_ids or []:
        s = str(raw or "").strip()
        if s.isdigit():
            ids.append(int(s))
    ids = sorted(set(ids))

    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "usuario_conjuntos"):
                raise RuntimeError("Tabla usuario_conjuntos no disponible")
            # Solo conjuntos existentes
            if ids:
                cur.execute(
                    """
                    SELECT id FROM conjuntos_residenciales
                    WHERE id = ANY(%s)
                    """,
                    (ids,),
                )
                ids = sorted(int(_celda(r, "id", 0)) for r in cur.fetchall() or [])
            cur.execute("DELETE FROM usuario_conjuntos WHERE usuario_id=%s", (uid,))
            for cid in ids:
                cur.execute(
                    """
                    INSERT INTO usuario_conjuntos (usuario_id, conjunto_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (uid, cid),
                )
        if not external:
            conn.commit()
        return ids
    except Exception:
        if not external:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if not external:
            conn.release()


def _parte_numerica(token: str) -> int:
    m = re.search(r"\d+", str(token or ""))
    return int(m.group()) if m else 0


def sort_key_unidad(torre_apto: str) -> tuple:
    """Orden numérico: torre y luego apto (canónico)."""
    bloque, apto = partir_bloque_apto(torre_apto)
    texto = (torre_apto or "").strip()
    return (
        _parte_numerica(bloque),
        (bloque or "").upper(),
        _parte_numerica(apto),
        (apto or "").upper(),
        texto.upper(),
    )


def _demandados_por_inmueble(cur, inmueble_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {i: [] for i in inmueble_ids}
    if not inmueble_ids:
        return out
    if _table_exists(cur, "inmueble_propietarios") and _table_exists(cur, "contactos"):
        cur.execute(
            """
            SELECT ip.inmueble_id, ct.nombre, COALESCE(ip.es_principal, FALSE) AS es_principal
            FROM inmueble_propietarios ip
            JOIN contactos ct ON ct.id = ip.contacto_id
            WHERE ip.inmueble_id = ANY(%s)
            ORDER BY ip.inmueble_id,
                     COALESCE(ip.es_principal, FALSE) DESC,
                     ct.nombre ASC
            """,
            (inmueble_ids,),
        )
        for r in cur.fetchall() or []:
            iid = int(_celda(r, "inmueble_id", 0))
            nombre = str(_celda(r, "nombre", 1) or "").strip()
            if nombre and nombre not in out[iid]:
                out[iid].append(nombre)

    # Fallback: demandados del proceso activo ligado al inmueble
    faltan = [i for i, noms in out.items() if not noms]
    if faltan and _table_exists(cur, "procesos") and _table_exists(cur, "proceso_partes"):
        cur.execute(
            """
            SELECT DISTINCT ON (p.inmueble_id, c.id)
                   p.inmueble_id, c.nombre
            FROM procesos p
            JOIN proceso_partes pp ON pp.radicado_interno = p.radicado_interno
            JOIN contactos c ON c.id = pp.contacto_id
            WHERE p.inmueble_id = ANY(%s)
              AND UPPER(COALESCE(pp.rol, '')) = 'DEMANDADO'
              AND UPPER(COALESCE(p.estado, 'ACTIVO')) <> 'INACTIVO'
            ORDER BY p.inmueble_id, c.id, c.nombre
            """,
            (faltan,),
        )
        for r in cur.fetchall() or []:
            iid = int(_celda(r, "inmueble_id", 0))
            nombre = str(_celda(r, "nombre", 1) or "").strip()
            if nombre and nombre not in out[iid]:
                out[iid].append(nombre)
    return out


def listar_unidades_portal(
    usuario_id: int | str,
    *,
    conjunto_id: Optional[int | str] = None,
    conn=None,
) -> dict[str, Any]:
    """
    Unidades PH visibles para el cliente.

    Returns:
      conjuntos, conjunto_id (filtro efectivo), unidades[{torre_apto, demandados, ...}]
    """
    conjuntos = listar_conjuntos_habilitados(usuario_id, conn=conn)
    permitidos = {int(c["id"]) for c in conjuntos}
    filtro: Optional[int] = None
    raw = str(conjunto_id or "").strip()
    if raw.isdigit() and int(raw) in permitidos:
        filtro = int(raw)
    elif permitidos:
        # Default: primer conjunto (orden por nombre ya en listar)
        filtro = int(conjuntos[0]["id"])

    unidades: list[dict] = []
    if not filtro:
        return {
            "conjuntos": conjuntos,
            "conjunto_id": None,
            "unidades": [],
        }

    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "inmuebles_ph"):
                return {
                    "conjuntos": conjuntos,
                    "conjunto_id": filtro,
                    "unidades": [],
                }
            cur.execute(
                """
                SELECT i.id, i.torre_apto, i.conjunto_id,
                       c.nombre AS conjunto_nombre
                FROM inmuebles_ph i
                JOIN conjuntos_residenciales c ON c.id = i.conjunto_id
                WHERE i.conjunto_id=%s
                """,
                (filtro,),
            )
            filas = [dict(r) for r in cur.fetchall() or []]
            ids = [int(f["id"]) for f in filas]
            dem_map = _demandados_por_inmueble(cur, ids)
            for f in filas:
                torre = str(f.get("torre_apto") or "").strip()
                dems = dem_map.get(int(f["id"]), [])
                unidades.append(
                    {
                        "inmueble_id": int(f["id"]),
                        "torre_apto": torre or "—",
                        "conjunto_id": int(f["conjunto_id"]),
                        "conjunto_nombre": f.get("conjunto_nombre") or "",
                        "demandados": dems,
                        "demandados_texto": ", ".join(dems) if dems else "Sin demandados",
                    }
                )
            unidades.sort(key=lambda u: sort_key_unidad(u.get("torre_apto") or ""))
    finally:
        if not external:
            conn.release()

    return {
        "conjuntos": conjuntos,
        "conjunto_id": filtro,
        "unidades": unidades,
    }
