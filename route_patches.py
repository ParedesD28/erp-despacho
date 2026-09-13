"""Correcciones puntuales aplicadas después del registro de compat_routes."""
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from psycopg2.extras import RealDictCursor
import main


def _conn():
    return main.db_pool.getconn()


def _release(conn):
    main.db_pool.putconn(conn)


def _replace_detail_route():
    # FastAPI conserva las rutas en orden. Quitamos la versión con GROUP BY
    # incompatible con SELECT p.* y dejamos una implementación estable.
    routes = main.app.router.routes
    main.app.router.routes[:] = [
        r for r in routes
        if not (getattr(r, "path", None) == "/expediente/{radicado}" and "GET" in getattr(r, "methods", set()))
    ]

    @main.app.get("/expediente/{radicado}")
    def detalle_expediente_corregido(request: Request, radicado: str):
        conn = _conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT p.*, c.nombre AS demandante_db, a.nombre AS abogado_asignado
                    FROM procesos p
                    LEFT JOIN contactos c ON c.identificacion=p.id_cliente
                    LEFT JOIN abogados a ON a.id=p.abogado_id
                    WHERE p.radicado_interno=%s
                    LIMIT 1
                """, (radicado,))
                proceso = cur.fetchone()
                if not proceso:
                    raise HTTPException(status_code=404, detail="Expediente no encontrado")
                proceso = dict(proceso)
                cur.execute("""
                    SELECT pl.identificacion_demandado, c.nombre
                    FROM procesos_litisconsorcio pl
                    LEFT JOIN contactos c ON c.identificacion=pl.identificacion_demandado
                    WHERE pl.radicado_interno=%s
                    ORDER BY c.nombre NULLS LAST
                """, (radicado,))
                demandados = cur.fetchall()
                if demandados:
                    proceso["id_demandado"] = " | ".join(str(r[0]) for r in demandados if r[0])
                    proceso["demandado"] = " | ".join(str(r[1] or r[0]) for r in demandados)
                cur.execute("SELECT * FROM actuaciones WHERE radicado_interno=%s ORDER BY fecha DESC, id DESC", (radicado,))
                actuaciones = [dict(r) for r in cur.fetchall()]
            return main.templates.TemplateResponse(
                request=request,
                name="detalle_expediente.html",
                context={"request": request, "proceso": proceso, "actuaciones": actuaciones},
            )
        finally:
            _release(conn)


_replace_detail_route()
print("[ROUTE_PATCHES] Detalle de expediente corregido", flush=True)
