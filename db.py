"""Acceso robusto a PostgreSQL mediante ThreadedConnectionPool.

Arquitectura:
    FastAPI -> db.get_connection() -> ThreadedConnectionPool -> PostgreSQL/Neon

Objetivos:
- No crear el pool al importar el módulo.
- Crear el pool de forma perezosa.
- Mantener timeout explícito de conexión.
- No destruir el pool ante PoolError por saturación.
- Reintentar de forma limitada.
- Validar conexiones antes de entregarlas.
- Descartar conexiones rotas.
- Limpiar transacciones antes de devolver conexiones.
- Impedir esperas indefinidas durante la adquisición.
- Mantener compatibilidad con start.py.
- NO modificar globalmente psycopg2.connect().
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
    """Lee un entero desde variables de entorno de forma segura."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Lee un float desde variables de entorno de forma segura."""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Mantengo estos valores conservadores para no alterar innecesariamente
# la capacidad actual del ERP durante el diagnóstico.
_MIN_CONN = max(
    1,
    _env_int("DB_POOL_MIN", 1),
)

_MAX_CONN = max(
    _MIN_CONN,
    _env_int("DB_POOL_MAX", 15),
)

# Tiempo máximo para establecer una conexión PostgreSQL individual.
_CONNECT_TIMEOUT = max(
    1.0,
    _env_float("DB_CONNECT_TIMEOUT", 8.0),
)

# Número máximo de intentos de adquisición/validación.
_DB_ATTEMPTS = max(
    1,
    _env_int("DB_VALIDATION_ATTEMPTS", 3),
)

# Espera entre intentos.
_RETRY_DELAY = max(
    0.05,
    _env_float("DB_RETRY_DELAY", 0.3),
)

# Tiempo máximo TOTAL que get_connection() puede permanecer intentando.
#
# Este timeout es independiente de CONNECT_TIMEOUT:
#
# CONNECT_TIMEOUT
#     = límite de una conexión individual.
#
# DB_ACQUISITION_TIMEOUT
#     = límite de toda la operación get_connection().
_ACQUISITION_TIMEOUT = max(
    1.0,
    _env_float("DB_ACQUISITION_TIMEOUT", 12.0),
)

_DSN = os.getenv("DATABASE_URL")

_LOCK = threading.RLock()

# El pool se crea SOLO cuando realmente se solicita una conexión.
POOL = None


# =============================================================================
# LOGGING
# =============================================================================

def _log(tag: str, msg: str, **detalles):
    """Logging consistente para Render."""
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
# CREACIÓN DEL POOL
# =============================================================================

def _crear_pool():
    """Crea el ThreadedConnectionPool nativo de psycopg2.

    No se modifica psycopg2.connect().

    Esto evita la recursión:

        get_connection()
            -> ThreadedConnectionPool()
            -> psycopg2.connect()
            -> get_connection()
            -> ...
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

            # Timeout de conexión real.
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
    """Obtiene el pool actual o lo crea de forma perezosa."""

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


def _is_valid_connection(conn) -> bool:
    """Comprueba que PostgreSQL siga respondiendo."""

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
    """Proxy de cursor.

    Si una operación PostgreSQL falla, marca la conexión como potencialmente
    dañada para que no vuelva al pool como si estuviera sana.
    """

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
            return self._raw.execute(
                *args,
                **kwargs,
            )

        except Exception:
            self._owner._failed = True
            raise

    def executemany(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        try:
            return self._raw.executemany(
                *args,
                **kwargs,
            )

        except Exception:
            self._owner._failed = True
            raise

    def callproc(
        self,
        *args: Any,
        **kwargs: Any,
    ):
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

    def fetchmany(
        self,
        *args: Any,
        **kwargs: Any,
    ):
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
        return getattr(
            self._raw,
            name,
        )


# =============================================================================
# CONEXIÓN PROTEGIDA
# =============================================================================

class PooledConnection:
    """Proxy DB-API compatible con las rutas existentes del ERP."""

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
        # Debemos usar super().__setattr__ porque existe __setattr__
        # delegado hacia la conexión PostgreSQL real.
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

    def cursor(
        self,
        *args: Any,
        **kwargs: Any,
    ):
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
        """Compatibilidad DB-API: devuelve la conexión al pool."""
        self.release()

    def release(self):
        """Devuelve la conexión al pool o la descarta si está dañada."""

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

                # Una conexión con una transacción abierta NO debe
                # regresar al pool.
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
        """Delega atributos de configuración a la conexión real."""

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

    El proceso tiene:

    - timeout por conexión;
    - timeout global;
    - reintentos controlados;
    - validación SELECT 1;
    - manejo seguro de PoolError.

    Importante:
    PoolError NO destruye el pool.
    """

    inicio = time.monotonic()

    limite = (
        inicio
        + _ACQUISITION_TIMEOUT
    )

    ultimo_error = None
    intento = 0

    while True:

        intento += 1

        restante = (
            limite
            - time.monotonic()
        )

        # ---------------------------------------------------------------------
        # LÍMITE GLOBAL
        # ---------------------------------------------------------------------

        if restante <= 0:

            mensaje = (
                "No fue posible obtener una conexión PostgreSQL "
                f"dentro del límite de "
                f"{_ACQUISITION_TIMEOUT:.1f} segundos."
            )

            _log(
                "⏱️ [DB TIMEOUT]",
                mensaje,
                intentos=intento - 1,
                timeout_s=_ACQUISITION_TIMEOUT,
            )

            raise TimeoutError(
                mensaje
            ) from ultimo_error

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

        # ---------------------------------------------------------------------
        # OBTENER CONEXIÓN
        # ---------------------------------------------------------------------

        try:

            # IMPORTANTE:
            # _obtener_pool() está DENTRO del try.
            #
            # Crear el pool puede implicar la primera conexión con Neon.
            # Si falla, la excepción será tratada y podremos reintentar.
            pool = _obtener_pool()

            raw = pool.getconn()

        except PoolError as exc:

            ultimo_error = exc

            _log(
                "⏳ [DB POOL]",
                "No hay conexiones disponibles; esperando liberación",
                intento=intento,
                error=repr(exc),
            )

            # NUNCA hacer closeall() aquí.
            #
            # Otro hilo puede estar utilizando conexiones del mismo pool.
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

        # ---------------------------------------------------------------------
        # SI NO HAY CONEXIÓN, REINTENTAR
        # ---------------------------------------------------------------------

        if raw is None:

            restante = (
                limite
                - time.monotonic()
            )

            if restante <= 0:
                break

            espera = min(
                _RETRY_DELAY,
                restante,
            )

            if espera > 0:
                time.sleep(espera)

            continue

        # ---------------------------------------------------------------------
        # VALIDACIÓN
        # ---------------------------------------------------------------------

        try:

            if _is_valid_connection(raw):

                elapsed_ms = round(
                    (
                        time.monotonic()
                        - inicio
                    ) * 1000,
                    1,
                )

                _log(
                    "✅ [DB]",
                    "Conexión PostgreSQL validada",
                    intento=intento,
                    ms=elapsed_ms,
                )

                return PooledConnection(
                    raw,
                    pool,
                )

            # -----------------------------------------------------------------
            # CONEXIÓN INVÁLIDA
            # -----------------------------------------------------------------

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
                "La conexión PostgreSQL "
                "no superó la validación."
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

        # ---------------------------------------------------------------------
        # SIGUIENTE INTENTO
        # ---------------------------------------------------------------------

        restante = (
            limite
            - time.monotonic()
        )

        if restante <= 0:
            break

        espera = min(
            _RETRY_DELAY,
            restante,
        )

        if espera > 0:
            time.sleep(espera)

    # =========================================================================
    # TIMEOUT FINAL
    # =========================================================================

    mensaje = (
        "No fue posible obtener una conexión PostgreSQL "
        f"válida dentro del límite de "
        f"{_ACQUISITION_TIMEOUT:.1f} segundos."
    )

    _log(
        "❌ [DB FATAL]",
        mensaje,
        ultimo_error=repr(ultimo_error),
        intentos=intento,
    )

    raise TimeoutError(
        mensaje
    ) from ultimo_error


# =============================================================================
# LIBERACIÓN EXTERNA
# =============================================================================

def release_connection(conn) -> None:
    """Libera una conexión obtenida mediante get_connection()."""

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

    IMPORTANTE:
    Esta función deliberadamente NO reemplaza psycopg2.connect().

    El pool se administra explícitamente mediante:
        db.get_connection()

Esto evita la recursión que apareció cuando un pool perezoso intentó
crear conexiones mientras psycopg2.connect() estaba interceptado.
"""

    _log(
        "✅ [DB POOL]",
        "Gestión explícita del pool activada; "
        "psycopg2.connect() no será interceptado",
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
