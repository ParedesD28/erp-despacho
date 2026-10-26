"""Autenticación central: sesiones HMAC y contraseñas bcrypt únicamente."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import bcrypt


SESSION_COOKIE = "token_erp"
SESSION_VERSION = "v2"
SESSION_TTL_MIN = 300
SESSION_TTL_MAX = 86400
CLOCK_SKEW = 60


def _ttl_desde_env() -> int:
    try:
        valor = int(os.getenv("ERP_SESSION_TTL", "28800"))
    except (TypeError, ValueError):
        valor = 28800
    return max(SESSION_TTL_MIN, min(valor, SESSION_TTL_MAX))


# Vigencia absoluta de la sesión (acotada entre 5 min y 24 h; por defecto 8 h).
SESSION_TTL = _ttl_desde_env()
SESSION_SECRET = (os.getenv("ERP_SESSION_SECRET") or "").strip()

if len(SESSION_SECRET) < 32:
    raise RuntimeError("ERP_SESSION_SECRET debe estar configurado y tener al menos 32 caracteres")


@dataclass(frozen=True)
class SessionData:
    user_id: str
    issued_at: int
    expires_at: int
    sid: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _firmar(payload: bytes) -> bytes:
    return hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()


def new_session_id() -> str:
    return secrets.token_hex(16)


def sign_session(
    user_id: str,
    expires_at: Optional[int] = None,
    *,
    sid: Optional[str] = None,
    issued_at: Optional[int] = None,
) -> str:
    """Token v2: payload `uid.iat.exp.sid` firmado con HMAC-SHA256.

    Cada llamada sin `sid` genera un identificador de sesión nuevo (anti-fixation).
    El token solo depende de ERP_SESSION_SECRET: rotarlo invalida todas las
    sesiones y cada usuario solo debe iniciar sesión una vez más.
    """
    ahora = int(issued_at if issued_at is not None else time.time())
    expiry = int(expires_at or (ahora + SESSION_TTL))
    session_id = sid or new_session_id()
    payload = f"{str(user_id)}.{ahora}.{expiry}.{session_id}".encode("utf-8")
    return f"{SESSION_VERSION}.{_b64(payload)}.{_b64(_firmar(payload))}"


def parse_session(token: Optional[str]) -> Optional[SessionData]:
    """Valida versión, firma (tiempo constante), expiración y vigencia máxima."""
    if not token or not isinstance(token, str) or len(token) > 512:
        return None
    try:
        version, payload_b64, signature_b64 = token.split(".", 2)
        if not hmac.compare_digest(version.encode("ascii"), SESSION_VERSION.encode("ascii")):
            return None
        payload = _b64decode(payload_b64)
        supplied = _b64decode(signature_b64)
        if not hmac.compare_digest(_firmar(payload), supplied):
            return None
        user_id, iat_text, exp_text, sid = payload.decode("utf-8").rsplit(".", 3)
        issued_at = int(iat_text)
        expires_at = int(exp_text)
        ahora = int(time.time())
        if not user_id or not sid or len(sid) != 32:
            return None
        if expires_at <= ahora or issued_at > ahora + CLOCK_SKEW:
            return None
        if expires_at - issued_at > SESSION_TTL_MAX:
            return None
        return SessionData(user_id=user_id, issued_at=issued_at, expires_at=expires_at, sid=sid)
    except (ValueError, TypeError, UnicodeError):
        return None


def verify_session(token: Optional[str]) -> Optional[str]:
    data = parse_session(token)
    return data.user_id if data else None


def session_id_from_token(token: Optional[str]) -> Optional[str]:
    data = parse_session(token)
    return data.sid if data else None


def _cookie_kwargs() -> dict:
    return {"path": "/", "secure": True, "httponly": True, "samesite": "lax"}


def set_session_cookie(response, user_id: str) -> str:
    """Emite una sesión nueva (sid fresco) y devuelve su token."""
    token = sign_session(str(user_id))
    response.set_cookie(key=SESSION_COOKIE, value=token, max_age=SESSION_TTL, **_cookie_kwargs())
    return token


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, **_cookie_kwargs())


def is_bcrypt_hash(value) -> bool:
    return bool(value) and str(value).encode("utf-8").startswith((b"$2a$", b"$2b$", b"$2y$"))


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("La contraseña no puede estar vacía")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not is_bcrypt_hash(password_hash):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), str(password_hash).encode("utf-8"))
    except (ValueError, TypeError):
        return False


def migrate_legacy_passwords(db_pool) -> int:
    """Convierte hashes/contraseñas heredadas a bcrypt sin exponer sus valores."""
    conn = db_pool.getconn()
    migrated = 0
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, password FROM abogados WHERE password IS NOT NULL")
                rows = cur.fetchall()
                for user_id, stored in rows:
                    if stored and is_bcrypt_hash(stored):
                        continue
                    if not stored:
                        continue
                    cur.execute(
                        "UPDATE abogados SET password=%s WHERE id=%s",
                        (hash_password(str(stored)), user_id),
                    )
                    migrated += 1
        return migrated
    finally:
        db_pool.putconn(conn)
