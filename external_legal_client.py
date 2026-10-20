"""
Cliente HTTP async para la API legal externa del ERP.

Host y timeouts solo vía variables de entorno. Sin URLs fijas en código.
"""
from __future__ import annotations

import json
import logging
import os
import re
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
# LEGAL_API_EXTRA_HEADERS     — objeto JSON opcional de headers extra (Authorization, api-key, …)

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_MIN_WAIT_S = 0.5
DEFAULT_RETRY_MAX_WAIT_S = 8.0
DEFAULT_USER_AGENT = "ERPLegalClient/1.0"
# Negociación flexible: varias APIs legales rechazan Accept estricto application/json (HTTP 406).
DEFAULT_ACCEPT = "application/json, text/plain, */*"
DEFAULT_ACCEPT_LANGUAGE = "es-CO,es;q=0.9"
RETRYABLE_STATUS = frozenset({502, 503, 504})
_ERROR_BODY_SNIPPET_LIMIT = 240
_NON_JSON_SNIPPET_LIMIT = 300
# Claves candidatas para id interno en búsqueda por radicado (snake / camel / genéricas).
_ID_INTERNO_KEYS: tuple[str, ...] = (
    "id_interno",
    "idProceso",
    "id_proceso",
    "IdProceso",
    "IDProceso",
    "id",
)
_PROCESOS_LIST_KEYS: tuple[str, ...] = (
    "procesos",
    "Procesos",
    "data",
    "resultados",
    "Resultados",
    "items",
    "Items",
    "results",
)

T = TypeVar("T", bound=BaseModel)

_HTML_OPEN_RE = re.compile(
    r"^\s*(<!doctype\s+html\b|<html\b|<head\b|<body\b)",
    re.IGNORECASE,
)


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


def _extra_headers_from_env() -> dict[str, str]:
    """Parsea LEGAL_API_EXTRA_HEADERS (objeto JSON). Sin secretos en código."""
    raw = (os.environ.get("LEGAL_API_EXTRA_HEADERS") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "LEGAL_API_EXTRA_HEADERS debe ser un objeto JSON válido "
            '(ej. {"Authorization":"Bearer …"})'
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            "LEGAL_API_EXTRA_HEADERS debe ser un objeto JSON (no array ni escalar)"
        )
    out: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                "LEGAL_API_EXTRA_HEADERS: cada clave debe ser un string no vacío"
            )
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(
                f"LEGAL_API_EXTRA_HEADERS: valor de {key!r} debe ser escalar"
            )
        out[key.strip()] = str(value)
    return out


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


def _safe_response_snippet(
    response: httpx.Response,
    *,
    limit: int = _ERROR_BODY_SNIPPET_LIMIT,
) -> str:
    """Texto corto del cuerpo para depurar 406/errores; sin datos binarios largos."""
    try:
        text = (response.text or "").strip()
    except Exception:  # noqa: BLE001 — body ilegible
        return ""
    if not text:
        return ""
    # Colapsar whitespace / saltos (HTML o plaintext de gateways).
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _content_type(response: httpx.Response) -> str:
    return (response.headers.get("content-type") or "").strip() or "(vacío)"


def _looks_like_html(response: httpx.Response) -> bool:
    """Detecta portal/login HTML. No scrapea: solo clasifica para fallar claro."""
    ct = _content_type(response).lower()
    if "text/html" in ct or "application/xhtml" in ct:
        return True
    try:
        text = response.text or ""
    except Exception:  # noqa: BLE001
        return False
    return bool(_HTML_OPEN_RE.match(text))


def _detail_non_json(response: httpx.Response) -> str:
    """Mensaje UI: status, Content-Type y ~300 chars del cuerpo (seguro)."""
    ct = _content_type(response)
    snippet = _safe_response_snippet(response, limit=_NON_JSON_SNIPPET_LIMIT)
    if _looks_like_html(response):
        base = (
            "La API devolvió HTML (revisa LEGAL_API_BASE_URL: debe ser la base "
            "REST, no el portal web)"
        )
    else:
        base = "Respuesta no JSON de la API legal externa"
    parts = [f"{base}. HTTP {response.status_code}, Content-Type: {ct}"]
    if snippet:
        parts.append(f"Cuerpo: {snippet}")
    return ". ".join(parts) if snippet else parts[0]


def _detail_for_http_error(response: httpx.Response) -> str:
    detail = f"API legal externa respondió HTTP {response.status_code}"
    structured: Optional[str] = None
    try:
        body = response.json()
        if isinstance(body, dict):
            msg = body.get("message") or body.get("detail") or body.get("error")
            if msg is not None:
                structured = str(msg).strip() or None
                if structured:
                    detail = f"{detail}: {structured}"
    except Exception:  # noqa: BLE001 — cuerpo no JSON
        pass

    # 406 Not Acceptable / cuerpo no estructurado: snippet + Content-Type.
    if not structured and (
        response.status_code == 406 or _looks_like_html(response)
    ):
        ct = _content_type(response)
        snippet = _safe_response_snippet(response)
        extra = f"Content-Type: {ct}"
        if snippet:
            extra = f"{extra}. Cuerpo: {snippet}"
        detail = f"{detail}. {extra}"
    return detail


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

        headers: dict[str, str] = {
            "Accept": DEFAULT_ACCEPT,
            "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
            "User-Agent": ua,
        }
        # Extra headers (Authorization / api-key) sobrescriben defaults si coincide la clave.
        headers.update(_extra_headers_from_env())

        self._owns_client = client is None
        # Sin Content-Type por defecto: los GET no deben forzar application/json.
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_s, connect=connect_s),
            headers=headers,
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
            raise HTTPException(
                status_code=502,
                detail=_detail_for_http_error(response),
            )

        if not response.content:
            return {}

        if _looks_like_html(response):
            raise HTTPException(
                status_code=502,
                detail=_detail_non_json(response),
            )

        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail=_detail_non_json(response),
            ) from exc

    def _parse_busqueda(self, data: Any) -> ProcesoBusquedaResponse:
        # Array raíz de procesos → normalizar a dict.
        if isinstance(data, list):
            data = {"procesos": data}
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=502,
                detail="Respuesta de búsqueda con formato inesperado",
            )

        pag = (
            data.get("paginacion")
            or data.get("paginación")
            or data.get("pagination")
            or {}
        )
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
    def _coerce_id(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _id_from_mapping(cls, mapping: dict[str, Any]) -> Optional[int]:
        for key in _ID_INTERNO_KEYS:
            if key in mapping:
                parsed = cls._coerce_id(mapping.get(key))
                if parsed is not None:
                    return parsed
        return None

    @classmethod
    def _id_from_list_item(cls, item: Any) -> Optional[int]:
        if isinstance(item, dict):
            return cls._id_from_mapping(item)
        return cls._coerce_id(item)

    @classmethod
    def _extract_id_interno(cls, data: dict[str, Any]) -> Optional[int]:
        top = cls._id_from_mapping(data)
        if top is not None:
            return top

        for list_key in _PROCESOS_LIST_KEYS:
            procesos = data.get(list_key)
            if isinstance(procesos, list) and procesos:
                found = cls._id_from_list_item(procesos[0])
                if found is not None:
                    return found
            elif isinstance(procesos, dict):
                found = cls._id_from_mapping(procesos)
                if found is not None:
                    return found

        # Un solo proceso anidado: {proceso: {idProceso: …}} / {resultado: {…}}
        for nest_key in ("proceso", "Proceso", "resultado", "Resultado"):
            nested = data.get(nest_key)
            if isinstance(nested, dict):
                found = cls._id_from_mapping(nested)
                if found is not None:
                    return found
            elif isinstance(nested, list) and nested:
                found = cls._id_from_list_item(nested[0])
                if found is not None:
                    return found
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
