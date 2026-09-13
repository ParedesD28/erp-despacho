import os
from functools import wraps

_API_PATH = "/api/bot/liquidar"


def _api_key_ok(request):
    expected = os.getenv("LIQUIDADOR_API_KEY")
    supplied = request.headers.get("X-API-Key")
    return bool(expected and supplied and supplied == expected)


try:
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware

    _original_middleware = FastAPI.middleware
    _original_add_middleware = FastAPI.add_middleware
    _original_post = FastAPI.post

    def _secure_middleware(self, middleware_type):
        decorator = _original_middleware(self, middleware_type)

        def register(func):
            if middleware_type != "http":
                return decorator(func)

            @wraps(func)
            async def guarded(request, call_next):
                if request.url.path == _API_PATH and _api_key_ok(request):
                    return await call_next(request)
                return await func(request, call_next)

            return decorator(guarded)

        return register

    def _secure_add_middleware(self, middleware_class, *args, **kwargs):
        if middleware_class is BaseHTTPMiddleware:
            dispatch = kwargs.get("dispatch")
            if dispatch is not None:
                original_dispatch = dispatch

                @wraps(original_dispatch)
                async def guarded_dispatch(request, call_next):
                    if request.url.path == _API_PATH and _api_key_ok(request):
                        return await call_next(request)
                    return await original_dispatch(request, call_next)

                kwargs["dispatch"] = guarded_dispatch

        return _original_add_middleware(self, middleware_class, *args, **kwargs)

    def _secure_post(self, path, *args, **kwargs):
        if path != _API_PATH:
            return _original_post(self, path, *args, **kwargs)

        def decorator(_legacy_function):
            from bot_api import liquidar_para_bot
            route_kwargs = dict(kwargs)
            for key in (
                "response_model", "status_code", "responses", "deprecated",
                "operation_id", "summary", "description", "response_description",
                "tags", "dependencies", "callbacks", "openapi_extra", "include_in_schema",
            ):
                route_kwargs.pop(key, None)
            self.add_api_route(path, liquidar_para_bot, methods=["POST"], include_in_schema=True, **route_kwargs)
            print("[SITECUSTOMIZE] Ruta /api/bot/liquidar registrada con autenticacion API", flush=True)
            return _legacy_function

        return decorator

    FastAPI.middleware = _secure_middleware
    FastAPI.add_middleware = _secure_add_middleware
    FastAPI.post = _secure_post
    print("[SITECUSTOMIZE] Adaptador del bot cargado correctamente", flush=True)
except Exception as exc:
    print(f"[SITECUSTOMIZE] No se pudo cargar el adaptador: {exc}", flush=True)
