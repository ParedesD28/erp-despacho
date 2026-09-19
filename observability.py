"""Logging conversacional estructurado, diagnóstico de rutas en tiempo real y captura de errores."""
from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

LOGGER = logging.getLogger("erp")
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def log_msg(tag: str, mensaje: str, **detalles) -> None:
    """Emite un log en texto claro con emojis y métricas legibles al instante en Render."""
    extra = " | " + " ".join(f"{k}={v}" for k, v in detalles.items()) if detalles else ""
    hora = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{hora} UTC] {tag} {mensaje}{extra}", flush=True)


def _json_log(level: str, event: str, **fields) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "service": "erp-despacho",
        "event": event,
        **fields,
    }
    LOGGER.log(
        logging.ERROR if level == "ERROR" else logging.INFO,
        json.dumps(payload, ensure_ascii=False, default=str),
    )


def _es_api_path(path: str) -> bool:
    """Determina si una ruta pertenece a una API que debe conservar respuestas HTTP/JSON."""
    return path.startswith("/api/") or path.startswith("/sms/api/") or path.startswith("/sms/wizard/")


def _instalar_selector_sms(app=None) -> None:
    """Punto de compatibilidad; la integración SMS canónica se instala en start.py."""
    return


def install_exception_handling(app) -> None:
    # main.py ya importó sms_router y registró su router antes de llegar aquí.
    # En este punto es seguro sustituir el selector y registrar el wizard.
    _instalar_selector_sms(app)

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4())[:8])
        log_msg("⚠️ [HTTP ERROR]", f"{exc.status_code} en {request.method} {request.url.path}", detalle=exc.detail)
        if _es_api_path(request.url.path):
            return JSONResponse({"error": exc.detail, "request_id": request_id}, status_code=exc.status_code)
        if exc.status_code in (401, 403):
            return RedirectResponse(url="/login", status_code=303)
        return JSONResponse({"error": exc.detail, "request_id": request_id}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4())[:8])
        print("\n🚨 ===================== ERROR CRÍTICO =====================", flush=True)
        print(f"💥 Ruta: {request.method} {request.url.path} | Request ID: {request_id}", flush=True)
        print(f"💥 Excepción: {type(exc).__name__}: {exc}", flush=True)
        print(f"💥 Traza detallada:\n{traceback.format_exc()}", flush=True)
        print("🚨 =========================================================\n", flush=True)

        if _es_api_path(request.url.path):
            return JSONResponse(
                {"error": "Error interno del servidor", "detalle": str(exc), "request_id": request_id},
                status_code=500,
            )
        return JSONResponse(
            {"error": "No fue posible completar la solicitud", "detalle": str(exc), "request_id": request_id},
            status_code=500,
        )

    @app.middleware("http")
    async def structured_request_logging(request: Request, call_next):
        # Silenciar /health para que no inunde la consola cada 5 segundos
        es_health = request.url.path == "/health"
        inicio = time.perf_counter()
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        ip_cliente = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "local")

        if not es_health:
            log_msg("🚀 [HTTP ENTRANTE]", f"{request.method} {request.url.path}", ip=ip_cliente, id=request_id)

        try:
            response = await call_next(request)
        except Exception as exc:
            duracion_ms = round((time.perf_counter() - inicio) * 1000, 1)
            log_msg("💥 [HTTP FALLÓ]", f"{request.method} {request.url.path} tras {duracion_ms}ms", error=f"{type(exc).__name__}: {exc}")
            raise

        duracion_ms = round((time.perf_counter() - inicio) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        tamano = response.headers.get("content-length") or "streaming"
        content_type = response.headers.get("content-type", "n/a").split(";")[0]

        if not es_health:
            log_msg("✅ [HTTP SALIDA]", f"{request.method} {request.url.path} -> {response.status_code}", ms=duracion_ms, bytes=tamano, tipo=content_type)

        return response
