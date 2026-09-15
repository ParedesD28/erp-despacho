"""Acceso robusto a PostgreSQL mediante ThreadedConnectionPool.

Arquitectura:
    FastAPI -> db.get_connection() -> ThreadedConnectionPool -> PostgreSQL/Neon

Principios:
- No conectar a PostgreSQL al importar el módulo.
- No modificar globalmente psycopg2.connect().
- Crear el pool de forma perezosa.
- connect_timeout siempre entero para libpq/psycopg2.
- PoolError nunca destruye el pool.
- Validar conexiones antes de entregarlas.
- Descartar conexiones rotas.
- Limpiar transacciones antes de devolver conexiones.
- Limitar el tiempo total de adquisición.
- Permitir que Render/FastAPI arranque aunque Neon esté temporalmente inaccesible.
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from typing import Any

from psycopg2 import pool as psycopg_pool
from psycopg2.extensions import STATUS_READY, STATUS_IN_TRANSACTION
from psycopg2.pool import PoolError


# =============================================================================
# CONFIGURACIÓN
# =============================================================================

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_MIN_CONN = max(
    1,
    _env_int("DB_POOL_MIN", 1),
)

_MAX_CONN = max(
    _MIN_CONN,
    _env_int("DB_POOL_MAX", 15),
)

# IMPORTANTE:
# psycopg2/libpq espera este parámetro como entero.
_CONNECT_TIMEOUT = max(
    1,
    _env_int("DB_CONNECT_TIMEOUT", 8),
)

_MAX_ATTEMPTS = max(
    1,
    _env_int("DB_MAX_ATTEMPTS", 3),
)

_RETRY_DELAY = max(
    0.05,
    _env_float("DB_RETRY_DELAY", 0.3),
)

# Este valor sí puede ser float porque es control interno de Python.
_ACQUISITION_TIMEOUT = max(
    1.0,
    _env_float("DB_ACQUISITION_TIMEOUT", 12.0),
)

_DSN = os.getenv("DATABASE_URL")

_LOCK = threading.RLock()

# El pool se crea exclusivamente cuando alguna operación necesita PostgreSQL.
POOL = None


# =============================================================================
# LOGGING
# =============================================================================

def _log(tag: str, msg: str, **detalles):
    extra = (
        " | "
        + " ".join(f"{k}={v}" for k, v in detalles.items())
        if detalles
        else ""
    )

    hora = time.strftime("%H:%M:%S UTC")

    print(
        f"[{hora}] {tag} {msg}{extra}",
        flush=True,
    )


# =============================================================================
# CREACIÓN PEREZOSA DEL POOL
# =============================================================================

def _crear_pool():
    if not _DSN:
        raise RuntimeError(
            "DATABASE_URL no está configurada"
        )

    _log(
        "🔌 [DB POOL]",
        "Creando pool PostgreSQL...",
        min=_MIN_CONN,
        max=_MAX_CONN,
        connect_timeout=_CONNECT_TIMEOUT,
    )

    try:

        nuevo_pool = psycopg_pool.ThreadedConnectionPool(
            minconn=_MIN_CONN,
            maxconn=_MAX_CONN,
            dsn=_DSN,

            # ESTE valor es int.
            connect_timeout=_CONNECT_TIMEOUT,

            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )

        _log(
            "✅ [DB POOL]",
            "Pool PostgreSQL creado correctamente",
            min=_MIN_CONN,
            max=_MAX_CONN,
        )

        return nuevo_pool

    except Exception as exc:

        _log(
            "❌ [DB POOL]",
            "No fue posible crear el pool PostgreSQL",
            error=repr(exc),
        )

        raise


def _obtener_pool():
    global POOL

    with _LOCK:

        if POOL is None:
            POOL = _crear_pool()

        return POOL


def get_pool():
    """Obtiene el pool real para tareas internas de mantenimiento.

    No crea el pool durante el import.
    Si Neon no está disponible, propaga el error al llamador.
    """

    return _obtener_pool()


# =============================================================================
# TRANSACCIONES
# =============================================================================

def _limpiar_transaccion(conn) -> None:

    if conn is None:
        return

    if getattr(conn, "closed", 1) != 0:
        return

    if (
        getattr(
            conn,
            "status",
            STATUS_READY,
        )
        == STATUS_IN_TRANSACTION
    ):

        try:
            conn.rollback()

        except Exception:
            pass


# =============================================================================
# VALIDACIÓN
# =============================================================================

def _is_valid_connection(conn) -> bool:

    if conn is None:
        return False

    if getattr(conn, "closed", 1) != 0:
        return False

    try:

        _limpiar_transaccion(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT 1;")

        _limpiar_transaccion(conn)

        return True

    except Exception as exc:

        _log(
            "⚠️ [DB CHECK]",
            "La conexión PostgreSQL no respondió",
            error=repr(exc),
        )

        return False


# =============================================================================
# CURSOR
# =============================================================================

class PooledCursor:

    def __init__(
        self,
        owner: "PooledConnection",
        raw_cursor,
    ):
        self._owner = owner
        self._raw = raw_cursor

    def execute(self, *args: Any, **kwargs: Any):
        try:
            return self._raw.execute(
                *args,
                **kwargs,
            )
        except Exception:
            self._owner._failed = True
            raise

    def executemany(self, *args: Any, **kwargs: Any):
        try:
            return self._raw.executemany(
                *args,
                **kwargs,
            )
        except Exception:
            self._owner._failed = True
            raise

    def callproc(self, *args: Any, **kwargs: Any):
        try:
            return self._raw.callproc(
                *args,
                **kwargs,
            )
        except Exception:
            self._owner._failed = True
            raise

    def fetchone(self):
        try:
            return self._raw.fetchone()
        except Exception:
            self._owner._failed = True
            raise

    def fetchmany(self, *args: Any, **kwargs: Any):
        try:
            return self._raw.fetchmany(
                *args,
                **kwargs,
            )
        except Exception:
            self._owner._failed = True
            raise

    def fetchall(self):
        try:
            return self._raw.fetchall()
        except Exception:
            self._owner._failed = True
            raise

    def close(self):
        return self._raw.close()

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):

        if exc_type:
            self._owner._failed = True

        return self._raw.__exit__(
            exc_type,
            exc_value,
            traceback,
        )

    def __iter__(self):
        return iter(self._raw)

    def __getattr__(self, name):
        return getattr(
            self._raw,
            name,
        )


# =============================================================================
# CONEXIÓN
# =============================================================================

class PooledConnection:

    _INTERNAL_ATTRIBUTES = {
        "_raw",
        "_pool",
        "_returned",
        "_failed",
    }

    def __init__(
        self,
        raw,
        pool,
    ):

        super().__setattr__(
            "_raw",
            raw,
        )

        super().__setattr__(
            "_pool",
            pool,
        )

        super().__setattr__(
            "_returned",
            False,
        )

        super().__setattr__(
            "_failed",
            False,
        )

    def cursor(self, *args: Any, **kwargs: Any):

        try:

            raw_cursor = self._raw.cursor(
                *args,
                **kwargs,
            )

            return PooledCursor(
                self,
                raw_cursor,
            )

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
            raise

    def close(self):
        self.release()

    def release(self):

        if self._returned:
            return

        self._returned = True

        pool = self._pool
        raw = self._raw

        if pool is None:

            try:
                raw.close()

            except Exception:
                pass

            return

        with _LOCK:

            try:

                if (
                    getattr(
                        raw,
                        "status",
                        STATUS_READY,
                    )
                    == STATUS_IN_TRANSACTION
                ):

                    try:
                        raw.rollback()

                    except Exception:
                        self._failed = True

                is_bad = (
                    self._failed
                    or getattr(
                        raw,
                        "closed",
                        1,
                    ) != 0
                )

                if is_bad:

                    _log(
                        "⚠️ [DB POOL]",
                        "Descartando conexión rota",
                    )

                    pool.putconn(
                        raw,
                        close=True,
                    )

                    return

                pool.putconn(raw)

            except Exception as exc:

                _log(
                    "⚠️ [DB POOL]",
                    "Error devolviendo conexión",
                    error=repr(exc),
                )

                try:
                    pool.putconn(
                        raw,
                        close=True,
                    )

                except Exception:

                    try:
                        raw.close()
                    except Exception:
                        pass

    @property
    def closed(self):
        return getattr(
            self._raw,
            "closed",
            1,
        )

    @property
    def status(self):
        return getattr(
            self._raw,
            "status",
            -1,
        )

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):

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
        return getattr(
            self._raw,
            name,
        )

    def __setattr__(
        self,
        name,
        value,
    ):

        if name in self._INTERNAL_ATTRIBUTES:

            super().__setattr__(
                name,
                value,
            )

            return

        setattr(
            self._raw,
            name,
            value,
        )


# =============================================================================
# ADQUISICIÓN
# =============================================================================

def get_connection() -> PooledConnection:

    inicio = time.monotonic()

    ultimo_error = None

    for intento in range(
        1,
        _MAX_ATTEMPTS + 1,
    ):

        transcurrido = (
            time.monotonic()
            - inicio
        )

        restante = (
            _ACQUISITION_TIMEOUT
            - transcurrido
        )

        if restante <= 0:

            break

        _log(
            "🔎 [DB]",
            "Solicitando conexión PostgreSQL",
            intento=intento,
            restante_s=round(
                restante,
                2,
            ),
        )

        raw = None
        pool = None

        try:

            # Crear el pool y obtener la conexión están
            # dentro del try porque cualquiera de los dos
            # puede fallar al despertar Neon.
            pool = _obtener_pool()

            raw = pool.getconn()

        except PoolError as exc:

            ultimo_error = exc

            _log(
                "⏳ [DB POOL]",
                "Pool temporalmente sin conexiones disponibles",
                intento=intento,
                error=repr(exc),
            )

        except Exception as exc:

            ultimo_error = exc

            _log(
                "❌ [DB POOL]",
                "Fallo al obtener conexión PostgreSQL",
                intento=intento,
                error=repr(exc),
            )

        if raw is None:

            restante = (
                _ACQUISITION_TIMEOUT
                - (
                    time.monotonic()
                    - inicio
                )
            )

            if restante <= 0:
                break

            time.sleep(
                min(
                    _RETRY_DELAY,
                    restante,
                )
            )

            continue

        try:

            if _is_valid_connection(raw):

                _log(
                    "✅ [DB]",
                    "Conexión PostgreSQL validada",
                    intento=intento,
                    ms=round(
                        (
                            time.monotonic()
                            - inicio
                        ) * 1000,
                        1,
                    ),
                )

                return PooledConnection(
                    raw,
                    pool,
                )

            _log(
                "⚠️ [DB]",
                "Conexión inválida; descartando",
                intento=intento,
            )

            try:

                pool.putconn(
                    raw,
                    close=True,
                )

            except Exception:

                try:
                    raw.close()
                except Exception:
                    pass

            raw = None

            ultimo_error = RuntimeError(
                "La conexión PostgreSQL no superó la validación."
            )

        except Exception as exc:

            ultimo_error = exc

            _log(
                "❌ [DB]",
                "Error durante validación de PostgreSQL",
                intento=intento,
                error=repr(exc),
            )

            try:

                pool.putconn(
                    raw,
                    close=True,
                )

            except Exception:

                try:
                    raw.close()
                except Exception:
                    pass

        restante = (
            _ACQUISITION_TIMEOUT
            - (
                time.monotonic()
                - inicio
            )
        )

        if restante <= 0:
            break

        time.sleep(
            min(
                _RETRY_DELAY,
                restante,
            )
        )

    mensaje = (
        "No fue posible obtener una conexión PostgreSQL "
        f"válida después de {_MAX_ATTEMPTS} intentos "
        f"o {_ACQUISITION_TIMEOUT:.1f} segundos."
    )

    _log(
        "❌ [DB FATAL]",
        mensaje,
        ultimo_error=repr(ultimo_error),
        intentos=_MAX_ATTEMPTS,
    )

    raise TimeoutError(
        mensaje
    ) from ultimo_error


# =============================================================================
# LIBERACIÓN
# =============================================================================

def release_connection(conn) -> None:

    if conn is None:
        return

    if isinstance(
        conn,
        PooledConnection,
    ):

        conn.release()
        return

    with _LOCK:

        pool = POOL

        if pool is None:

            try:
                conn.close()
            except Exception:
                pass

            return

        try:

            is_bad = (
                getattr(
                    conn,
                    "closed",
                    1,
                )
                != 0
            )

            pool.putconn(
                conn,
                close=is_bad,
            )

        except Exception:

            try:
                pool.putconn(
                    conn,
                    close=True,
                )

            except Exception:

                try:
                    conn.close()
                except Exception:
                    pass


# =============================================================================
# COMPATIBILIDAD CON start.py
# =============================================================================

def install_psycopg2_pool() -> None:
    """Compatibilidad con start.py.

    NO modifica psycopg2.connect().

    Esto existe porque start.py ya llama a esta función,
    pero el pool ahora se administra explícitamente mediante
    get_connection()/get_pool().
    """

    _log(
        "✅ [DB POOL]",
        "Pool PostgreSQL administrado explícitamente; "
        "sin interceptor global de psycopg2.connect()",
    )


# =============================================================================
# CIERRE
# =============================================================================

def close_pool() -> None:

    global POOL

    with _LOCK:

        if POOL is None:
            return

        pool = POOL

        POOL = None

        try:

            pool.closeall()

            _log(
                "🧹 [DB POOL]",
                "Pool PostgreSQL cerrado",
            )

        except Exception as exc:

            _log(
                "⚠️ [DB POOL]",
                "Error cerrando pool",
                error=repr(exc),
            )


atexit.register(close_pool)
