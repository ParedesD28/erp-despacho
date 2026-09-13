"""Acceso único y obligatorio a PostgreSQL mediante ThreadedConnectionPool."""
from __future__ import annotations

import atexit
import os
import threading
from typing import Any

from psycopg2 import pool as psycopg_pool


_MIN_CONN = int(os.getenv("DB_POOL_MIN", "1"))
_MAX_CONN = int(os.getenv("DB_POOL_MAX", "20"))
_DSN = os.getenv("DATABASE_URL")

if not _DSN:
    raise RuntimeError("DATABASE_URL no está configurada")

POOL = psycopg_pool.ThreadedConnectionPool(_MIN_CONN, _MAX_CONN, dsn=_DSN)
_LOCK = threading.RLock()
_ORIGINAL_CONNECT = None
_INSTALLED = False


class PooledConnection:
    """Proxy DB-API que devuelve la conexión al pool en lugar de cerrarla."""

    def __init__(self, raw):
        self._raw = raw
        self._returned = False

    def cursor(self, *args: Any, **kwargs: Any):
        return self._raw.cursor(*args, **kwargs)

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def close(self):
        self.release()

    def release(self):
        if self._returned:
            return
        self._returned = True
        try:
            if self._raw.closed:
                POOL.putconn(self._raw, close=True)
                return
            POOL.putconn(self._raw)
        except Exception:
            try:
                POOL.putconn(self._raw, close=True)
            except Exception:
                pass

    @property
    def closed(self):
        return self._raw.closed

    @property
    def status(self):
        return self._raw.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type:
                self._raw.rollback()
            else:
                self._raw.commit()
        finally:
            self.release()
        return False

    def __getattr__(self, name):
        return getattr(self._raw, name)


def get_connection() -> PooledConnection:
    return PooledConnection(POOL.getconn())


def release_connection(conn) -> None:
    if conn is None:
        return
    if isinstance(conn, PooledConnection):
        conn.release()
        return
    try:
        POOL.putconn(conn)
    except Exception:
        try:
            POOL.putconn(conn, close=True)
        except Exception:
            pass


def install_psycopg2_pool() -> None:
    """Hace que psycopg2.connect() nunca abra conexiones fuera del pool."""
    global _ORIGINAL_CONNECT, _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        import psycopg2

        _ORIGINAL_CONNECT = psycopg2.connect

        def pooled_connect(*args, **kwargs):
            # El ERP usa una única DATABASE_URL. Se conserva la firma de connect
            # para compatibilidad con pandas y módulos legacy.
            return get_connection()

        psycopg2.connect = pooled_connect
        _INSTALLED = True


def close_pool() -> None:
    try:
        POOL.closeall()
    except Exception:
        pass


atexit.register(close_pool)
