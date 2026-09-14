"""Acceso único y obligatorio a PostgreSQL mediante ThreadedConnectionPool con reconexión automática y KeepAlive."""
from __future__ import annotations

import atexit
import os
import threading
from typing import Any

import psycopg2
from psycopg2 import pool as psycopg_pool
from psycopg2.extensions import STATUS_READY

_MIN_CONN = int(os.getenv("DB_POOL_MIN", "1"))
_MAX_CONN = int(os.getenv("DB_POOL_MAX", "20"))
_DSN = os.getenv("DATABASE_URL")

_LOCK = threading.RLock()
_ORIGINAL_CONNECT = None
_INSTALLED = False


def _crear_pool():
    if not _DSN:
        raise RuntimeError("DATABASE_URL no está configurada")
    # Activa keepalives para evitar desconexiones silenciosas de SSL en Neon y Render
    try:
        return psycopg_pool.ThreadedConnectionPool(
            _MIN_CONN,
            _MAX_CONN,
            dsn=_DSN,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
    except Exception:
        return psycopg_pool.ThreadedConnectionPool(_MIN_CONN, _MAX_CONN, dsn=_DSN)


POOL = _crear_pool()


def _is_valid_connection(conn) -> bool:
    """Verifica si la conexión física con PostgreSQL / Neon sigue activa."""
    if conn is None or getattr(conn, "closed", 1) != 0:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        return True
    except Exception:
        return False


class PooledConnection:
    """Proxy DB-API que devuelve la conexión al pool en lugar de cerrarla."""

    def __init__(self, raw):
        self._raw = raw
        self._returned = False
        self._failed = False

    def cursor(self, *args: Any, **kwargs: Any):
        try:
            return self._raw.cursor(*args, **kwargs)
        except Exception:
            self._failed = True
            raise

    def commit(self):
        try:
            return self._raw.commit()
        except Exception:
            self._failed = True
            raise

    def rollback(self):
        try:
            return self._raw.rollback()
        except Exception:
            self._failed = True

    def close(self):
        self.release()

    def release(self):
        if self._returned:
            return
        self._returned = True
        with _LOCK:
            if not POOL:
                return
            try:
                is_bad = (
                    self._failed
                    or getattr(self._raw, "closed", 1) != 0
                    or getattr(self._raw, "status", STATUS_READY) != STATUS_READY
                )
                if is_bad:
                    POOL.putconn(self._raw, close=True)
                else:
                    POOL.putconn(self._raw)
            except Exception:
                try:
                    POOL.putconn(self._raw, close=True)
                except Exception:
                    pass

    @property
    def closed(self):
        return getattr(self._raw, "closed", 1)

    @property
    def status(self):
        return getattr(self._raw, "status", -1)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type:
                self._failed = True
                try:
                    self._raw.rollback()
                except Exception:
                    pass
            else:
                self._raw.commit()
        except Exception:
            self._failed = True
            raise
        finally:
            self.release()
        return False

    def __getattr__(self, name):
        return getattr(self._raw, name)


def get_connection() -> PooledConnection:
    """Obtiene una conexión viva y validada del pool. Si la conexión caducó por inactividad, se descarta y reconecta."""
    global POOL
    with _LOCK:
        if not POOL:
            POOL = _crear_pool()

        for _ in range(min(_MAX_CONN, 5)):
            try:
                raw = POOL.getconn()
            except Exception:
                break

            if _is_valid_connection(raw):
                return PooledConnection(raw)

            # Si la conexión se cayó (ej: SSL cerrado por reposo de Neon), se destruye del pool
            try:
                POOL.putconn(raw, close=True)
            except Exception:
                pass

        # Si no había conexiones vivas en el pool, forzar una conexión nueva
        try:
            raw = POOL._connect()
            return PooledConnection(raw)
        except Exception:
            close_pool()
            POOL = _crear_pool()
            raw = POOL.getconn()
            return PooledConnection(raw)


def release_connection(conn) -> None:
    if conn is None:
        return
    if isinstance(conn, PooledConnection):
        conn.release()
        return
    with _LOCK:
        if not POOL:
            return
        try:
            is_bad = getattr(conn, "closed", 1) != 0
            POOL.putconn(conn, close=is_bad)
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

        _ORIGINAL_CONNECT = psycopg2.connect

        def pooled_connect(*args, **kwargs):
            return get_connection()

        psycopg2.connect = pooled_connect
        _INSTALLED = True


def close_pool() -> None:
    global POOL
    with _LOCK:
        if POOL:
            try:
                POOL.closeall()
            except Exception:
                pass
            POOL = None


atexit.register(close_pool)
