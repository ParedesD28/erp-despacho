"""Supervision de solo lectura del agente de cobranza.

No modifica conversaciones, Neon ni la integracion con WhatsApp. Expone una
vista administrativa que inspecciona el estado de la configuracion del agente,
el endpoint M2M del liquidador y el historial disponible en gestiones_cartera.
"""
import os
from datetime import datetime

from fastapi import Request
from psycopg2.extras import RealDictCursor

import main


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


def _columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    rows = cur.fetchall()
    return {
        row["column_name"] if isinstance(row, dict) else row[0]
        for row in rows
    }


def _first(columns, *candidates):
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _estado_integracion():
    return {
        "api_key": bool(os.getenv("LIQUIDADOR_API_KEY")),
        "base_url": bool(os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")),
        "database": bool(os.getenv("DATABASE_URL")),
    }


@main.app.get("/supervision-agente")
def supervision_agente(request: Request):
    estado = _estado_integracion()
    resumen = {
        "interacciones": 0,
        "hoy": 0,
        "errores": 0,
        "ultimo_evento": None,
    }
    eventos = []
    fuente = "Sin tabla de gestiones_cartera"

    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if _table_exists(cur, "gestiones_cartera"):
                cols = _columns(cur, "gestiones_cartera")
                identificacion = _first(cols, "identificacion_deudor", "cedula", "identificacion")
                tipo = _first(cols, "tipo_contacto", "tipo", "canal")
                resumen_col = _first(cols, "resumen", "mensaje", "descripcion", "detalle")
                fecha_col = _first(cols, "fecha", "fecha_gestion", "created_at", "timestamp", "fecha_registro", "created")
                estado_col = _first(cols, "estado", "status", "resultado")
                id_col = _first(cols, "id")

                if fecha_col:
                    order_expr = f'"{fecha_col}" DESC NULLS LAST'
                elif id_col:
                    order_expr = f'"{id_col}" DESC'
                else:
                    order_expr = "1"

                total_expr = "COUNT(*)"
                cur.execute(f'SELECT {total_expr} AS total FROM gestiones_cartera')
                resumen["interacciones"] = int(cur.fetchone()["total"])

                if fecha_col:
                    cur.execute(
                        f"SELECT COUNT(*) AS total FROM gestiones_cartera "
                        f"WHERE DATE(\"{fecha_col}\")=CURRENT_DATE"
                    )
                    resumen["hoy"] = int(cur.fetchone()["total"])

                if estado_col:
                    cur.execute(
                        f"SELECT COUNT(*) AS total FROM gestiones_cartera "
                        f"WHERE LOWER(COALESCE(\"{estado_col}\"::text,'')) LIKE ANY(%s)",
                        (["%error%", "%fall%", "%rechaz%"],),
                    )
                    resumen["errores"] = int(cur.fetchone()["total"])

                select_parts = []
                select_parts.append(f'"{id_col}" AS id' if id_col else "NULL::bigint AS id")
                select_parts.append(f'"{identificacion}" AS identificacion' if identificacion else "NULL::text AS identificacion")
                select_parts.append(f'"{tipo}" AS tipo' if tipo else "NULL::text AS tipo")
                select_parts.append(f'"{resumen_col}" AS resumen' if resumen_col else "NULL::text AS resumen")
                select_parts.append(f'"{estado_col}" AS estado' if estado_col else "NULL::text AS estado")
                select_parts.append(f'"{fecha_col}" AS fecha' if fecha_col else "NULL::timestamp AS fecha")

                cur.execute(
                    "SELECT " + ", ".join(select_parts) +
                    f" FROM gestiones_cartera ORDER BY {order_expr} LIMIT 100"
                )
                eventos = [dict(row) for row in cur.fetchall()]
                resumen["ultimo_evento"] = eventos[0].get("fecha") if eventos else None
                fuente = "gestiones_cartera"
    except Exception as exc:
        print(f"[SUPERVISION AGENTE] Lectura no disponible: {exc!r}", flush=True)
        fuente = "Error de lectura de gestiones_cartera"
    finally:
        _release(conn)

    activo = all(estado.values())
    return main.templates.TemplateResponse(
        request=request,
        name="supervision_agente.html",
        context={
            "request": request,
            "estado": estado,
            "activo": activo,
            "resumen": resumen,
            "eventos": eventos,
            "fuente": fuente,
        },
    )


print("[SUPERVISION AGENTE] Modulo de supervision cargado en modo solo lectura", flush=True)
