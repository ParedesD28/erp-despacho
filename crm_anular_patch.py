"""Correccion de anulado para gestiones CRM y de cartera del agente.

Mantiene los registros para auditoria y solo cambia su visibilidad operativa.
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


def _filter_anulados(historial, inmueble_id, cur):
    if not historial or not _table_exists(cur, "gestiones_cartera"):
        return historial
    cur.execute("SELECT id FROM gestiones_crm WHERE inmueble_id=%s", (inmueble_id,))
    crm_ids = {int(r[0]) for r in cur.fetchall()}
    ids = {
        int(item.get("id"))
        for item in historial
        if item.get("id") is not None and int(item.get("id")) not in crm_ids
    }
    if not ids:
        return historial
    cur.execute(
        "SELECT id FROM gestiones_cartera WHERE anulado=TRUE AND id = ANY(%s)",
        (list(ids),),
    )
    anulados = {int(r[0]) for r in cur.fetchall()}
    return [item for item in historial if item.get("id") is None or int(item.get("id")) not in anulados]


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
            if not isinstance(context, dict) or not buscar_inmueble:
                return response
            historial = context.get("historial") or []
            if not historial:
                return response
            conn = _conn()
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    context["historial"] = _filter_anulados(historial, int(buscar_inmueble), cur)
            except Exception as exc:
                print(f"[CRM] No fue posible filtrar anuladas: {exc!r}", flush=True)
            finally:
                _release(conn)
            return response

        crm_wrapped._anulado_filter_wrapped = True
        route.endpoint = crm_wrapped
        return


def install():
    global _installed
    if _installed:
        return
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                if _table_exists(cur, "gestiones_cartera"):
                    cur.execute(
                        "ALTER TABLE gestiones_cartera ADD COLUMN IF NOT EXISTS anulado BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_gestiones_cartera_anulado ON gestiones_cartera (anulado)"
                    )
    finally:
        _release(conn)

    main.app.router.routes[:] = [
        r for r in main.app.router.routes
        if not (getattr(r, "path", None) == "/crm/anular" and "POST" in getattr(r, "methods", set()))
    ]

    @main.app.post("/crm/anular")
    def crm_anular_corregido(gestion_id: int = Form(...), inmueble_id: int = Form(...)):
        conn = _conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id FROM gestiones_crm WHERE id=%s AND inmueble_id=%s LIMIT 1",
                        (gestion_id, inmueble_id),
                    )
                    if cur.fetchone():
                        cur.execute(
                            "UPDATE gestiones_crm SET anulado=TRUE, estado='ANULADO' WHERE id=%s AND inmueble_id=%s",
                            (gestion_id, inmueble_id),
                        )
                        return RedirectResponse(f"/crm?buscar_inmueble={inmueble_id}&mensaje=Gestion+anulada", status_code=303)
                    cur.execute("SELECT cedula FROM inmuebles_ph WHERE id=%s LIMIT 1", (inmueble_id,))
                    inmueble = cur.fetchone()
                    cedula = str(inmueble["cedula"]) if inmueble and inmueble.get("cedula") is not None else ""
                    if _table_exists(cur, "gestiones_cartera") and cedula:
                        cur.execute(
                            "SELECT id FROM gestiones_cartera WHERE id=%s AND REGEXP_REPLACE(COALESCE(identificacion_deudor::text,''),'[^0-9]','','g')=REGEXP_REPLACE(%s,'[^0-9]','','g') LIMIT 1",
                            (gestion_id, cedula),
                        )
                        if cur.fetchone():
                            cur.execute(
                                "UPDATE gestiones_cartera SET anulado=TRUE WHERE id=%s AND REGEXP_REPLACE(COALESCE(identificacion_deudor::text,''),'[^0-9]','','g')=REGEXP_REPLACE(%s,'[^0-9]','','g')",
                                (gestion_id, cedula),
                            )
                            return RedirectResponse(f"/crm?buscar_inmueble={inmueble_id}&mensaje=Gestion+anulada", status_code=303)
            return RedirectResponse(f"/crm?buscar_inmueble={inmueble_id}&error=Gestion+no+encontrada", status_code=303)
        except Exception as exc:
            print(f"[CRM] Error anulando gestion: {exc!r}", flush=True)
            return RedirectResponse(f"/crm?buscar_inmueble={inmueble_id}&error=No+fue+posible+anular+la+gestion", status_code=303)
        finally:
            _release(conn)

    _wrap_crm_get()
    _installed = True
    print("[CRM] Anulado de gestiones manuales y del agente habilitado", flush=True)


install()
