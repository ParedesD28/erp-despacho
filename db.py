"""Acceso robusto a PostgreSQL mediante ThreadedConnectionPool.

Diseñado para:
    Render + FastAPI + Neon PostgreSQL

Objetivos:
- No crear conexiones al importar este módulo.
- Crear el pool de forma perezosa.
- Mantener timeout explícito al conectar.
- No destruir el pool cuando esté temporalmente saturado.
- Reintentar de forma limitada.
- Evitar conexiones PostgreSQL muertas en el pool.
- Evitar transacciones abiertas al devolver conexiones.
- Garantizar un tiempo máximo global para obtener una conexión.
- Mantener compatibilidad con el código actual del ERP.
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from typing import Any

import psycopg2
from psycopg2 import pool as psycopg_pool
from psycopg2.extensions import STATUS_READY, STATUS_IN_TRANSACTION
from psycopg2.pool import PoolError


# =============================================================================
# CONFIGURACIÓN
# =============================================================================

def _env_int(name: str, default: int) -> int:
    """Obtiene un entero desde variables de entorno de forma segura."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Mantengo 15 como máximo porque es el valor de la versión revisada que
# estamos auditando y no quiero introducir un cambio de capacidad innecesario.
_MIN_CONN = max(1, _env_int("DB_POOL_MIN", 1))
_MAX_CONN = max(_MIN_CONN, _env_int("DB_POOL_MAX", 15))

# Timeout del establecimiento de la conexión PostgreSQL.
_CONNECT_TIMEOUT = max(
    1,
    _env_int("DB_CONNECT_TIMEOUT", 8),
)

# Número máximo de intentos para obtener y validar una conexión.
_VALIDATION_ATTEMPTS = max(
    1,
    _env_int("DB_VALIDATION_ATTEMPTS", 3),
)

# Espera entre intentos cuando el pool está saturado o una conexión falla.
_RETRY_DELAY = max(
    0.05,
    float(os.getenv("DB_RETRY_DELAY", "0.3")),
)

# Tiempo máximo TOTAL que get_connection() puede invertir antes de devolver
# un error. Este límite es independiente del connect_timeout de PostgreSQL.
#
# Lo importante es que:
#
#   connect_timeout = límite por conexión
#   acquisition_timeout = límite de toda la operación
#
# Para este ERP propongo 12 segundos como máximo global.
_ACQUISITION_TIMEOUT = max(
    1.0,
    float(os.getenv("DB_ACQUISITION_TIMEOUT", "12")),
)

_DSN = os.getenv("DATABASE_URL")

_LOCK = threading.RLock()

# El pool se crea únicamente cuando una operación realmente necesita DB.
POOL = None

_ORIGINAL_CONNECT = None
_INSTALLED = False


# =============================================================================
# LOGGING
# =============================================================================

def _log(tag: str, msg: str, **detalles):
    """Log consistente y visible en Render."""
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
    """Crea el pool PostgreSQL.

    Importante:
    - Nunca se crea durante el import del módulo.
    - No existe un fallback que elimine connect_timeout.
    - Todas las conexiones usan la misma política.
    """

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

            # Timeout del establecimiento de conexión.
            connect_timeout=_CONNECT_TIMEOUT,

            # TCP keepalive.
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
    """Obtiene el pool existente o lo crea de manera perezosa."""

    global POOL

    with _LOCK:
        if POOL is None:
            POOL = _crear_pool()

        return POOL


# =============================================================================
# UTILIDADES DE CONEXIÓN
# =============================================================================

def _limpiar_transaccion(conn) -> None:
    """Revierte una transacción pendiente antes de reutilizar la conexión."""

    if conn is None:
        return

    if getattr(conn, "closed", 1) != 0:
        return

    if getattr(conn, "status", STATUS_READY) == STATUS_IN_TRANSACTION:
        try:
            conn.rollback()
        except Exception:
            pass


def _is_valid_connection(conn) -> bool:
    """Comprueba que la conexión PostgreSQL siga funcionando."""

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
            "La conexión PostgreSQL no respondió correctamente",
            error=repr(exc),
        )

        return False


# =============================================================================
# CURSOR PROTEGIDO
# =============================================================================

class PooledCursor:
    """Proxy de cursor que marca la conexión si una operación falla."""

    def __init__(
        self,
        owner: "PooledConnection",
        raw_cursor,
    ):
        self._owner = owner
        self._raw = raw_cursor

    def execute(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        try:
            return self._raw.execute(*args, **kwargs)
        except Exception:
            self._owner._failed = True
            raise

    def executemany(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        try:
            return self._raw.executemany(*args, **kwargs)
        except Exception:
            self._owner._failed = True
            raise

    def callproc(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        try:
            return self._raw.callproc(*args, **kwargs)
        except Exception:
            self._owner._failed = True
            raise

    def fetchone(self):
        try:
            return self._raw.fetchone()
        except Exception:
            self._owner._failed = True
            raise

    def fetchmany(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        try:
            return self._raw.fetchmany(*args, **kwargs)
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
        try:
            return self._raw.close()
        except Exception:
            self._owner._failed = True
            raise

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

        try:
            return self._raw.__exit__(
                exc_type,
                exc_value,
                traceback,
            )
        except Exception:
            self._owner._failed = True
            raise

    def __iter__(self):
        try:
            return iter(self._raw)
        except Exception:
            self._owner._failed = True
            raise

    def __getattr__(self, name):
        return getattr(self._raw, name)


# =============================================================================
# CONEXIÓN PROTEGIDA
# =============================================================================

class PooledConnection:
    """Proxy DB-API compatible con psycopg2."""

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
        super().__setattr__("_raw", raw)
        super().__setattr__("_pool", pool)
        super().__setattr__("_returned", False)
        super().__setattr__("_failed", False)

    def cursor(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        try:
            raw_cursor = self._raw.cursor(*args, **kwargs)

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
        """close() devuelve la conexión al pool."""
        self.release()

    def release(self):
        """Devuelve la conexión al pool o la descarta."""

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

                # Nunca devolver una transacción abierta.
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
                    or getattr(raw, "closed", 1) != 0
                )

                if is_bad:
                    _log(
                        "⚠️ [DB POOL]",
                        "Descartando conexión rota del pool",
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
                    "Error devolviendo conexión al pool",
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
        """Propaga configuraciones al objeto psycopg2 real."""

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
# ADQUISICIÓN DE CONEXIONES
# =============================================================================

def get_connection() -> PooledConnection:
    """Obtiene una conexión PostgreSQL válida.

    Hay dos límites:

    1. DB_CONNECT_TIMEOUT:
       cuánto puede tardar una conexión individual.

    2. DB_ACQUISITION_TIMEOUT:
       cuánto puede durar TODA la operación de adquisición.

    Esto evita que una petición HTTP pueda quedarse esperando
    indefinidamente a PostgreSQL.
    """

    global POOL

    ultimo_error = None

    inicio = time.monotonic()
    limite = inicio + _ACQUISITION_TIMEOUT

    intento = 0

    while True:

        intento += 1

        # -------------------------------------------------------------
        # Comprobar límite global ANTES del siguiente intento.
        # -------------------------------------------------------------

        restante = limite - time.monotonic()

        if restante <= 0:
            mensaje = (
                "Tiempo máximo agotado intentando obtener "
                "una conexión PostgreSQL."
            )

            _log(
                "⏱️ [DB TIMEOUT]",
                mensaje,
                intentos=intento - 1,
                timeout_s=_ACQUISITION_TIMEOUT,
            )

            raise TimeoutError(mensaje) from ultimo_error

        _log(
            "🔎 [DB]",
            "Solicitando conexión PostgreSQL",
            intento=intento,
            restante_s=round(restante, 2),
        )

        raw = None
        pool = None

        try:

            # IMPORTANTE:
            # La creación del pool también puede fallar cuando Neon está
            # dormido, por eso permanece dentro del try.
            pool = _obtener_pool()

            # ---------------------------------------------------------
            # Obtener conexión del pool.
            # ---------------------------------------------------------

            raw = pool.getconn()

        except PoolError as exc:

            ultimo_error = exc

            _log(
                "⏳ [DB POOL]",
                "No hay conexiones disponibles; esperando liberación",
                intento=intento,
                error=repr(exc),
            )

            # NO cerrar el pool.
            # NO tocar conexiones de otros usuarios.
            raw = None

        except Exception as exc:

            ultimo_error = exc

            _log(
                "❌ [DB POOL]",
                "Fallo al obtener conexión PostgreSQL",
                intento=intento,
                error=repr(exc),
            )

            raw = None

        # -------------------------------------------------------------
        # Si no obtuvimos conexión, reintentar si todavía queda tiempo.
        # -------------------------------------------------------------

        if raw is None:

            restante = limite - time.monotonic()

            if restante <= 0:
                break

            espera = min(
                _RETRY_DELAY,
                restante,
            )

            if espera > 0:
                time.sleep(espera)

            continue

        # -------------------------------------------------------------
        # Validar la conexión sin mantener el lock global.
        # -------------------------------------------------------------

        try:

            if _is_valid_connection(raw):

                _log(
                    "✅ [DB]",
                    "Conexión PostgreSQL validada",
                    intento=intento,
                    ms=round(
                        (time.monotonic() - inicio) * 1000,
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
                "Error durante la validación de PostgreSQL",
                intento=intento,
                error=repr(exc),
            )

            if raw is not None:

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

        # -------------------------------------------------------------
        # Preparar siguiente intento.
        # -------------------------------------------------------------

        restante = limite - time.monotonic()

        if restante <= 0:
            break

        espera = min(
            _RETRY_DELAY,
            restante,
        )

        if espera > 0:
            time.sleep(espera)

    # =========================================================================
    # FIN POR TIMEOUT GLOBAL
    # =========================================================================

    mensaje = (
        "No fue posible obtener una conexión PostgreSQL válida "
        f"dentro del límite de {_ACQUISITION_TIMEOUT:.1f} segundos."
    )

    _log(
        "❌ [DB FATAL]",
        mensaje,
        ultimo_error=repr(ultimo_error),
        intentos=intento,
    )

    raise TimeoutError(mensaje) from ultimo_error


# =============================================================================
# LIBERACIÓN
# =============================================================================

def release_connection(conn) -> None:
    """Libera una conexión obtenida con get_connection()."""

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
# INTERCEPTOR GLOBAL DE psycopg2.connect()
# =============================================================================

def install_psycopg2_pool() -> None:
    """Hace que psycopg2.connect() pase por el pool central."""

    global _ORIGINAL_CONNECT
    global _INSTALLED

    with _LOCK:

        if _INSTALLED:
            return

        _ORIGINAL_CONNECT = psycopg2.connect

        def pooled_connect(
            *args,
            **kwargs,
        ):
            # Los parámetros recibidos no se utilizan porque el ERP
            # centraliza la conexión en DATABASE_URL.
            return get_connection()

        psycopg2.connect = pooled_connect

        _INSTALLED = True

        _log(
            "✅ [DB POOL]",
            "Interceptor global de psycopg2.connect() instalado",
        )


# =============================================================================
# CIERRE
# =============================================================================

def close_pool() -> None:
    """Cierra el pool completo durante el apagado del proceso."""

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
                "Error cerrando el pool",
                error=repr(exc),
            )


atexit.register(close_pool)
