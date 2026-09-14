"""Acceso robusto a PostgreSQL mediante ThreadedConnectionPool con reconexión y logs activos."""
from __future__ import annotations

import atexit
import os
import threading
import time
from typing import Any

import psycopg2
from psycopg2 import pool as psycopg_pool
from psycopg2.extensions import STATUS_READY, STATUS_IN_TRANSACTION

def _log(tag: str, msg: str, **detalles):
    extra = " | " + " ".join(f"{k}={v}" for k, v in detalles.items()) if detalles else ""
    hora = time.strftime("%H:%M:%S")
    print(f"[{hora} UTC] {tag} {msg}{extra}", flush=True)

_MIN_CONN = int(os.getenv("DB_POOL_MIN", "1"))
_MAX_CONN = int(os.getenv("DB_POOL_MAX", "20"))
_DSN = os.getenv("DATABASE_URL")

_LOCK = threading.RLock()
_ORIGINAL_CONNECT = None
_INSTALLED = False


def _crear_pool():
    if not _DSN:
        raise RuntimeError("DATABASE_URL no está configurada")
    _log("🔌 [DB POOL]", f"Inicializando pool (min={_MIN_CONN}, max={_MAX_CONN})...")
    try:
        return psycopg_pool.ThreadedConnectionPool(
            _MIN_CONN,
            _MAX_CONN,
            dsn=_DSN,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
    except Exception as e:
        _log("⚠️ [DB POOL]", "Fallo al crear pool con keepalives, intentando básico", error=str(e))
        return psycopg_pool.ThreadedConnectionPool(_MIN_CONN, _MAX_CONN, dsn=_DSN)


POOL = _crear_pool()


def _is_valid_connection(conn) -> bool:
    """Valida rápidamente que el socket SSL con Neon siga respondiendo."""
    if conn is None or getattr(conn, "closed", 1) != 0:
        return False
    try:
        if getattr(conn, "status", STATUS_READY) == STATUS_IN_TRANSACTION:
            conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        if getattr(conn, "status", STATUS_READY) == STATUS_IN_TRANSACTION:
            conn.rollback()
        return True
    except Exception:
        return False


class PooledConnection:
    """Proxy DB-API que devuelve la conexión limpia al pool."""

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
                # Asegura que nunca quede una transacción colgada en el pool (evita bloqueos de tablas)
                if getattr(self._raw, "status", STATUS_READY) == STATUS_IN_TRANSACTION:
                    try:
                        self._raw.rollback()
                    except Exception:
                        self._failed = True

                is_bad = (
                    self._failed
                    or getattr(self._raw, "closed", 1) != 0
                )
                if is_bad:
                    _log("⚠️ [DB POOL]", "Descartando conexión rota/cerrada del pool")
                    POOL.putconn(self._raw, close=True)
                else:
                    POOL.putconn(self._raw)
            except Exception as e:
                _log("⚠️ [DB POOL]", f"Error al devolver conexión: {e}")
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
                try:
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
    """Obtiene una conexión viva, validada y limpia del pool."""
    global POOL
    with _LOCK:
        if not POOL:
            POOL = _crear_pool()

        for intento in range(3):
            try:
                raw = POOL.getconn()
            except Exception as e:
                _log("⚠️ [DB POOL]", f"Error solicitando conexión (intento {intento+1}): {e}")
                time.sleep(0.1)
                continue

            if _is_valid_connection(raw):
                return PooledConnection(raw)

            _log("⚠️ [DB POOL]", "Conexión inactiva detectada (reemplazando por una fresca)...")
            try:
                POOL.putconn(raw, close=True)
            except Exception:
                pass

        _log("🔄 [DB POOL]", "Reiniciando pool de conexiones completo...")
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
