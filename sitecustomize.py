"""Runtime compatibility hooks for the ERP bot endpoint.

The ERP has a global authentication middleware that protects the web UI with
an ERP cookie. The machine-to-machine bot endpoint must instead authenticate
with LIQUIDADOR_API_KEY. This module patches the existing decorators before
main.py registers its routes, without modifying the large monolithic main.py.
"""
import os
from functools import wraps

_API_PATH = "/api/bot/liquidar"


def _api_key_ok(request):
    expected = os.getenv("LIQUIDADOR_API_KEY")
    supplied = request.headers.get("X-API-Key")
    return bool(expected and supplied and supplied == expected)


try:
    from fastapi import FastAPI

    _original_middleware = FastAPI.middleware
    _original_post = FastAPI.post

    def _secure_middleware(self, middleware_type):
        """Wrap HTTP middleware so the bot API bypasses the browser login guard."""
        decorator = _original_middleware(self, middleware_type)

        def register(func):
            if middleware_type != "http":
                return decorator(func)

            @wraps(func)
            async def guarded(request, call_next):
                if request.url.path == _API_PATH and _api_key_ok(request):
                    return await call_next(request)
                return await func(request, call_next)

            # Use FastAPI's normal middleware decorator mechanism. Do not call
            # add_middleware manually here; doing so can create duplicate stacks
            # and, in older versions, interfere with route startup.
            return decorator(guarded)

        return register

    def _secure_post(self, path, *args, **kwargs):
        """Replace only the legacy bot POST route with the authenticated handler."""
        if path != _API_PATH:
            return _original_post(self, path, *args, **kwargs)

        def decorator(_legacy_function):
            from bot_api import liquidar_para_bot

            route_kwargs = dict(kwargs)
            # These decorator arguments can conflict with the handler replacement
            # and are not needed for this endpoint.
            for key in (
                "response_model", "status_code", "responses", "deprecated",
                "operation_id", "summary", "description", "response_description",
                "tags", "dependencies", "callbacks", "openapi_extra",
                "include_in_schema",
            ):
                route_kwargs.pop(key, None)

            self.add_api_route(
                path,
                liquidar_para_bot,
                methods=["POST"],
                include_in_schema=True,
                **route_kwargs,
            )
            print("[SITECUSTOMIZE] Ruta /api/bot/liquidar registrada con autenticacion API", flush=True)
            return _legacy_function

        return decorator

    FastAPI.middleware = _secure_middleware
    FastAPI.post = _secure_post
    print("[SITECUSTOMIZE] Adaptador del bot cargado correctamente", flush=True)
except Exception as exc:
    # Never prevent the ERP from starting if the compatibility hook itself fails.
    print(f"[SITECUSTOMIZE] No se pudo cargar el adaptador: {exc}", flush=True)
