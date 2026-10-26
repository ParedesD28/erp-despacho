"""Settings clave/valor con valor cifrado (Fernet).

Llave de cifrado: ``DRIVE_TOKEN_KEY`` (dedicada, mínimo 32 caracteres). Mientras no
esté configurada se usa la derivada de ``ERP_SESSION_SECRET`` (comportamiento
histórico). Al leer se prueba primero la dedicada y luego la derivada; si el valor
solo descifra con la derivada y existe la dedicada, se re-cifra con la dedicada
(migración perezosa). Así rotar ``ERP_SESSION_SECRET`` no vuelve a romper el token.

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


ENV_DEDICATED_KEY = "DRIVE_TOKEN_KEY"
CIPHER_DEDICADA = "dedicada"
CIPHER_DERIVADA = "derivada"


def _fernet_desde_secreto(secret: str):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "Falta dependencia cryptography para cifrar app_settings"
        ) from exc
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _dedicated_fernet():
    secret = (os.environ.get(ENV_DEDICATED_KEY) or "").strip().strip('"').strip("'")
    if not secret:
        return None
    if len(secret) < 32:
        raise RuntimeError(f"{ENV_DEDICATED_KEY} insuficiente (mínimo 32 caracteres)")
    return _fernet_desde_secreto(secret)


def _legacy_fernet():
    secret = (os.environ.get("ERP_SESSION_SECRET") or "").strip()
    if len(secret) < 32:
        return None
    return _fernet_desde_secreto(secret)


def dedicated_key_configured() -> bool:
    return bool((os.environ.get(ENV_DEDICATED_KEY) or "").strip())


def _fernet():
    """Fernet para cifrar: la dedicada si existe; si no, la derivada de la sesión."""
    f = _dedicated_fernet() or _legacy_fernet()
    if f is None:
        raise RuntimeError("ERP_SESSION_SECRET insuficiente para cifrar settings")
    return f


def encrypt_value(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def _decrypt_con_origen(token: str) -> tuple[str, str]:
    """Descifra probando dedicada y luego derivada. Devuelve ``(texto, origen)``."""
    from cryptography.fernet import InvalidToken

    raw = token.encode("ascii")
    candidatos = []
    dedicada = _dedicated_fernet()
    if dedicada is not None:
        candidatos.append((CIPHER_DEDICADA, dedicada))
    derivada = _legacy_fernet()
    if derivada is not None:
        candidatos.append((CIPHER_DERIVADA, derivada))
    if not candidatos:
        raise RuntimeError("No hay llave de cifrado configurada para app_settings")
    for origen, f in candidatos:
        try:
            return f.decrypt(raw).decode("utf-8"), origen
        except InvalidToken:
            continue
    raise InvalidToken()


def decrypt_value(token: str) -> str:
    return _decrypt_con_origen(token)[0]


def _reencrypt_si_necesario(conn, key: str, enc_actual: str, plain: str, origen: str) -> bool:
    """Migración perezosa derivada -> dedicada. Nunca propaga errores ni registra valores."""
    if origen != CIPHER_DERIVADA or not dedicated_key_configured():
        return False
    try:
        nuevo = encrypt_value(plain)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app_settings SET value_encrypted = %s, updated_at = CURRENT_TIMESTAMP "
                    "WHERE key = %s AND value_encrypted = %s",
                    (nuevo, key, enc_actual),
                )
        logger.info("app_settings %s re-cifrado con %s", key, ENV_DEDICATED_KEY)
        return True
    except Exception as exc:
        logger.warning("app_settings re-cifrado %s falló: %s", key, type(exc).__name__)
        return False


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
            plain, origen = _decrypt_con_origen(str(enc))
        _reencrypt_si_necesario(conn, key, str(enc), plain, origen)
        return plain
    except Exception as exc:
        logger.warning("app_settings get %s falló: %s", key, type(exc).__name__)
        return None
    finally:
        if conn is not None:
            conn.release()


def cipher_status(key: str) -> Optional[str]:
    """``dedicada``/``derivada`` según la llave con que está cifrado hoy, o None. Sin secretos."""
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
                "SELECT value_encrypted FROM app_settings WHERE key = %s LIMIT 1", (key,)
            )
            row = cur.fetchone()
        if not row:
            return None
        enc = row["value_encrypted"] if isinstance(row, dict) else row[0]
        return _decrypt_con_origen(str(enc))[1] if enc else None
    except Exception as exc:
        logger.warning("app_settings cipher_status %s falló: %s", key, type(exc).__name__)
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
