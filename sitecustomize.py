"""Runtime compatibility hooks for the existing ERP monolith.

Python imports sitecustomize automatically when it is on sys.path. We use this
small adapter so the bot endpoint can be secured and replaced without making
a risky wholesale rewrite of main.py.
"""
import os
from functools import wraps


_API_PATH = "/api/bot/liquidar"


def _api_key_ok(request):
    expected = os.getenv("LIQUIDADOR_API_KEY")
    supplied = request.headers.get("X-API-Key")
    return bool(expected and supplied and supplied == expected)


try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    _original_middleware = FastAPI.middleware
    _original_post = FastAPI.post

    def _secure_middleware(self, middleware_type):
        decorator = _original_middleware(self, middleware_type)

        def register(func):
            if middleware_type != "http":
                return decorator(func)

            @wraps(func)
            async def guarded(request, call_next):
                if request.url.path == _API_PATH:
                    if _api_key_ok(request):
                        return await call_next(request)
                    return JSONResponse({"status": "error", "mensaje": "No autorizado"}, status_code=401)
                return await func(request, call_next)

            # Register the wrapped version but preserve the original function
            # for the module namespace.
            self.add_middleware.__wrapped_dispatch__ = guarded
            from starlette.middleware.base import BaseHTTPMiddleware
            self.add_middleware(BaseHTTPMiddleware, dispatch=guarded)
            return func

        return register

    def _secure_post(self, path, *args, **kwargs):
        if path != _API_PATH:
            return _original_post(self, path, *args, **kwargs)

        def decorator(_legacy_function):
            from bot_api import liquidar_para_bot

            # The legacy handler remains in main.py for compatibility, but it
            # is intentionally not registered: it exposed no API auth, returned
            # the wrong total and generated a placeholder PDF URL.
            route_kwargs = dict(kwargs)
            route_kwargs.pop("response_model", None)
            route_kwargs.pop("status_code", None)
            route_kwargs.pop("responses", None)
            route_kwargs.pop("deprecated", None)
            route_kwargs.pop("operation_id", None)
            route_kwargs.pop("summary", None)
            route_kwargs.pop("description", None)
            route_kwargs.pop("response_description", None)
            route_kwargs.pop("tags", None)
            route_kwargs.pop("dependencies", None)
            route_kwargs.pop("callbacks", None)
            route_kwargs.pop("openapi_extra", None)
            route_kwargs.pop("include_in_schema", None)

            self.add_api_route(
                path,
                liquidar_para_bot,
                methods=["POST"],
                include_in_schema=True,
                **route_kwargs,
            )
            return _legacy_function

        return decorator

    FastAPI.middleware = _secure_middleware
    FastAPI.post = _secure_post
except Exception as exc:
    # Never prevent the ERP from starting if the compatibility hook itself fails.
    print(f"[SITECUSTOMIZE] No se pudo cargar el adaptador: {exc}", flush=True)
