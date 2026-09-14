"""Logging estructurado y manejo global seguro de excepciones HTTP."""
from __future__ import annotations

import json
import logging
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


def _json_log(level: str, event: str, **fields) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "service": "erp-despacho",
        "event": event,
        **fields,
    }
    LOGGER.log(logging.ERROR if level == "ERROR" else logging.INFO, json.dumps(payload, ensure_ascii=False, default=str))


def install_exception_handling(app) -> None:
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        _json_log("INFO", "http_error", request_id=request_id, method=request.method, path=request.url.path, status=exc.status_code)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Solicitud no autorizada o inválida", "request_id": request_id}, status_code=exc.status_code)
        if exc.status_code in (401, 403):
            return RedirectResponse(url="/login", status_code=303)
        return JSONResponse({"error": "No fue posible completar la solicitud", "request_id": request_id}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        _json_log(
            "ERROR",
            "unhandled_exception",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            exception=type(exc).__name__,
            traceback=traceback.format_exc(),
        )
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Error interno del servidor", "request_id": request_id}, status_code=500)
        return JSONResponse({"error": "No fue posible completar la solicitud", "request_id": request_id}, status_code=500)

    @app.middleware("http")
    async def structured_request_logging(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            # El handler global registra la traza completa; este bloque conserva
            # el identificador y evita duplicar información sensible.
            raise
        response.headers["X-Request-ID"] = request_id
        _json_log("INFO", "request", request_id=request_id, method=request.method, path=request.url.path, status=response.status_code)
        return response
