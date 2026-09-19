"""Servicio y lógica de negocio unificada para Expedientes Judiciales."""
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
import json
import unicodedata
import time

import pandas as pd
from psycopg2.extras import RealDictCursor

import db

CANONICAL_STAGES = [
    "1. Presentación de la demanda",
    "2. Inadmisión",
    "3. Admisión",
    "4. Medidas Cautelares",
    "5. Notificación",
    "6. Excepciones",
    "7. Sentencia",
    "8. Desistimiento tácito",
    "Auto de Trámite / General",
    "Terminación del Proceso",
]
CANONICAL = set(CANONICAL_STAGES)
TIPOS_CARTERA = {"PREJURIDICO", "JURIDICO"}


def _row_value(row, key_or_index, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        if key_or_index in row:
            return row[key_or_index]
        if isinstance(key_or_index, int):
            values = list(row.values())
            return values[key_or_index] if 0 <= key_or_index < len(values) else default
        return default
    try:
        return row[key_or_index]
    except (KeyError, IndexError, TypeError):
        return default


def _table_exists_without_cartera(cur, table):
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s)",
        (table,),
    )
    row = cur.fetchone()
    return bool(_row_value(row, 0, False))


def _ensure_inmueble_propietarios_schema(cur):
    """Verifica la relación normalizada inmueble -> propietarios sin mutar el esquema."""
    required = {
        "inmueble_propietarios": {"inmueble_id", "contacto_id", "es_principal"},
        "inmuebles_ph": {"id", "contacto_id"},
        "contactos": {"id", "identificacion"},
    }
    for table, expected in required.items():
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            """,
            (table,),
        )
        actual = {str(row[0]) for row in cur.fetchall()}
        if not actual:
            raise RuntimeError(f"[SCHEMA PREFLIGHT] Falta tabla: {table}")
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(
                f"[SCHEMA PREFLIGHT] Faltan columnas en {table}: {', '.join(missing)}"
            )


def _ensure_cartera_schema(cur):
    """Verifica el contrato canónico de procesos sin ejecutar DDL en runtime."""
    required = {"tipo_cartera", "estado_rama", "tipo_proceso_id", "naturaleza"}
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='procesos'
        """
    )
    actual = {str(row[0]) for row in cur.fetchall()}
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            "[SCHEMA PREFLIGHT] Faltan columnas en procesos: " + ", ".join(missing)
        )
    _ensure_inmueble_propietarios_schema(cur)


def _cols(cur, table):
    if table == "procesos":
        _ensure_cartera_schema(cur)
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    values = set()
    for row in cur.fetchall():
        value = _row_value(row, "column_name", _row_value(row, 0))
        if value:
            values.add(str(value))
    return values


def _table_exists(cur, table):
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s) AS exists_table",
        (table,),
    )
    row = cur.fetchone()
    return bool(_row_value(row, "exists_table", _row_value(row, 0, False)))


def _norm(value):
    text = str(value or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _stage_from_act(etapa, descripcion, tipificacion):
    explicit = str(etapa or "").strip()
    if explicit in CANONICAL:
        return explicit
    n = _norm(" ".join(str(x or "") for x in (etapa, tipificacion, descripcion)))
    if any(k in n for k in ("terminacion del proceso", "archivo definitivo", "paz y salvo procesal")):
        return "Terminación del Proceso"
    if any(k in n for k in ("sentencia ejecutoriada", "sentencia", "fallo", "condena")):
        return "7. Sentencia"
    if "desistimiento tacito" in n:
        return "8. Desistimiento tácito"
    if any(k in n for k in ("excepcion", "excepciones", "contestacion a excepciones", "traslado de excepciones")):
        return "6. Excepciones"
    if any(k in n for k in ("notificacion", "notificacion personal", "citacion", "emplazamiento")):
        return "5. Notificación"
    if any(k in n for k in ("medida cautelar", "medidas cautelares", "embargo", "secuestro", "oficio de embargo", "libramiento de embargo")):
        return "4. Medidas Cautelares"
    if any(k in n for k in ("inadmis", "subsanacion")):
        return "2. Inadmisión"
    if any(k in n for k in ("admision", "auto admite", "mandamiento ejecutivo", "libra mandamiento", "solicitud de oficios")):
        return "3. Admisión"
    if any(k in n for k in ("presentacion de la demanda", "radicacion", "reparto", "reparto y radicacion", "inicio")):
        return "1. Presentación de la demanda"
    return None


def _sync_stage(cur, radicado):
    cols = _cols(cur, "procesos")
    if "tipo_cartera" in cols:
        cur.execute("SELECT tipo_cartera FROM procesos WHERE radicado_interno=%s LIMIT 1", (radicado,))
        row_tipo = cur.fetchone()
        if row_tipo and str(_row_value(row_tipo, "tipo_cartera", _row_value(row_tipo, 0)) or "").upper() == "PREJURIDICO":
            return None
    if "etapa_actual" not in cols or not _table_exists(cur, "actuaciones"):
        return None
    act_cols = _cols(cur, "actuaciones")
    available = [c for c in ("etapa", "descripcion", "tipificacion_sugerida") if c in act_cols]
    if not available:
        return None
    select = ", ".join(available)
    order_col = "fecha" if "fecha" in act_cols else "id"
    id_clause = ", id DESC" if "id" in act_cols else ""
    cur.execute(f"SELECT {select} FROM actuaciones WHERE radicado_interno=%s ORDER BY {order_col} DESC NULLS LAST{id_clause} LIMIT 1", (radicado,))
    act = cur.fetchone()
    if not act:
        return None
    values = {name: _row_value(act, name, _row_value(act, idx)) for idx, name in enumerate(available)}
    stage = _stage_from_act(values.get("etapa"), values.get("descripcion"), values.get("tipificacion_sugerida"))
    if not stage:
        return None
    cur.execute("UPDATE procesos SET etapa_actual=%s WHERE radicado_interno=%s", (stage, radicado))
    return stage


def _parse_money(value, default=None):
    text = str(value or "").strip().replace("$", "").replace(" ", "")
    if not text:
        return default
    try:
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        elif text.count(".") > 1:
            text = text.replace(".", "")
        elif "." in text:
            integer, fraction = text.split(".", 1)
            if len(fraction) == 3 and integer.replace("-", "").isdigit():
                text = integer + fraction
        return Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("El valor de pretensiones no es válido")


def _ensure_audit_table(cur):
    """Verifica la auditoría estructurada; la tabla se crea únicamente por migración."""
    if not _table_exists(cur, "expediente_ediciones"):
        raise RuntimeError("[SCHEMA PREFLIGHT] Falta tabla: expediente_ediciones")


def _get_inmueble_info(cur, inmueble_id):
    if not inmueble_id or not _table_exists(cur, "inmuebles_ph"):
        return {}, []

    cur.execute("""
        SELECT id, conjunto_residencial, torre_apto, contacto_id
        FROM inmuebles_ph
        WHERE id=%s
        LIMIT 1
    """, (inmueble_id,))
    row = cur.fetchone()
    if not row:
        return {}, []

    inmueble = {
        "id": _row_value(row, "id", _row_value(row, 0)),
        "conjunto_residencial": _row_value(row, "conjunto_residencial", _row_value(row, 1)) or "",
        "torre_apto": _row_value(row, "torre_apto", _row_value(row, 2)) or "",
        "contacto_id": _row_value(row, "contacto_id", _row_value(row, 3)),
    }

    propietarios = []
    if _table_exists(cur, "inmueble_propietarios") and _table_exists(cur, "contactos"):
        cur.execute("""
            SELECT c.identificacion, c.nombre, c.telefono, c.email,
                   ip.es_principal
            FROM inmueble_propietarios ip
            JOIN contactos c ON c.id=ip.contacto_id
            WHERE ip.inmueble_id=%s
            ORDER BY ip.es_principal DESC, c.nombre ASC
        """, (inmueble_id,))
        propietarios = [
            {
                "identificacion": _row_value(r, "identificacion", _row_value(r, 0)),
                "nombre": _row_value(r, "nombre", _row_value(r, 1)),
                "telefono": _row_value(r, "telefono", _row_value(r, 2)) or "",
                "email": _row_value(r, "email", _row_value(r, 3)) or "",
                "es_principal": bool(_row_value(r, "es_principal", _row_value(r, 4, False))),
            }
            for r in cur.fetchall()
        ]

    if not propietarios and inmueble.get("contacto_id") and _table_exists(cur, "contactos"):
        cur.execute("SELECT identificacion, nombre, telefono, email FROM contactos WHERE id=%s", (inmueble["contacto_id"],))
        owner = cur.fetchone()
        if owner:
            propietarios = [{
                "identificacion": _row_value(owner, "identificacion", _row_value(owner, 0)),
                "nombre": _row_value(owner, "nombre", _row_value(owner, 1)),
                "telefono": _row_value(owner, "telefono", _row_value(owner, 2)) or "",
                "email": _row_value(owner, "email", _row_value(owner, 3)) or "",
                "es_principal": True,
            }]

    return inmueble, propietarios


def _get_process(cur, radicado):
    cols = _cols(cur, "procesos")
    wanted = [
        "radicado_interno", "radicado_rama", "estado_rama", "tipo_cartera",
        "tipo_proceso_id", "naturaleza", "juzgado", "etapa_actual", "estado",
        "pretensiones", "medidas_cautelares", "abogado_id", "inmueble_id",
        "fecha_radicacion", "torre_apto",
    ]
    avail = [c for c in wanted if c in cols]
    if not avail:
        return None
    cur.execute(f"SELECT {', '.join(avail)} FROM procesos WHERE radicado_interno=%s LIMIT 1", (radicado,))
    row = cur.fetchone()
    if not row:
        return None
    proc = {c: _row_value(row, c, _row_value(row, idx)) for idx, c in enumerate(avail)}
    inmueble, propietarios = _get_inmueble_info(cur, proc.get("inmueble_id"))
    proc["inmueble"] = inmueble
    proc["propietarios_inmueble"] = propietarios
    if _table_exists(cur, "abogados") and proc.get("abogado_id"):
        cur.execute("SELECT nombre FROM abogados WHERE id=%s", (proc["abogado_id"],))
        ab_row = cur.fetchone()
        if ab_row:
            proc["abogado_asignado"] = _row_value(ab_row, "nombre", _row_value(ab_row, 0))
    return proc

def _get_demandantes(cur, proceso):
    if not proceso:
        return []
    radicado = str(proceso.get("radicado_interno") or "").strip()
    if not radicado:
        return []

    cur.execute(
        """
        SELECT c.identificacion, c.nombre, c.tipo, c.telefono, c.email,
               c.direccion, c.ciudad, pp.es_principal
        FROM proceso_partes pp
        JOIN contactos c ON c.id = pp.contacto_id
        WHERE pp.radicado_interno=%s
          AND UPPER(pp.rol)='DEMANDANTE'
        ORDER BY pp.es_principal DESC, pp.id ASC
        """,
        (radicado,),
    )
    return [dict(r) for r in cur.fetchall()]


def _get_demandados(cur, radicado):
    radicado = str(radicado or "").strip()
    if not radicado:
        return []

    cur.execute(
        """
        SELECT c.identificacion, c.nombre, c.tipo, c.telefono, c.email,
               c.direccion, c.ciudad, pp.es_principal
        FROM proceso_partes pp
        JOIN contactos c ON c.id = pp.contacto_id
        WHERE pp.radicado_interno=%s
          AND UPPER(pp.rol)='DEMANDADO'
        ORDER BY pp.es_principal DESC, pp.id ASC
        """,
        (radicado,),
    )
    return [dict(r) for r in cur.fetchall()]


def _get_actuaciones(cur, radicado):
    if not _table_exists(cur, "actuaciones"):
        return []
    cols = _cols(cur, "actuaciones")
    wanted = ["id", "radicado_interno", "fecha", "etapa", "descripcion", "usuario", "tipificacion_sugerida", "archivo"]
    avail = [c for c in wanted if c in cols]
    order_col = "fecha" if "fecha" in cols else "id"
    id_clause = ", id DESC" if "id" in cols else ""
    cur.execute(f"SELECT {', '.join(avail)} FROM actuaciones WHERE radicado_interno=%s ORDER BY {order_col} DESC NULLS LAST{id_clause}", (radicado,))
    rows = cur.fetchall()
    return [{c: _row_value(r, c, _row_value(r, idx)) for idx, c in enumerate(avail)} for r in rows]


def _get_crm_agreements(cur, inmueble_id, identificaciones, obligacion_id=None):
    """Carga CRM priorizando la obligación; mantiene fallback histórico por inmueble/persona."""
    if not _table_exists(cur, "gestiones_crm"):
        return []

    queries, params = [], []
    if obligacion_id:
        queries.append("obligacion_id=%s")
        params.append(int(obligacion_id))
    if inmueble_id:
        queries.append("inmueble_id=%s")
        params.append(inmueble_id)

    clean_ids = [str(x).strip() for x in (identificaciones or []) if str(x).strip()]
    if clean_ids:
        placeholders = ",".join(["%s"] * len(clean_ids))
        queries.append(f"identificacion_deudor IN ({placeholders})")
        params.extend(clean_ids)

    if not queries:
        return []

    cur.execute(
        f"""
        SELECT id, fecha, resumen, promesa_pago_fecha, tipo_contacto,
               usuario, COALESCE(estado,'ACTIVO') AS estado,
               obligacion_id, radicado_interno
        FROM gestiones_crm
        WHERE ({' OR '.join(queries)})
          AND COALESCE(anulado,FALSE)=FALSE
        ORDER BY CASE WHEN obligacion_id=%s THEN 0 ELSE 1 END,
                 fecha DESC
        LIMIT 50
        """,
        params + [int(obligacion_id) if obligacion_id else None],
    )
    rows = cur.fetchall()
    return [
        {
            "id": _row_value(r,"id"),
            "fecha": _row_value(r,"fecha"),
            "resumen": _row_value(r,"resumen"),
            "promesa_pago_fecha": _row_value(r,"promesa_pago_fecha"),
            "tipo_contacto": _row_value(r,"tipo_contacto"),
            "usuario": _row_value(r,"usuario"),
            "estado": _row_value(r,"estado"),
            "obligacion_id": _row_value(r,"obligacion_id"),
            "radicado_interno": _row_value(r,"radicado_interno"),
        }
        for r in rows
    ]


def _get_abogados(cur):
    if not _table_exists(cur, "abogados"):
        return []
    cur.execute("SELECT id, nombre FROM abogados ORDER BY nombre ASC")
    return [{"id":_row_value(r,"id",_row_value(r,0)),"nombre":_row_value(r,"nombre",_row_value(r,1))} for r in cur.fetchall()]


def _contact_options(cur):
    if not _table_exists(cur, "contactos"):
        return []
    cols = _cols(cur, "contactos")
    wanted = ["identificacion", "nombre", "tipo", "telefono", "email", "direccion", "ciudad"]
    avail = [c for c in wanted if c in cols]
    cur.execute(f"SELECT {', '.join(avail)} FROM contactos ORDER BY nombre ASC LIMIT 1000")
    return [{c:_row_value(r,c,_row_value(r,idx)) for idx,c in enumerate(avail)} for r in cur.fetchall()]


def _audit(cur, radicado):
    if not _table_exists(cur, "expediente_ediciones"):
        return []
    cur.execute("SELECT id, fecha, usuario, accion FROM expediente_ediciones WHERE radicado_interno=%s ORDER BY fecha DESC LIMIT 20", (radicado,))
    return [{"id":_row_value(r,"id"),"fecha":_row_value(r,"fecha"),"usuario":_row_value(r,"usuario"),"accion":_row_value(r,"accion")} for r in cur.fetchall()]


def cargar_procesos_general_sin_duplicados():
    hora = time.strftime("%H:%M:%S")
    print(f"[{hora} UTC] 📂 [EXPEDIENTES] Consultando base de datos...", flush=True)
    t0 = time.perf_counter()
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        p.radicado_interno,
                        p.radicado_rama,
                        p.tipo_cartera,
                        p.naturaleza,
                        p.juzgado,
                        p.etapa_actual,
                        p.estado,
                        p.pretensiones,
                        p.medidas_cautelares,
                        COALESCE(ppdemandante.nombres, 'SIN REGISTRO') AS demandante_nombre,
                        COALESCE(ppdemandado.nombres, 'SIN REGISTRO') AS demandado_nombre,
                        a.nombre AS abogado_asignado
                    FROM procesos p
                    LEFT JOIN abogados a ON p.abogado_id = a.id
                    LEFT JOIN LATERAL (
                        SELECT STRING_AGG(DISTINCT c.nombre, ' | ' ORDER BY c.nombre) AS nombres
                        FROM proceso_partes pp
                        JOIN contactos c ON c.id=pp.contacto_id
                        WHERE pp.radicado_interno=p.radicado_interno
                          AND UPPER(pp.rol)='DEMANDANTE'
                    ) ppdemandante ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT STRING_AGG(DISTINCT c.nombre, ' | ' ORDER BY c.nombre) AS nombres
                        FROM proceso_partes pp
                        JOIN contactos c ON c.id=pp.contacto_id
                        WHERE pp.radicado_interno=p.radicado_interno
                          AND UPPER(pp.rol)='DEMANDADO'
                    ) ppdemandado ON TRUE
                    ORDER BY p.radicado_interno DESC
                    """
                )
                rows = cur.fetchall()
                lista = []
                for r in rows:
                    d = dict(r)
                    d["tipo_cartera"] = str(d.get("tipo_cartera") or "JURIDICO").upper()
                    for k, v in d.items():
                        if v is None:
                            d[k] = ""
                    lista.append(d)
                print(
                    f"[{time.strftime('%H:%M:%S')} UTC] 📂 [EXPEDIENTES] "
                    f"{len(lista)} registros cargados exitosamente "
                    f"({round((time.perf_counter()-t0)*1000,1)}ms)",
                    flush=True,
                )
                return lista
    except Exception as exc:
        print(f"[{time.strftime('%H:%M:%S')} UTC] 💥 [EXPEDIENTES ERROR] {exc}", flush=True)
        return []
    finally:
        if conn is not None:
            if hasattr(conn, "release"):
                conn.release()
            else:
                conn.close()
