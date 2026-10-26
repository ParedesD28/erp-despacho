"""Llaves M2M separadas por propósito, con compatibilidad temporal a la llave compartida.

Propósitos y variables de entorno del ERP:

- ``bot``          -> ``BOT_API_KEY``            (X-API-Key en /api/bot/*)
- ``recaudos``     -> ``RECAUDOS_API_KEY``       (X-API-Key en /api/recaudos/bot/*)
- ``pdf``          -> ``BOT_PDF_PUBLIC_SECRET``  (HMAC de URLs firmadas de PDF)
- ``supervision``  -> ``AGENT_SUPERVISION_KEY``  (ERP -> agente, cabecera saliente)

Mientras la llave compartida heredada ``LIQUIDADOR_API_KEY`` siga habilitada
(``M2M_LEGACY_KEY_ENABLED`` distinto de false, por defecto habilitada) cada
propósito acepta su llave nueva Y la heredada, así cada consumidor se puede
cambiar de forma independiente. Para retirar la heredada basta con borrar
``LIQUIDADOR_API_KEY`` o poner ``M2M_LEGACY_KEY_ENABLED=false`` (reversible).

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

PURPOSE_BOT = "bot"
PURPOSE_RECAUDOS = "recaudos"
PURPOSE_PDF = "pdf"
PURPOSE_SUPERVISION = "supervision"

PURPOSE_ENV = {
    PURPOSE_BOT: "BOT_API_KEY",
    PURPOSE_RECAUDOS: "RECAUDOS_API_KEY",
    PURPOSE_PDF: "BOT_PDF_PUBLIC_SECRET",
    PURPOSE_SUPERVISION: "AGENT_SUPERVISION_KEY",
}
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
        elif legacy_key():
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
