"""Correccion de anulado para gestiones CRM.

La anulacion es reversible/auditable: no borra registros, solo marca la gestion
como anulada y la excluye de la vista operativa del CRM.
"""
from fastapi import Form
from fastapi.responses import RedirectResponse
from psycopg2.extras import RealDictCursor
import main

_installed = False


def _conn():
    return main.db_pool.getconn()


def _release(conn):
    main.db_pool.putconn(conn)


def _table_exists(cur, table):
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s)",
        (table,),
    )
    row = cur.fetchone()
    return bool(row["exists"] if isinstance(row, dict) else row[0])


def _table_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return {
        r["column_name"] if isinstance(r, dict) else r[0]
        for r in cur.fetchall()
    }


def _wrap_crm_get():
    for route in main.app.router.routes:
        if getattr(route, "path", None) != "/crm" or "GET" not in getattr(route, "methods", set()):
            continue
        if getattr(route.endpoint, "_anulado_filter_wrapped", False):
            return
        original = route.endpoint

        def crm_wrapped(request, buscar_inmueble=None):
            response = original(request, buscar_inmueble)
            context = getattr(response, "context", None)
            if not isinstance(context, dict):
                return response
            historial = context.get("historial") or []
            context["historial"] = [
                item for item in historial
                if not bool(item.get("anulado"))
                and str(item.get("estado") or "").strip().upper() != "ANULADO"
            ]
            return response

        crm_wrapped._anulado_filter_wrapped = True
        route.endpoint = crm_wrapped
        # Starlette/FastAPI ya conserva el callable en dependant.call.
        if hasattr(route, "dependant"):
            route.dependant.call = crm_wrapped
        return


def install():
    global _installed
    if _installed:
        return

    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if not _table_exists(cur, "gestiones_crm"):
                    print("[CRM] gestiones_crm no existe; no se instala anulación", flush=True)
                    return
                cols = _table_columns(cur, "gestiones_crm")
                if "anulado" not in cols:
                    cur.execute(
                        "ALTER TABLE gestiones_crm ADD COLUMN anulado BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_gestiones_crm_inmueble_anulado "
                    "ON gestiones_crm (inmueble_id, anulado)"
                )
    except Exception as exc:
        print(f"[CRM] Error preparando anulación: {exc!r}", flush=True)
        return
    finally:
        _release(conn)

    main.app.router.routes[:] = [
        r for r in main.app.router.routes
        if not (getattr(r, "path", None) == "/crm/anular" and "POST" in getattr(r, "methods", set()))
    ]

    @main.app.post("/crm/anular")
    def crm_anular_corregido(
        gestion_id: int = Form(...),
        inmueble_id: int = Form(...),
    ):
        conn = _conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id FROM gestiones_crm WHERE id=%s AND inmueble_id=%s LIMIT 1",
                        (gestion_id, inmueble_id),
                    )
                    if not cur.fetchone():
                        return RedirectResponse(
                            f"/crm?buscar_inmueble={inmueble_id}&error=Gestion+no+encontrada",
                            status_code=303,
                        )
                    cols = _table_columns(cur, "gestiones_crm")
                    if "estado" in cols:
                        cur.execute(
                            "UPDATE gestiones_crm SET anulado=TRUE, estado='ANULADO' "
                            "WHERE id=%s AND inmueble_id=%s",
                            (gestion_id, inmueble_id),
                        )
                    else:
                        cur.execute(
                            "UPDATE gestiones_crm SET anulado=TRUE "
                            "WHERE id=%s AND inmueble_id=%s",
                            (gestion_id, inmueble_id),
                        )
            print(f"[CRM] Gestion {gestion_id} anulada para inmueble {inmueble_id}", flush=True)
            return RedirectResponse(
                f"/crm?buscar_inmueble={inmueble_id}&mensaje=Gestion+anulada",
                status_code=303,
            )
        except Exception as exc:
            print(f"[CRM] Error anulando gestion {gestion_id}: {exc!r}", flush=True)
            return RedirectResponse(
                f"/crm?buscar_inmueble={inmueble_id}&error=No+fue+posible+anular+la+gestion",
                status_code=303,
            )
        finally:
            _release(conn)

    _wrap_crm_get()
    _installed = True
    print("[CRM] Anulado de gestiones habilitado; los registros anulados se ocultan del historial", flush=True)


install()
