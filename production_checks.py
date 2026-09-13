"""Checks de disponibilidad para producción del ERP."""

import os

from fastapi.responses import JSONResponse

import main


def _db_ready():
    try:
        pool = getattr(main, "db_pool", None)
        if pool is None:
            return False, "db_pool no disponible"
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True, "ok"
        finally:
            pool.putconn(conn)
    except Exception as exc:
        print(f"[HEALTH][DB] {repr(exc)}", flush=True)
        return False, "conexion db no disponible"


# Reemplazo determinista del health check simple por uno de disponibilidad real.
main.app.router.routes[:] = [
    r for r in main.app.router.routes
    if getattr(r, "path", None) != "/health"
]


@main.app.get("/health")
def healthcheck_produccion():
    ready, db_message = _db_ready()
    required = [
        "DATABASE_URL",
        "ERP_SESSION_SECRET",
        "LIQUIDADOR_API_KEY",
        "PUBLIC_BASE_URL",
    ]
    missing = [name for name in required if not os.getenv(name)]
    ok = ready and not missing
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "database": db_message,
            "missing_env": missing,
        },
    )


print("[PRODUCTION] Health check DB + variables activado", flush=True)
