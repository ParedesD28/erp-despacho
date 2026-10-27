"""Llaves M2M separadas por propósito, con compatibilidad temporal a la llave compartida.

Propósitos y variables de entorno del ERP:

- ``bot``          -> ``BOT_API_KEY``            (X-API-Key en /api/bot/*)
- ``recaudos``     -> ``RECAUDOS_API_KEY``       (X-API-Key en /api/recaudos/bot/*)
- ``pdf``          -> ``BOT_PDF_PUBLIC_SECRET``  (HMAC de URLs firmadas de PDF)
- ``supervision``  -> ``AGENT_SUPERVISION_KEY``  (ERP -> agente, cabecera saliente)
- ``grok``         -> ``GROK_BOT_API_KEY``       (X-API-Key de "Grok Bot": /api/bot/*,
                                                  /api/recaudos/bot/* y /api/grok/*)

Mientras la llave compartida heredada ``LIQUIDADOR_API_KEY`` siga habilitada
(``M2M_LEGACY_KEY_ENABLED`` distinto de false, por defecto habilitada) cada
propósito acepta su llave nueva Y la heredada, así cada consumidor se puede
cambiar de forma independiente. Para retirar la heredada basta con borrar
``LIQUIDADOR_API_KEY`` o poner ``M2M_LEGACY_KEY_ENABLED=false`` (reversible).

La llave de Grok Bot es ADICIONAL: se acepta además de la llave del propósito en
``/api/bot/*`` y ``/api/recaudos/bot/*`` (el bot de cobranza no cambia), nunca acepta
la llave heredada ni las de otros propósitos, y es la única que abre ``/api/grok/*``.
No abre administración, sesiones, llaves, ``/sms/api/`` ni ``/api/v1/cron/``.

Reglas: comparación en tiempo constante, nunca se registran valores de llaves,
una llave vacía o ausente jamás autentica (fail-closed).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time

from fastapi import HTTPException

import rate_limit

PURPOSE_BOT = "bot"
PURPOSE_RECAUDOS = "recaudos"
PURPOSE_PDF = "pdf"
PURPOSE_SUPERVISION = "supervision"
PURPOSE_GROK = "grok"

CONSUMER_GROK = "grok"

PURPOSE_ENV = {
    PURPOSE_BOT: "BOT_API_KEY",
    PURPOSE_RECAUDOS: "RECAUDOS_API_KEY",
    PURPOSE_PDF: "BOT_PDF_PUBLIC_SECRET",
    PURPOSE_SUPERVISION: "AGENT_SUPERVISION_KEY",
    PURPOSE_GROK: "GROK_BOT_API_KEY",
}
NO_LEGACY_PURPOSES = frozenset({PURPOSE_GROK})
LEGACY_ENV = "LIQUIDADOR_API_KEY"
LEGACY_SWITCH_ENV = "M2M_LEGACY_KEY_ENABLED"

_FALSE_VALUES = {"0", "false", "no", "off", "n"}
_LEGACY_LOG_INTERVAL_S = 300
_legacy_last_log: dict[str, float] = {}
_log_lock = threading.Lock()


def _clean(value: str | None) -> str:
    return (value or "").strip().strip('"').strip("'").strip()


def legacy_enabled() -> bool:
    raw = _clean(os.getenv(LEGACY_SWITCH_ENV))
    return raw.lower() not in _FALSE_VALUES


def specific_key(purpose: str) -> str:
    return _clean(os.getenv(PURPOSE_ENV[purpose]))


def legacy_key() -> str:
    if not legacy_enabled():
        return ""
    return _clean(os.getenv(LEGACY_ENV))


def accepted_keys(purpose: str) -> list[tuple[str, str]]:
    """Lista ``[(origen, valor)]`` de llaves válidas para el propósito.

    ``origen`` es ``"nueva"`` o ``"heredada"``. Nunca incluye valores vacíos.
    """
    keys: list[tuple[str, str]] = []
    nueva = specific_key(purpose)
    if nueva:
        keys.append(("nueva", nueva))
    if purpose in NO_LEGACY_PURPOSES:
        return keys
    legada = legacy_key()
    if legada and legada != nueva:
        keys.append(("heredada", legada))
    return keys


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def match_key(purpose: str, supplied: str | None) -> str | None:
    """Devuelve ``"nueva"``/``"heredada"`` si ``supplied`` es válida, si no ``None``.

    Compara digests SHA-256 con ``compare_digest`` contra todas las candidatas
    sin cortar en la primera coincidencia; las longitudes distintas no lanzan.
    """
    if not supplied:
        return None
    supplied_digest = _digest(supplied)
    matched: str | None = None
    for origen, valor in accepted_keys(purpose):
        if hmac.compare_digest(supplied_digest, _digest(valor)) and matched is None:
            matched = origen
    return matched


def note_legacy_use(purpose: str) -> None:
    now = time.monotonic()
    with _log_lock:
        last = _legacy_last_log.get(purpose)
        if last is not None and now - last < _LEGACY_LOG_INTERVAL_S:
            return
        _legacy_last_log[purpose] = now
    print(
        f"[M2M][AVISO] propósito={purpose} autenticado con la llave heredada "
        f"{LEGACY_ENV}; configurar {PURPOSE_ENV[purpose]} en el consumidor.",
        flush=True,
    )


def require_api_key(purpose: str, supplied: str | None) -> None:
    """Valida la llave presentada por un consumidor externo o lanza 503/401."""
    if not accepted_keys(purpose):
        raise HTTPException(
            status_code=503,
            detail=f"{PURPOSE_ENV[purpose]} no está configurada",
        )
    origen = match_key(purpose, supplied)
    if origen is None:
        raise HTTPException(status_code=401, detail="No autorizado")
    if origen == "heredada":
        note_legacy_use(purpose)


def _digest_matches(purpose: str, supplied_digest: bytes) -> str | None:
    matched: str | None = None
    for origen, valor in accepted_keys(purpose):
        if hmac.compare_digest(supplied_digest, _digest(valor)) and matched is None:
            matched = origen
    return matched


def authenticate_machine(purpose: str, supplied: str | None) -> str:
    """Autentica una llamada M2M y devuelve el consumidor: ``purpose`` o ``"grok"``.

    Acepta la llave del propósito (nueva o heredada, como siempre) o la llave de
    Grok Bot. Si no hay ninguna llave configurada responde 503 (fail-closed); si hay
    pero no coincide, 401. Las comparaciones son en tiempo constante y no cortan en
    la primera coincidencia.
    """
    if not accepted_keys(purpose) and not accepted_keys(PURPOSE_GROK):
        raise HTTPException(
            status_code=503,
            detail=f"{PURPOSE_ENV[purpose]} no está configurada",
        )
    if not supplied:
        raise HTTPException(status_code=401, detail="No autorizado")
    supplied_digest = _digest(supplied)
    origen = _digest_matches(purpose, supplied_digest)
    es_grok = _digest_matches(PURPOSE_GROK, supplied_digest) is not None
    if origen is not None:
        if origen == "heredada":
            note_legacy_use(purpose)
        return purpose
    if es_grok:
        return CONSUMER_GROK
    raise HTTPException(status_code=401, detail="No autorizado")


def require_machine_request(purpose: str, request) -> str:
    """Dependencia de rutas M2M: autentica, marca el consumidor y aplica el límite de Grok.

    Deja ``request.state.m2m_consumer`` para el log de auditoría del middleware.
    """
    consumer = authenticate_machine(purpose, request.headers.get("X-API-Key"))
    _marcar_consumidor(request, consumer)
    if consumer == CONSUMER_GROK:
        _limitar_grok()
    return consumer


def _marcar_consumidor(request, consumer: str) -> None:
    state = getattr(request, "state", None)
    if state is not None:
        state.m2m_consumer = consumer


def require_grok_request(request) -> str:
    """Dependencia de ``/api/grok/*``: solo la llave de Grok Bot (503 si falta, 401 si no coincide)."""
    if not accepted_keys(PURPOSE_GROK):
        raise HTTPException(
            status_code=503,
            detail=f"{PURPOSE_ENV[PURPOSE_GROK]} no está configurada",
        )
    if match_key(PURPOSE_GROK, request.headers.get("X-API-Key")) is None:
        raise HTTPException(status_code=401, detail="No autorizado")
    _marcar_consumidor(request, CONSUMER_GROK)
    _limitar_grok()
    return CONSUMER_GROK


def _limitar_grok() -> None:
    permitido, espera = rate_limit.limiter.grok_window.hit(CONSUMER_GROK)
    if not permitido:
        raise HTTPException(
            status_code=429,
            detail="Demasiadas solicitudes de Grok Bot",
            headers={"Retry-After": str(espera)},
        )


def signing_secret(purpose: str) -> str:
    """Secreto para firmar: el específico si existe, si no la heredada. Vacío si ninguno."""
    nueva = specific_key(purpose)
    if nueva:
        return nueva
    return legacy_key()


def verification_secrets(purpose: str) -> list[str]:
    """Secretos con los que se puede verificar una firma (nuevo + heredado)."""
    return [valor for _origen, valor in accepted_keys(purpose)]


def outbound_key(purpose: str) -> tuple[str, str]:
    """Llave que el ERP envía a un servicio externo: ``(valor, origen)``.

    Prefiere la específica; usa la heredada solo mientras esté habilitada.
    Devuelve ``("", "")`` si no hay ninguna (el llamador debe fallar cerrado).
    """
    nueva = specific_key(purpose)
    if nueva:
        return nueva, "nueva"
    legada = legacy_key()
    if legada:
        return legada, "heredada"
    return "", ""


def describe_config() -> dict[str, str]:
    """Estado por propósito sin exponer valores: nueva / solo-heredada / faltante."""
    estado: dict[str, str] = {}
    for purpose in PURPOSE_ENV:
        if specific_key(purpose):
            estado[purpose] = "nueva"
        elif legacy_key() and purpose not in NO_LEGACY_PURPOSES:
            estado[purpose] = "solo-heredada"
        else:
            estado[purpose] = "faltante"
    return estado


def log_config_summary() -> None:
    estado = describe_config()
    partes = " ".join(f"{k}={v}" for k, v in estado.items())
    print(
        f"[M2M] llaves: {partes} legacy_habilitada={legacy_enabled()}",
        flush=True,
    )
