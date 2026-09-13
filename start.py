"""Render startup wrapper for the ERP.

The legacy global auth middleware is registered inside main.py and requires the
browser cookie token_erp for every route. The machine-to-machine bot endpoint
already authenticates independently with X-API-Key in bot_api.py, so we replace
only that middleware dispatch before Uvicorn builds the middleware stack.
"""

import os

import uvicorn

import main


BOT_PATH = "/api/bot/liquidar"


def _bypass_bot_auth_middleware() -> None:
    """Make the bot route bypass the browser-session middleware only."""
    user_middleware = getattr(main.app, "user_middleware", [])

    for middleware in user_middleware:
        dispatch = middleware.kwargs.get("dispatch")
        if dispatch is None:
            continue

        name = getattr(dispatch, "__name__", "")
        if name != "validador_general_seguridad":
            continue

        original_dispatch = dispatch

        async def guarded_dispatch(request, call_next, _original=original_dispatch):
            if request.url.path == BOT_PATH:
                # bot_api.py validates X-API-Key itself.
                return await call_next(request)
            return await _original(request, call_next)

        middleware.kwargs["dispatch"] = guarded_dispatch
        print("[START] Middleware browser-session adaptado para /api/bot/liquidar", flush=True)
        return

    print("[START] No se encontro validador_general_seguridad", flush=True)


_bypass_bot_auth_middleware()

if __name__ == "__main__":
    uvicorn.run(
        main.app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
