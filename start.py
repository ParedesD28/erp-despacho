"""Startup productivo del ERP: seguridad, pool, extensiones y observabilidad."""
from __future__ import annotations

import os
import re
from datetime import date

from dotenv import load_dotenv

load_dotenv()

import db
from security import (
    SESSION_COOKIE,
    clear_session_cookie,
    migrate_legacy_passwords,
    set_session_cookie,
    verify_password,
    verify_session,
)

# Cualquier psycopg2.connect() posterior termina obligatoriamente en el pool.
db.install_psycopg2_pool()

from psycopg2.extras import RealDictCursor
from fastapi.responses import RedirectResponse
import uvicorn

import main

# Compatibilidad con funciones legacy que ya usan main.db_pool.getconn().
_legacy_pool = getattr(main, "db_pool", None)
main.db_pool = db.POOL
if _legacy_pool is not None and _legacy_pool is not db.POOL:
    try:
        _legacy_pool.closeall()
    except Exception:
        pass
main.verificar_password = verify_password

# Registro único de extensiones y rutas del ERP/bot.
import extensions  # noqa: F401,E402

from observability import install_exception_handling, _json_log

BOT_PATH = "/api/bot/liquidar"
BOT_PDF_PREFIX = "/api/bot/pdf/"

try:
    migrated = migrate_legacy_passwords(db.POOL)
    if migrated:
        _json_log("INFO", "password_migration", migrated_users=migrated)
except Exception:
    _json_log("ERROR", "password_migration_failed")


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
        _json_log("INFO", "legacy_auth_middleware_adapted")
        return


def _liquidador_get_view(request):
    """Carga el formulario sin ejecutar el motor ni causar cuotas."""
    query = request.query_params
    try:
        inmueble_id = int(query.get("inmueble_id")) if query.get("inmueble_id") else None
    except (TypeError, ValueError):
        inmueble_id = None
    try:
        tasa_fija = float(query.get("tasa_fija", 2.5))
    except (TypeError, ValueError):
        tasa_fija = 2.5
    try:
        honorarios_pct = float(query.get("honorarios_pct", 23.8))
    except (TypeError, ValueError):
        honorarios_pct = 23.8
    try:
        gastos = float(query.get("gastos", 0.0))
    except (TypeError, ValueError):
        gastos = 0.0
    return main.templates.TemplateResponse(
        request=request,
        name="liquidador.html",
        context={
            "inmuebles": main.cargar_inmuebles_ph(),
            "resultados": [],
            "resumen": {},
            "parametros": {
                "inmueble_id": inmueble_id,
                "tipo_tasa": query.get("tipo_tasa", "Máxima Legal"),
                "tasa_fija": tasa_fija,
                "honorarios_pct": honorarios_pct,
                "gastos": gastos,
                "fecha_corte": query.get("fecha_corte", date.today().strftime("%Y-%m-%d")),
            },
        },
    )


async def _production_security_middleware(request, call_next):
    path = request.url.path

    # Nunca delegar al handler legacy de login, que escribía el ID plano.
    if path == "/login" and request.method == "POST":
        try:
            form = await request.form()
            email = str(form.get("email") or "").strip().lower()
            password = str(form.get("password") or "")
            if not email or not password:
                return main.templates.TemplateResponse(
                    request=request,
                    name="login.html",
                    context={"request": request, "error": "Credenciales incorrectas."},
                    status_code=401,
                )

            conn = db.POOL.getconn()
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT id, email, password FROM abogados WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
                    usuario = cur.fetchone()
            finally:
                db.POOL.putconn(conn)

            if not usuario or not verify_password(password, usuario.get("password")):
                _json_log("INFO", "login_failed")
                return main.templates.TemplateResponse(
                    request=request,
                    name="login.html",
                    context={"request": request, "error": "Credenciales incorrectas."},
                    status_code=401,
                )

            response = RedirectResponse(url="/dashboard", status_code=303)
            set_session_cookie(response, str(usuario["id"]))
            _json_log("INFO", "login_success")
            return response
        except Exception:
            _json_log("ERROR", "login_error")
            return main.templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"request": request, "error": "No fue posible iniciar sesión."},
                status_code=500,
            )

    if path == "/logout":
        response = RedirectResponse(url="/login", status_code=303)
        clear_session_cookie(response)
        return response

    public = path == "/login" or path == "/health" or _is_bot_public_path(path) or path.startswith("/static/")
    if not public:
        user_id = verify_session(request.cookies.get(SESSION_COOKIE))
        if not user_id:
            response = RedirectResponse(url="/login", status_code=303)
            clear_session_cookie(response)
            return response
        request.state.user_id = user_id

    # GET /liquidador solo prepara la vista. Ninguna navegación crea expensas.
    if path == "/liquidador" and request.method == "GET" and request.query_params.get("inmueble_id"):
        return _liquidador_get_view(request)

    if path == "/crear_proceso_cascada" and request.method == "POST":
        try:
            form = await request.form()
            radicado = str(form.get("radicado_interno", "")).strip()
            cedulas = [x.strip() for x in str(form.get("demandado_cedulas", "")).split("|") if x.strip()]
            nombres = [x.strip() for x in str(form.get("demandado_nombres", "")).split("|") if x.strip()]
            if not radicado or not cedulas or len(cedulas) != len(nombres):
                return RedirectResponse(url="/expedientes?error=Datos+de+demandados+invalidos", status_code=303)
            if any(not re.match(r"^\d{6,15}$", c) for c in cedulas):
                return RedirectResponse(url="/expedientes?error=Identificacion+invalida", status_code=303)
            conn = db.POOL.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM procesos WHERE radicado_interno=%s LIMIT 1", (radicado,))
                    if cur.fetchone():
                        return RedirectResponse(url="/expedientes?error=El+radicado+ya+existe", status_code=303)
            finally:
                db.POOL.putconn(conn)
        except Exception:
            _json_log("ERROR", "process_prevalidation_failed")
            return RedirectResponse(url="/expedientes?error=No+fue+posible+validar+los+datos", status_code=303)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


_bypass_bot_auth_middleware()
main.app.middleware("http")(_production_security_middleware)
install_exception_handling(main.app)


if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
