"""Autenticación central: sesiones HMAC y contraseñas bcrypt únicamente."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Optional

import bcrypt


SESSION_COOKIE = "token_erp"
SESSION_TTL = int(os.getenv("ERP_SESSION_TTL", "28800"))
SESSION_SECRET = (os.getenv("ERP_SESSION_SECRET") or "").strip()

if len(SESSION_SECRET) < 32:
    raise RuntimeError("ERP_SESSION_SECRET debe estar configurado y tener al menos 32 caracteres")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_session(user_id: str, expires_at: Optional[int] = None) -> str:
    expiry = int(expires_at or (time.time() + SESSION_TTL))
    payload = f"{str(user_id)}.{expiry}".encode("utf-8")
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"v1.{_b64(payload)}.{_b64(signature)}"


def verify_session(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        version, payload_b64, signature_b64 = token.split(".", 2)
        if version != "v1":
            return None
        payload = _b64decode(payload_b64)
        supplied = _b64decode(signature_b64)
        expected = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, supplied):
            return None
        user_id, expiry_text = payload.decode("utf-8").split(".", 1)
        expiry = int(expiry_text)
        if not user_id or expiry <= int(time.time()):
            return None
        return user_id
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def set_session_cookie(response, user_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sign_session(str(user_id)),
        max_age=SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


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
