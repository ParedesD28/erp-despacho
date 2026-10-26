"""Settings clave/valor con valor cifrado (Fernet derivado de ERP_SESSION_SECRET).

Uso principal: persistir `google_oauth_refresh_token` tras el connect OAuth
admin, para que Drive quede funcional sin pegar el token en Render (aunque
también se puede poner en env como override).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

from psycopg2.extras import RealDictCursor

import db

logger = logging.getLogger(__name__)

KEY_GOOGLE_OAUTH_REFRESH = "google_oauth_refresh_token"


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "Falta dependencia cryptography para cifrar app_settings"
        ) from exc
    secret = (os.environ.get("ERP_SESSION_SECRET") or "").strip()
    if len(secret) < 32:
        raise RuntimeError("ERP_SESSION_SECRET insuficiente para cifrar settings")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_value(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def _table_exists(cur) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'app_settings'
        LIMIT 1
        """
    )
    return cur.fetchone() is not None


def get_setting(key: str) -> Optional[str]:
    """Lee y descifra un setting. None si no existe / tabla ausente / error."""
    key = (key or "").strip()
    if not key:
        return None
    conn = None
    try:
        conn = db.get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur):
                return None
            cur.execute(
                "SELECT value_encrypted FROM app_settings WHERE key = %s LIMIT 1",
                (key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            enc = row["value_encrypted"] if isinstance(row, dict) else row[0]
            if not enc:
                return None
            return decrypt_value(str(enc))
    except Exception as exc:
        logger.warning("app_settings get %s falló: %s", key, type(exc).__name__)
        return None
    finally:
        if conn is not None:
            conn.release()


def set_setting(key: str, value: str) -> bool:
    """Upsert cifrado. True si OK."""
    key = (key or "").strip()
    if not key:
        return False
    plain = value if value is not None else ""
    conn = None
    try:
        enc = encrypt_value(plain)
        conn = db.get_connection()
        with conn:
            with conn.cursor() as cur:
                if not _table_exists(cur):
                    logger.error("Tabla app_settings ausente — aplicar migración")
                    return False
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value_encrypted, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (key) DO UPDATE
                    SET value_encrypted = EXCLUDED.value_encrypted,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (key, enc),
                )
        return True
    except Exception as exc:
        logger.warning("app_settings set %s falló: %s: %s", key, type(exc).__name__, exc)
        return False
    finally:
        if conn is not None:
            conn.release()


def delete_setting(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    conn = None
    try:
        conn = db.get_connection()
        with conn:
            with conn.cursor() as cur:
                if not _table_exists(cur):
                    return False
                cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))
        return True
    except Exception as exc:
        logger.warning("app_settings delete %s falló: %s", key, type(exc).__name__)
        return False
    finally:
        if conn is not None:
            conn.release()


def get_google_oauth_refresh_token() -> Optional[str]:
    """Env override primero; si no, DB cifrada."""
    env = (os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN") or "").strip()
    if env:
        return env
    return get_setting(KEY_GOOGLE_OAUTH_REFRESH)


def save_google_oauth_refresh_token(token: str) -> bool:
    return set_setting(KEY_GOOGLE_OAUTH_REFRESH, (token or "").strip())
