"""
Cliente HTTP async para la API legal externa del ERP.

Host y timeouts solo vía variables de entorno. Sin URLs fijas en código.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Sequence, TypeVar

import httpx
from fastapi import HTTPException
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# --- Env (documentadas en docs/external-legal-client.md y .env.example) ---
# LEGAL_API_BASE_URL          — base URL obligatoria (sin slash final preferido)
# LEGAL_API_TIMEOUT_S         — timeout global en segundos (default 20)
# LEGAL_API_CONNECT_TIMEOUT_S — timeout de conexión (default = timeout global)
# LEGAL_API_MAX_ATTEMPTS      — intentos totales ante timeout/502/503/504 (default 3)
# LEGAL_API_RETRY_MIN_WAIT_S  — espera mínima backoff (default 0.5)
# LEGAL_API_RETRY_MAX_WAIT_S  — espera máxima backoff (default 8)
# LEGAL_API_USER_AGENT        — User-Agent genérico (default ERPLegalClient/1.0)

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_MIN_WAIT_S = 0.5
DEFAULT_RETRY_MAX_WAIT_S = 8.0
DEFAULT_USER_AGENT = "ERPLegalClient/1.0"
RETRYABLE_STATUS = frozenset({502, 503, 504})

T = TypeVar("T", bound=BaseModel)


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser numérico, recibido: {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser entero, recibido: {raw!r}") from exc


def _base_url_from_env() -> str:
    base = (os.environ.get("LEGAL_API_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail="LEGAL_API_BASE_URL no configurada",
        )
    return base


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


class _IgnoreExtra(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class ProcesoBusquedaResponse(_IgnoreExtra):
    """Resultado normalizado de búsqueda por radicado (id interno + paginación)."""

    id_interno: int
    cantidad_registros: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("cantidad_registros", "cantidadRegistros"),
    )
    registros_pagina: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("registros_pagina", "registrosPagina"),
    )
    cantidad_paginas: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("cantidad_paginas", "cantidadPaginas"),
    )
    pagina: Optional[int] = None


class ProcesoDetalleResponse(_IgnoreExtra):
    juzgado: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("juzgado", "despacho"),
    )
    fecha_creacion: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("fecha_creacion", "fechaProceso", "fechaCreacion"),
    )
    estado: Optional[str] = None
    ponente: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ponente", "ponentes"),
    )
    llave_proceso: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("llave_proceso", "llaveProceso"),
    )
    departamento: Optional[str] = None
    tipo_proceso: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("tipo_proceso", "tipoProceso"),
    )
    clase_proceso: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("clase_proceso", "claseProceso"),
    )
    subclase_proceso: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("subclase_proceso", "subclaseProceso"),
    )
    recurso: Optional[str] = None
    ubicacion: Optional[str] = None
    contenido_radicacion: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("contenido_radicacion", "contenidoRadicacion"),
    )
    fecha_ultima_actuacion: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "fecha_ultima_actuacion",
            "fechaUltimaActuacion",
        ),
    )
    id_interno: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("id_interno", "idProceso"),
    )

    @field_validator("juzgado", "ponente", "estado", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ActuacionResponse(_IgnoreExtra):
    fecha_actuacion: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "fecha_actuacion",
            "fechaActuacion",
        ),
    )
    anotacion: Optional[str] = None
    inicio_termino: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "inicio_termino",
            "fechaInicial",
            "inicioTermino",
        ),
    )
    fin_termino: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "fin_termino",
            "fechaFinal",
            "finTermino",
        ),
    )
    actuacion: Optional[str] = None
    fecha_registro: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("fecha_registro", "fechaRegistro"),
    )
    con_documentos: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices("con_documentos", "conDocumentos"),
    )


class SujetoResponse(_IgnoreExtra):
    tipo_sujeto: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("tipo_sujeto", "tipoSujeto"),
    )
    nombre: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("nombre", "nombreRazonSocial"),
    )
    identificacion: Optional[str] = None


# ---------------------------------------------------------------------------
# Errores / reintentos
# ---------------------------------------------------------------------------


class _RetryableHTTPStatus(Exception):
    """Señal interna: status HTTP reintentable (502/503/504)."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (httpx.ConnectTimeout, httpx.ReadTimeout, _RetryableHTTPStatus),
    )


def _http_exception_after_retries(exc: BaseException) -> HTTPException:
    if isinstance(exc, httpx.ConnectTimeout):
        return HTTPException(
            status_code=504,
            detail="Timeout de conexión con la API legal externa",
        )
    if isinstance(exc, httpx.ReadTimeout):
        return HTTPException(
            status_code=504,
            detail="Timeout de lectura con la API legal externa",
        )
    if isinstance(exc, _RetryableHTTPStatus):
        return HTTPException(
            status_code=502,
            detail=f"API legal externa no disponible (HTTP {exc.status_code})",
        )
    return HTTPException(
        status_code=502,
        detail="Error al consultar la API legal externa",
    )


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


class LegalAPIClient:
    """Cliente async con pool, timeout global y reintentos ante timeout/5xx."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        connect_timeout: Optional[float] = None,
        max_attempts: Optional[int] = None,
        retry_min_wait: Optional[float] = None,
        retry_max_wait: Optional[float] = None,
        user_agent: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = (base_url or _base_url_from_env()).rstrip("/")

        if timeout is None:
            timeout_s = _env_float("LEGAL_API_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        else:
            timeout_s = float(timeout)

        if connect_timeout is None:
            connect_s = _env_float("LEGAL_API_CONNECT_TIMEOUT_S", timeout_s)
        else:
            connect_s = float(connect_timeout)

        if max_attempts is None:
            self._max_attempts = _env_int("LEGAL_API_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
        else:
            self._max_attempts = int(max_attempts)
        if self._max_attempts < 1:
            raise ValueError("LEGAL_API_MAX_ATTEMPTS / max_attempts debe ser >= 1")

        if retry_min_wait is None:
            self._retry_min_wait = _env_float(
                "LEGAL_API_RETRY_MIN_WAIT_S",
                DEFAULT_RETRY_MIN_WAIT_S,
            )
        else:
            self._retry_min_wait = float(retry_min_wait)

        if retry_max_wait is None:
            self._retry_max_wait = _env_float(
                "LEGAL_API_RETRY_MAX_WAIT_S",
                DEFAULT_RETRY_MAX_WAIT_S,
            )
        else:
            self._retry_max_wait = float(retry_max_wait)

        if user_agent is None:
            ua = (os.environ.get("LEGAL_API_USER_AGENT") or "").strip()
        else:
            ua = user_agent.strip()
        ua = ua or DEFAULT_USER_AGENT

        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_s, connect=connect_s),
            headers={
                "Accept": "application/json",
                "User-Agent": ua,
            },
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )

    async def __aenter__(self) -> "LegalAPIClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- API pública ---

    async def consultar_id_por_radicado(self, radicado: str) -> int:
        """GET /Procesos/Consulta/NumRadicado — retorna id interno del proceso."""
        numero = (radicado or "").strip()
        if not numero:
            raise HTTPException(status_code=400, detail="radicado vacío")

        data = await self._request_json(
            "GET",
            "/Procesos/Consulta/NumRadicado",
            params={"numero": numero, "SoloActivos": "false"},
        )
        parsed = self._parse_busqueda(data)
        return parsed.id_interno

    async def obtener_detalle(self, id_interno: int) -> ProcesoDetalleResponse:
        """GET /Proceso/Detalle/{id_interno}."""
        data = await self._request_json("GET", f"/Proceso/Detalle/{id_interno}")
        try:
            if isinstance(data, dict):
                return ProcesoDetalleResponse.model_validate(data)
            raise HTTPException(
                status_code=502,
                detail="Respuesta de detalle con formato inesperado",
            )
        except ValidationError as exc:
            logger.warning("Validación detalle fallida: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="No se pudo interpretar el detalle del proceso",
            ) from exc

    async def obtener_actuaciones(self, id_interno: int) -> list[ActuacionResponse]:
        """GET /Proceso/Actuaciones/{id_interno}."""
        data = await self._request_json("GET", f"/Proceso/Actuaciones/{id_interno}")
        items = self._unwrap_list(data, keys=("actuaciones", "Actuaciones", "data"))
        return self._parse_list(items, ActuacionResponse, "actuaciones")

    async def obtener_sujetos(self, id_interno: int) -> list[SujetoResponse]:
        """GET /Proceso/SujetosProcesales/{id_interno}."""
        data = await self._request_json(
            "GET",
            f"/Proceso/SujetosProcesales/{id_interno}",
        )
        items = self._unwrap_list(
            data,
            keys=("sujetos", "SujetosProcesales", "sujetosProcesales", "data"),
        )
        return self._parse_list(items, SujetoResponse, "sujetos")

    # --- Internos ---

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        last_exc: Optional[BaseException] = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(
                    multiplier=self._retry_min_wait,
                    min=self._retry_min_wait,
                    max=self._retry_max_wait,
                ),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    try:
                        return await self._request_json_once(method, path, params=params)
                    except BaseException as exc:
                        last_exc = exc
                        raise
        except BaseException as exc:
            last_exc = exc

        assert last_exc is not None
        if isinstance(last_exc, HTTPException):
            raise last_exc
        if _is_retryable(last_exc):
            raise _http_exception_after_retries(last_exc) from last_exc
        if isinstance(last_exc, httpx.HTTPError):
            logger.exception("Error HTTP con API legal externa")
            raise HTTPException(
                status_code=502,
                detail="Error de red con la API legal externa",
            ) from last_exc
        raise last_exc

    async def _request_json_once(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            response = await self._client.request(method, path, params=params)
        except (httpx.ConnectTimeout, httpx.ReadTimeout):
            raise
        except httpx.HTTPError as exc:
            logger.exception("Fallo de red en %s %s", method, path)
            raise HTTPException(
                status_code=502,
                detail="Error de red con la API legal externa",
            ) from exc

        if response.status_code in RETRYABLE_STATUS:
            raise _RetryableHTTPStatus(
                response.status_code,
                f"HTTP {response.status_code}",
            )

        if response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="Recurso no encontrado en la API legal externa",
            )

        if response.status_code >= 400:
            detail = f"API legal externa respondió HTTP {response.status_code}"
            try:
                body = response.json()
                if isinstance(body, dict):
                    msg = body.get("message") or body.get("detail") or body.get("error")
                    if msg:
                        detail = f"{detail}: {msg}"
            except Exception:  # noqa: BLE001 — cuerpo no JSON
                pass
            raise HTTPException(status_code=502, detail=detail)

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="Respuesta no JSON de la API legal externa",
            ) from exc

    def _parse_busqueda(self, data: Any) -> ProcesoBusquedaResponse:
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=502,
                detail="Respuesta de búsqueda con formato inesperado",
            )

        pag = data.get("paginacion") or data.get("paginación") or {}
        if not isinstance(pag, dict):
            pag = {}

        id_interno = self._extract_id_interno(data)
        if id_interno is None:
            raise HTTPException(
                status_code=404,
                detail="No se encontró proceso para el radicado indicado",
            )

        try:
            return ProcesoBusquedaResponse(
                id_interno=id_interno,
                cantidad_registros=pag.get(
                    "cantidadRegistros",
                    data.get("cantidadRegistros"),
                ),
                registros_pagina=pag.get(
                    "registrosPagina",
                    data.get("registrosPagina"),
                ),
                cantidad_paginas=pag.get(
                    "cantidadPaginas",
                    data.get("cantidadPaginas"),
                ),
                pagina=pag.get("pagina", data.get("pagina")),
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=502,
                detail="No se pudo interpretar la búsqueda por radicado",
            ) from exc

    @staticmethod
    def _extract_id_interno(data: dict[str, Any]) -> Optional[int]:
        for key in ("id_interno", "idProceso"):
            if key in data and data[key] is not None:
                try:
                    return int(data[key])
                except (TypeError, ValueError):
                    pass

        procesos = data.get("procesos") or data.get("Procesos") or data.get("data")
        if isinstance(procesos, list) and procesos:
            first = procesos[0]
            if isinstance(first, dict):
                for key in ("id_interno", "idProceso", "id"):
                    if first.get(key) is not None:
                        try:
                            return int(first[key])
                        except (TypeError, ValueError):
                            continue
            elif isinstance(first, (int, str)):
                try:
                    return int(first)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _unwrap_list(data: Any, *, keys: Sequence[str]) -> list[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, list):
                    return value
            if any(
                k in data
                for k in ("fechaActuacion", "tipoSujeto", "nombreRazonSocial")
            ):
                return [data]
        return []

    @staticmethod
    def _parse_list(
        items: list[Any],
        model: type[T],
        label: str,
    ) -> list[T]:
        out: list[T] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                out.append(model.model_validate(item))
            except ValidationError as exc:
                logger.warning("Ítem de %s omitido por validación: %s", label, exc)
        return out
