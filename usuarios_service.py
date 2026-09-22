"""Usuarios humanos del ERP (tabla abogados) + perfiles.

El bot no pasa por aquí. Arranque asegura esquema de forma idempotente
para no romper login si la migración SQL aún no se aplicó a mano en Neon.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg2.extras import RealDictCursor

import db
import permisos
from security import hash_password, is_bcrypt_hash


class UsuariosError(ValueError):
    """Error de validación de negocio al administrar usuarios."""


def _columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (table,),
    )
    return {str(r[0]).lower() for r in cur.fetchall()}


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name=%s
        LIMIT 1
        """,
        (table,),
    )
    return cur.fetchone() is not None


def ensure_perfiles_schema(conn=None) -> dict[str, Any]:
    """Crea perfiles, columnas en abogados y asigna ADMIN a usuarios sin perfil."""
    external = conn is not None
    if not external:
        conn = db.get_connection()
    resultado = {"perfiles": False, "columnas": False, "migrados_admin": 0}
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "abogados"):
                return resultado

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS perfiles (
                    id SERIAL PRIMARY KEY,
                    codigo VARCHAR(32) NOT NULL UNIQUE,
                    nombre TEXT NOT NULL,
                    descripcion TEXT,
                    activo BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )

            for codigo in permisos.PERFILES_HUMANOS:
                cur.execute(
                    """
                    INSERT INTO perfiles (codigo, nombre, descripcion, activo)
                    VALUES (%s, %s, %s, TRUE)
                    ON CONFLICT (codigo) DO UPDATE
                      SET nombre=EXCLUDED.nombre,
                          descripcion=EXCLUDED.descripcion,
                          activo=TRUE
                    """,
                    (
                        codigo,
                        permisos.PERFIL_NOMBRES[codigo],
                        permisos.PERFIL_DESCRIPCIONES[codigo],
                    ),
                )
            resultado["perfiles"] = True

            cols = _columns(cur, "abogados")
            if "perfil_id" not in cols:
                cur.execute(
                    """
                    ALTER TABLE abogados
                    ADD COLUMN perfil_id INTEGER REFERENCES perfiles(id)
                    """
                )
                resultado["columnas"] = True
            if "activo" not in cols:
                cur.execute(
                    """
                    ALTER TABLE abogados
                    ADD COLUMN activo BOOLEAN NOT NULL DEFAULT TRUE
                    """
                )
                resultado["columnas"] = True

            # Rollout seguro: quien ya existe queda como Admin.
            cur.execute("SELECT id FROM perfiles WHERE codigo=%s LIMIT 1", (permisos.PERFIL_ADMIN,))
            admin = cur.fetchone()
            if admin:
                cur.execute(
                    """
                    UPDATE abogados
                    SET perfil_id=%s
                    WHERE perfil_id IS NULL
                    """,
                    (admin["id"],),
                )
                resultado["migrados_admin"] = cur.rowcount or 0

            if _table_exists(cur, "schema_migrations"):
                cur.execute(
                    """
                    INSERT INTO schema_migrations(version)
                    VALUES ('20260922_perfiles_permisos')
                    ON CONFLICT (version) DO NOTHING
                    """
                )
        if not external:
            conn.commit()
        return resultado
    except Exception:
        if not external:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if not external and conn:
            conn.release()


def _select_usuario_sql(cols: set[str]) -> str:
    tiene_perfil = "perfil_id" in cols
    tiene_activo = "activo" in cols
    select_perfil = "a.perfil_id" if tiene_perfil else "NULL::integer AS perfil_id"
    select_activo = "COALESCE(a.activo, TRUE) AS activo" if tiene_activo else "TRUE AS activo"
    join_perfil = (
        "LEFT JOIN perfiles p ON p.id = a.perfil_id" if tiene_perfil else ""
    )
    select_codigo = (
        "p.codigo AS perfil_codigo" if tiene_perfil else "NULL::text AS perfil_codigo"
    )
    select_nombre_perfil = (
        "p.nombre AS perfil_nombre" if tiene_perfil else "NULL::text AS perfil_nombre"
    )
    return f"""
        SELECT a.id, a.email, a.nombre, a.password,
               {select_perfil}, {select_activo},
               {select_codigo}, {select_nombre_perfil}
        FROM abogados a
        {join_perfil}
    """


def obtener_usuario_por_id(user_id: str, conn=None) -> Optional[dict]:
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "abogados"):
                return None
            cols = _columns(cur, "abogados")
            sql = _select_usuario_sql(cols) + " WHERE a.id=%s LIMIT 1"
            cur.execute(sql, (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        if not external and conn:
            conn.release()


def obtener_usuario_por_email(email: str, conn=None) -> Optional[dict]:
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "abogados"):
                return None
            cols = _columns(cur, "abogados")
            sql = _select_usuario_sql(cols) + " WHERE LOWER(a.email)=LOWER(%s) LIMIT 1"
            cur.execute(sql, (email.strip(),))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        if not external and conn:
            conn.release()


def contexto_auth_usuario(user_id: str) -> dict[str, Any]:
    """Contexto para middleware: perfil, permisos, activo.

    Si el esquema aún no existe o falta el usuario, se asume ADMIN activo
    para no expulsar a nadie en el primer rollout.
    """
    fallback = {
        "user_id": str(user_id),
        "nombre": "",
        "email": "",
        "activo": True,
        "perfil_codigo": permisos.PERFIL_ADMIN,
        "perfil_nombre": permisos.PERFIL_NOMBRES[permisos.PERFIL_ADMIN],
        "permisos": permisos.permisos_de_perfil(permisos.PERFIL_ADMIN),
    }
    try:
        usuario = obtener_usuario_por_id(user_id)
    except Exception:
        return fallback
    if not usuario:
        return fallback

    activo = bool(usuario.get("activo", True))
    codigo = permisos.normalizar_perfil(usuario.get("perfil_codigo"))
    return {
        "user_id": str(usuario["id"]),
        "nombre": str(usuario.get("nombre") or ""),
        "email": str(usuario.get("email") or ""),
        "activo": activo,
        "perfil_codigo": codigo,
        "perfil_nombre": str(
            usuario.get("perfil_nombre") or permisos.PERFIL_NOMBRES.get(codigo, codigo)
        ),
        "permisos": permisos.permisos_de_perfil(codigo),
    }


def listar_perfiles(conn=None) -> list[dict]:
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "perfiles"):
                return [
                    {
                        "id": None,
                        "codigo": c,
                        "nombre": permisos.PERFIL_NOMBRES[c],
                        "descripcion": permisos.PERFIL_DESCRIPCIONES[c],
                    }
                    for c in permisos.PERFILES_HUMANOS
                ]
            cur.execute(
                """
                SELECT id, codigo, nombre, descripcion
                FROM perfiles
                WHERE activo=TRUE
                ORDER BY
                  CASE codigo
                    WHEN 'ADMIN' THEN 1
                    WHEN 'ABOGADO' THEN 2
                    WHEN 'AUXILIAR_COBRO' THEN 3
                    WHEN 'CONSULTA' THEN 4
                    ELSE 9
                  END
                """
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        if not external and conn:
            conn.release()


def listar_usuarios(incluir_inactivos: bool = True, conn=None) -> list[dict]:
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cols = _columns(cur, "abogados")
            sql = _select_usuario_sql(cols)
            if not incluir_inactivos and "activo" in cols:
                sql += " WHERE COALESCE(a.activo, TRUE)=TRUE"
            sql += " ORDER BY COALESCE(a.activo, TRUE) DESC, a.nombre ASC NULLS LAST, a.email ASC"
            cur.execute(sql)
            filas = []
            for row in cur.fetchall():
                item = dict(row)
                item.pop("password", None)
                codigo = permisos.normalizar_perfil(item.get("perfil_codigo"))
                item["perfil_codigo"] = codigo
                item["perfil_nombre"] = item.get("perfil_nombre") or permisos.PERFIL_NOMBRES[codigo]
                filas.append(item)
            return filas
    finally:
        if not external and conn:
            conn.release()


def _perfil_id_por_codigo(cur, codigo: str) -> int:
    codigo = permisos.normalizar_perfil(codigo)
    cur.execute("SELECT id FROM perfiles WHERE codigo=%s LIMIT 1", (codigo,))
    row = cur.fetchone()
    if not row:
        raise UsuariosError(f"Perfil desconocido: {codigo}")
    return int(row["id"] if isinstance(row, dict) else row[0])


def _contar_admins_activos(cur, excluir_id: Optional[Any] = None) -> int:
    sql = """
        SELECT COUNT(*) AS n
        FROM abogados a
        JOIN perfiles p ON p.id = a.perfil_id
        WHERE p.codigo=%s AND COALESCE(a.activo, TRUE)=TRUE
    """
    params: list[Any] = [permisos.PERFIL_ADMIN]
    if excluir_id is not None:
        sql += " AND a.id<>%s"
        params.append(excluir_id)
    cur.execute(sql, params)
    row = cur.fetchone()
    return int((row["n"] if isinstance(row, dict) else row[0]) or 0)


def crear_usuario(
    *,
    nombre: str,
    email: str,
    password: str,
    perfil_codigo: str,
    activo: bool = True,
    conn=None,
) -> dict:
    nombre = (nombre or "").strip()
    email = (email or "").strip().lower()
    password = password or ""
    if not nombre:
        raise UsuariosError("El nombre es obligatorio")
    if not email or "@" not in email:
        raise UsuariosError("El correo no es válido")
    if len(password) < 8:
        raise UsuariosError("La contraseña debe tener al menos 8 caracteres")

    ensure_perfiles_schema(conn=conn)
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM abogados WHERE LOWER(email)=LOWER(%s) LIMIT 1",
                (email,),
            )
            if cur.fetchone():
                raise UsuariosError("Ya existe un usuario con ese correo")
            perfil_id = _perfil_id_por_codigo(cur, perfil_codigo)
            hashed = hash_password(password)
            cur.execute(
                """
                INSERT INTO abogados (nombre, email, password, perfil_id, activo)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, nombre, email, perfil_id, activo
                """,
                (nombre, email, hashed, perfil_id, bool(activo)),
            )
            row = dict(cur.fetchone())
        if not external:
            conn.commit()
        return row
    except UsuariosError:
        if not external:
            conn.rollback()
        raise
    except Exception:
        if not external:
            conn.rollback()
        raise
    finally:
        if not external and conn:
            conn.release()


def actualizar_usuario(
    user_id: Any,
    *,
    nombre: Optional[str] = None,
    email: Optional[str] = None,
    password: Optional[str] = None,
    perfil_codigo: Optional[str] = None,
    activo: Optional[bool] = None,
    actor_id: Optional[Any] = None,
    conn=None,
) -> dict:
    ensure_perfiles_schema(conn=conn)
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT a.id, a.email, a.nombre, a.activo, p.codigo AS perfil_codigo
                FROM abogados a
                LEFT JOIN perfiles p ON p.id = a.perfil_id
                WHERE a.id=%s
                LIMIT 1
                """,
                (user_id,),
            )
            actual = cur.fetchone()
            if not actual:
                raise UsuariosError("Usuario no encontrado")

            nuevo_nombre = (nombre if nombre is not None else actual["nombre"]) or ""
            nuevo_nombre = str(nuevo_nombre).strip()
            nuevo_email = (
                email if email is not None else actual["email"]
            ) or ""
            nuevo_email = str(nuevo_email).strip().lower()
            if not nuevo_nombre:
                raise UsuariosError("El nombre es obligatorio")
            if not nuevo_email or "@" not in nuevo_email:
                raise UsuariosError("El correo no es válido")

            cur.execute(
                """
                SELECT id FROM abogados
                WHERE LOWER(email)=LOWER(%s) AND id<>%s
                LIMIT 1
                """,
                (nuevo_email, user_id),
            )
            if cur.fetchone():
                raise UsuariosError("Ya existe un usuario con ese correo")

            codigo_actual = permisos.normalizar_perfil(actual.get("perfil_codigo"))
            codigo_nuevo = (
                permisos.normalizar_perfil(perfil_codigo)
                if perfil_codigo is not None
                else codigo_actual
            )
            activo_nuevo = (
                bool(activo) if activo is not None else bool(actual.get("activo", True))
            )

            # No dejar el despacho sin Admin activo.
            pierde_admin = (
                codigo_actual == permisos.PERFIL_ADMIN
                and (
                    codigo_nuevo != permisos.PERFIL_ADMIN
                    or not activo_nuevo
                )
            )
            if pierde_admin and _contar_admins_activos(cur, excluir_id=user_id) < 1:
                raise UsuariosError("Debe quedar al menos un Administrador activo")

            if actor_id is not None and str(actor_id) == str(user_id) and not activo_nuevo:
                raise UsuariosError("No puedes desactivar tu propia cuenta")

            perfil_id = _perfil_id_por_codigo(cur, codigo_nuevo)
            sets = ["nombre=%s", "email=%s", "perfil_id=%s", "activo=%s"]
            params: list[Any] = [nuevo_nombre, nuevo_email, perfil_id, activo_nuevo]

            if password is not None and str(password).strip():
                if len(str(password)) < 8:
                    raise UsuariosError("La contraseña debe tener al menos 8 caracteres")
                sets.append("password=%s")
                params.append(hash_password(str(password)))

            params.append(user_id)
            cur.execute(
                f"UPDATE abogados SET {', '.join(sets)} WHERE id=%s "
                "RETURNING id, nombre, email, perfil_id, activo",
                params,
            )
            row = dict(cur.fetchone())
        if not external:
            conn.commit()
        return row
    except UsuariosError:
        if not external:
            conn.rollback()
        raise
    except Exception:
        if not external:
            conn.rollback()
        raise
    finally:
        if not external and conn:
            conn.release()


def desactivar_usuario(user_id: Any, *, actor_id: Optional[Any] = None, conn=None) -> dict:
    return actualizar_usuario(
        user_id,
        activo=False,
        actor_id=actor_id,
        conn=conn,
    )


def password_hash_valido(stored: Optional[str]) -> bool:
    return is_bcrypt_hash(stored)
