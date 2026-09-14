"""Normaliza la seguridad web antes de instalar el middleware productivo.

El ERP conserva bastante código legacy dentro de ``main.py``. Ese archivo aún
registraba un middleware que consideraba autenticado a cualquier valor presente
en ``token_erp``. El arranque productivo usa ahora sesiones HMAC verificadas en
``security.py``; mantener ambos guardias crea dos fuentes de verdad y permite
que una modificación futura vuelva a introducir el esquema legacy.

Este módulo se carga desde ``extensions.py`` antes de instalar el middleware
productivo de ``start.py``. Solo elimina el middleware legacy identificado por
su nombre; no altera rutas ni lógica de negocio.
"""

import main


def remove_legacy_auth_middleware() -> None:
    """Deja una sola fuente de verdad para autenticación web en producción."""
    middleware = getattr(main.app, "user_middleware", None)
    if middleware is None:
        return

    before = len(middleware)
    main.app.user_middleware[:] = [
        item
        for item in middleware
        if getattr(item.kwargs.get("dispatch"), "__name__", "")
        != "validador_general_seguridad"
    ]
    removed = before - len(main.app.user_middleware)
    if removed:
        print(
            f"[SECURITY] Middleware legacy eliminado: {removed}; "
            "la sesión HMAC de start.py es la única guardia web.",
            flush=True,
        )


remove_legacy_auth_middleware()
