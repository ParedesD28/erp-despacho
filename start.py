"""Production startup for the ERP."""

import base64
import hashlib
import hmac
import os
import re
import time
from http.cookies import SimpleCookie

import bcrypt
import psycopg2
import uvicorn

import main
import compat_routes  # noqa: F401
import feature_routes  # noqa: F401
import data_integrity  # noqa: F401
import route_patches  # noqa: F401
import tasa_patch  # noqa: F401
import expedientes_patch  # noqa: F401
import crm_anular_patch  # noqa: F401
import agent_supervision  # noqa: F401
import export_patches  # noqa: F401
import export_patches_compat  # noqa: F401
import pdf_final_patch  # noqa: F401
import bot_pdf_patch  # noqa: F401
import export_final_patch  # noqa: F401
import expediente_editor_patch  # noqa: F401
import production_checks  # noqa: F401

BOT_PATH = "/api/bot/liquidar"
BOT_PDF_PREFIX = "/api/bot/pdf/"
SESSION_COOKIE = "token_erp"
SESSION_TTL = int(os.getenv("ERP_SESSION_TTL", "28800"))
SESSION_SECRET = os.getenv("ERP_SESSION_SECRET") or os.getenv("LIQUIDADOR_API_KEY") or os.getenv("DATABASE_URL")
if not os.getenv("ERP_SESSION_SECRET"):
    print("[START] ADVERTENCIA: ERP_SESSION_SECRET no esta configurado; se usa un secreto de despliegue como fallback. Configure ERP_SESSION_SECRET en Render para produccion.", flush=True)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _firmar_sesion(user_id: str, expires_at: int) -> str:
    payload = f"{user_id}.{expires_at}".encode("utf-8")
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def _validar_sesion(token: str):
    try:
        payload_b64, signature_b64 = token.split(".", 1)
        payload = _b64decode(payload_b64)
        expected = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        supplied = _b64decode(signature_b64)
        if not hmac.compare_digest(expected, supplied):
            return None
        user_id, expires_text = payload.decode("utf-8").split(".", 1)
        expires_at = int(expires_text)
        return user_id if user_id and expires_at > int(time.time()) else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _cookie_from_response(response):
    raw = response.headers.get("set-cookie", "")
    if not raw:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def _set_secure_session(response, user_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=_firmar_sesion(str(user_id), int(time.time()) + SESSION_TTL),
        max_age=SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _password_compatible(password_plana, password_hash):
    if not password_plana or not password_hash:
        return False
    try:
        encoded = str(password_hash).encode("utf-8")
        if encoded.startswith((b"$2a$", b"$2b$", b"$2y$")):
            return bcrypt.checkpw(password_plana.encode("utf-8"), encoded)
        return hmac.compare_digest(str(password_hash), str(password_plana))
    except (ValueError, TypeError):
        return False


main.verificar_password = _password_compatible


def _is_bot_public_path(path: str) -> bool:
    return path == BOT_PATH or path.startswith(BOT_PDF_PREFIX)


def _bypass_bot_auth_middleware() -> None:
    for middleware in getattr(main.app, "user_middleware", []):
        dispatch = middleware.kwargs.get("dispatch")
        if dispatch is None or getattr(dispatch, "__name__", "") != "validador_general_seguridad":
            continue
        original_dispatch = dispatch

        async def guarded_dispatch(request, call_next, _original=original_dispatch):
            if _is_bot_public_path(request.url.path):
                return await call_next(request)
            return await _original(request, call_next)

        middleware.kwargs["dispatch"] = guarded_dispatch
        print("[START] Middleware browser-session adaptado para /api/bot/*", flush=True)
        return
    print("[START] No se encontro validador_general_seguridad", flush=True)


async def _production_security_middleware(request, call_next):
    path = request.url.path
    public_paths = {"/login", "/logout", "/health"}
    if path not in public_paths and not _is_bot_public_path(path) and not path.startswith("/static/"):
        token = request.cookies.get(SESSION_COOKIE)
        if not token or not _validar_sesion(token):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/login", status_code=303)

    if path == "/crear_proceso_cascada" and request.method == "POST":
        try:
            form = await request.form()
            radicado = str(form.get("radicado_interno", "")).strip()
            cedulas = [x.strip() for x in str(form.get("demandado_cedulas", "")).split("|") if x.strip()]
            nombres = [x.strip() for x in str(form.get("demandado_nombres", "")).split("|") if x.strip()]
            if not radicado or not cedulas or len(cedulas) != len(nombres):
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url="/expedientes?error=Datos+de+demandados+invalidos", status_code=303)
            if any(not re.match(r"^\d{6,15}$", c) for c in cedulas):
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url="/expedientes?error=Identificacion+invalida", status_code=303)
            with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM procesos WHERE radicado_interno=%s LIMIT 1", (radicado,))
                    if cur.fetchone():
                        from fastapi.responses import RedirectResponse
                        return RedirectResponse(url="/expedientes?error=El+radicado+ya+existe", status_code=303)
        except Exception as exc:
            print(f"[START] Validacion de proceso fallida: {repr(exc)}", flush=True)
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/expedientes?error=No+fue+posible+validar+los+datos", status_code=303)

    response = await call_next(request)
    if SESSION_SECRET and path == "/login" and response.status_code in (301, 302, 303, 307, 308):
        legacy_user_id = _cookie_from_response(response)
        if legacy_user_id and legacy_user_id.isdigit():
            _set_secure_session(response, legacy_user_id)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


_bypass_bot_auth_middleware()
main.app.middleware("http")(_production_security_middleware)


if __name__ == "__main__":
    if not SESSION_SECRET:
        print("[START] ERROR: no hay secreto disponible para firmar sesiones", flush=True)
    uvicorn.run(main.app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
