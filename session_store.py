"""Revocación de sesiones (logout / re-login) respaldada por PostgreSQL.

La cookie es un token firmado sin estado; para poder invalidarla en el servidor
se guarda en `sesiones_revocadas` el identificador (sid) de cada sesión cerrada.
La tabla solo contiene sesiones revocadas aún no expiradas y se purga sola.

- La tabla se crea de forma perezosa (CREATE TABLE IF NOT EXISTS), igual que el
  esquema de perfiles. No hay archivo en migrations/ a propósito: la guardia de
  migraciones de start.py abortaría el deploy si quedara pendiente.
- Los sid revocados se cachean en memoria (efecto inmediato en esta instancia).
- Los sid vigentes se cachean `ACTIVE_CACHE_SECONDS` para no consultar Neon en
  cada petición; con varias instancias una revocación tarda como máximo ese
  tiempo en verse en las demás.
- Si la BD falla, `esta_revocada` lanza `SesionNoVerificable` (el middleware
  responde 503): nunca se acepta una sesión sin poder verificarla.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import db

ACTIVE_CACHE_SECONDS = 15
_MAX_CACHE = 5000

_lock = threading.Lock()
_revocadas: dict[str, float] = {}
_vigentes: dict[str, float] = {}
_schema_ok = False


class SesionNoVerificable(RuntimeError):
    pass


DDL = """
CREATE TABLE IF NOT EXISTS sesiones_revocadas (
    sid TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expira_en TIMESTAMPTZ NOT NULL,
    revocada_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sesiones_revocadas_expira ON sesiones_revocadas (expira_en);
"""


def _podar(cache: dict[str, float], ahora: float) -> None:
    if len(cache) < _MAX_CACHE:
        return
    for key in [k for k, exp in cache.items() if exp <= ahora]:
        cache.pop(key, None)
    if len(cache) >= _MAX_CACHE:
        cache.clear()


def ensure_schema(conn=None) -> None:
    global _schema_ok
    external = conn is not None
    if not external:
        conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        _schema_ok = True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if not external:
            conn.release()


def _ejecutar(sql: str, params: tuple, *, fetch: bool = False):
    global _schema_ok
    for intento in (1, 2):
        if not _schema_ok:
            ensure_schema()
        conn = db.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                fila = cur.fetchone() if fetch else None
            conn.commit()
            return fila
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if intento == 1 and getattr(exc, "pgcode", None) == "42P01":
                _schema_ok = False
                continue
            raise
        finally:
            conn.release()
    return None


def revocar(sid: Optional[str], user_id: str, expira_en: int) -> bool:
    """Marca la sesión como cerrada. Devuelve False si no pudo persistirse."""
    if not sid:
        return False
    ahora = time.time()
    with _lock:
        _podar(_revocadas, ahora)
        _revocadas[sid] = float(expira_en)
        _vigentes.pop(sid, None)
    try:
        _ejecutar(
            """
            INSERT INTO sesiones_revocadas (sid, user_id, expira_en)
            VALUES (%s, %s, to_timestamp(%s))
            ON CONFLICT (sid) DO NOTHING
            """,
            (sid, str(user_id), float(expira_en)),
        )
        _ejecutar("DELETE FROM sesiones_revocadas WHERE expira_en < NOW()", ())
        return True
    except Exception:
        return False


def esta_revocada(sid: Optional[str]) -> bool:
    if not sid:
        return True
    ahora = time.time()
    with _lock:
        exp = _revocadas.get(sid)
        if exp is not None and exp > ahora:
            return True
        if _vigentes.get(sid, 0) > ahora:
            return False
    try:
        fila = _ejecutar(
            "SELECT 1 FROM sesiones_revocadas WHERE sid=%s AND expira_en > NOW() LIMIT 1",
            (sid,),
            fetch=True,
        )
    except Exception as exc:
        raise SesionNoVerificable(repr(exc)) from exc
    with _lock:
        if fila:
            _podar(_revocadas, ahora)
            _revocadas[sid] = ahora + 86400
            return True
        _podar(_vigentes, ahora)
        _vigentes[sid] = ahora + ACTIVE_CACHE_SECONDS
    return False


def reset_cache_for_tests() -> None:
    global _schema_ok
    with _lock:
        _revocadas.clear()
        _vigentes.clear()
    _schema_ok = False
