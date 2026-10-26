"""OAuth connect flow (admin) para obtener GOOGLE_OAUTH_REFRESH_TOKEN sin Python local.

Usa Client ID/Secret ya en env. Redirect URI debe coincidir con el cliente
**Web application** en Google Cloud (no Desktop).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from google_drive_client import DRIVE_SCOPE

# Preferir drive.file (mínimo); drive completo como fallback documentado.
OAUTH_CONNECT_SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
STATE_TTL_S = 600
STATE_COOKIE = "gdrive_oauth_state"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def public_base_url() -> str:
    return (
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or ""
    ).strip().rstrip("/")


def redirect_uri() -> str:
    """URI de callback. Override con GOOGLE_OAUTH_REDIRECT_URI si hace falta."""
    explicit = (os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    base = public_base_url()
    if not base:
        raise ValueError(
            "Falta PUBLIC_BASE_URL (o GOOGLE_OAUTH_REDIRECT_URI) para el callback OAuth"
        )
    return f"{base}/herramientas/google-drive/oauth/callback"


def client_id() -> str:
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()


def oauth_client_ready() -> bool:
    """True si hay Client ID+Secret (aún sin refresh token)."""
    return bool(client_id() and client_secret())


def _state_secret() -> bytes:
    raw = (os.environ.get("ERP_SESSION_SECRET") or "").strip()
    if len(raw) < 32:
        raise ValueError("ERP_SESSION_SECRET insuficiente para firmar state OAuth")
    return raw.encode("utf-8")


def mint_oauth_state(*, user_id: str = "") -> str:
    """State CSRF firmado (HMAC) con TTL corto."""
    expiry = int(time.time()) + STATE_TTL_S
    uid = str(user_id or "").strip() or "0"
    payload = f"{uid}.{expiry}".encode("utf-8")
    sig = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    return f"v1.{_b64(payload)}.{_b64(sig)}"


def verify_oauth_state(state: Optional[str], *, user_id: str = "") -> bool:
    if not state:
        return False
    try:
        version, payload_b64, sig_b64 = state.split(".", 2)
        if version != "v1":
            return False
        payload = _b64decode(payload_b64)
        supplied = _b64decode(sig_b64)
        expected = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            return False
        uid, expiry_text = payload.decode("utf-8").split(".", 1)
        if int(expiry_text) <= int(time.time()):
            return False
        expected_uid = str(user_id or "").strip() or "0"
        # Aceptar mismo user o state genérico "0" de sesión antigua.
        if uid not in {expected_uid, "0"} and expected_uid != "0":
            return False
        return True
    except (ValueError, TypeError, UnicodeDecodeError):
        return False


def build_authorize_url(*, state: str, scope: str = OAUTH_CONNECT_SCOPE) -> str:
    if not oauth_client_ready():
        raise ValueError("Faltan GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET")
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    """Intercambia authorization code por tokens. Incluye refresh_token si Google lo envía."""
    code = (code or "").strip()
    if not code:
        raise ValueError("code vacío")
    if not oauth_client_ready():
        raise ValueError("Faltan GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET")

    data = {
        "code": code,
        "client_id": client_id(),
        "client_secret": client_secret(),
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(TOKEN_ENDPOINT, data=data)
    try:
        body = resp.json()
    except Exception:
        body = {"error": "non_json", "raw": (resp.text or "")[:200]}
    if resp.status_code >= 400:
        err = body.get("error_description") or body.get("error") or resp.text
        raise RuntimeError(f"Google token error HTTP {resp.status_code}: {err}")
    if not isinstance(body, dict):
        raise RuntimeError("Respuesta de token inesperada")
    return body


def status_snapshot() -> dict[str, Any]:
    """Metadatos seguros para la UI admin (sin secretos)."""
    folder = (os.environ.get("GOOGLE_DRIVE_FOLDER_ID") or "").strip()
    env_refresh = bool((os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN") or "").strip())
    db_refresh = False
    try:
        from app_settings import get_setting, KEY_GOOGLE_OAUTH_REFRESH

        # Solo presencia: get_setting descifra; evitamos loguear el valor.
        db_refresh = bool(get_setting(KEY_GOOGLE_OAUTH_REFRESH))
    except Exception:
        db_refresh = False
    cipher = None
    if db_refresh:
        try:
            from app_settings import cipher_status, KEY_GOOGLE_OAUTH_REFRESH as _K

            cipher = cipher_status(_K)
        except Exception:
            cipher = None
    try:
        cb = redirect_uri()
    except ValueError as exc:
        cb = f"(error: {exc})"
    return {
        "client_id_present": bool(client_id()),
        "client_secret_present": bool(client_secret()),
        "refresh_token_present": env_refresh or db_refresh,
        "refresh_token_cipher": cipher,
        "refresh_token_source": (
            "env" if env_refresh else ("db" if db_refresh else None)
        ),
        "folder_id_present": bool(folder),
        "folder_id_suffix": folder[-6:] if len(folder) >= 6 else (folder or None),
        "oauth_client_ready": oauth_client_ready(),
        "redirect_uri": cb,
        "public_base_url": public_base_url() or None,
        "scope": OAUTH_CONNECT_SCOPE,
        "drive_scope_full": DRIVE_SCOPE,
    }
