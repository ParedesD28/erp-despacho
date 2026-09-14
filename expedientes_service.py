"""Servicio y lógica de negocio unificada para Expedientes Judiciales."""
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
import json
import unicodedata
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


def _cols(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
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
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s) AS exists_table",
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
    if "etapa_actual" not in cols or not _table_exists(cur, "actuaciones"):
        return None
    act_cols = _cols(cur, "actuaciones")
    available = [c for c in ("etapa", "descripcion", "tipificacion_sugerida") if c in act_cols]
    if not available:
        return None
    select = ", ".join(available)
    order_col = "fecha" if "fecha" in act_cols else "id"
    id_clause = ", id DESC" if "id" in act_cols else ""
    cur.execute(
        f"SELECT {select} FROM actuaciones WHERE radicado_interno=%s "
        f"ORDER BY {order_col} DESC NULLS LAST{id_clause} LIMIT 1",
        (radicado,),
    )
    act = cur.fetchone()
    if not act:
        return None
    values = {name: _row_value(act, name, _row_value(act, idx)) for idx, name in enumerate(available)}
    stage = _stage_from_act(
        values.get("etapa"),
        values.get("descripcion"),
        values.get("tipificacion_sugerida"),
    )
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
    if not _table_exists(cur, "expediente_ediciones"):
        cur.execute("""
            CREATE TABLE expediente_ediciones (
                id BIGSERIAL PRIMARY KEY,
                radicado_interno TEXT NOT NULL,
                fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                usuario TEXT NOT NULL,
                accion TEXT NOT NULL,
                antes JSONB,
                despues JSONB
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_expediente_ediciones_rad ON expediente_ediciones(radicado_interno, fecha DESC)")


def _get_process(cur, radicado):
    cols = _cols(cur, "procesos")
    wanted = [
        "radicado_interno", "radicado_rama", "naturaleza", "juzgado",
        "etapa_actual", "estado", "pretensiones", "medidas_cautelares",
        "id_cliente", "demandante", "id_demandado", "demandado",
        "abogado_id", "inmueble_id", "fecha_radicacion",
    ]
    avail = [c for c in wanted if c in cols]
    if not avail:
        return None
    cur.execute(
        f"SELECT {', '.join(avail)} FROM procesos WHERE radicado_interno=%s LIMIT 1",
        (radicado,),
    )
    row = cur.fetchone()
    if not row:
        return None
    proc = {c: _row_value(row, c, _row_value(row, idx)) for idx, c in enumerate(avail)}
    if _table_exists(cur, "abogados") and proc.get("abogado_id"):
        cur.execute("SELECT nombre FROM abogados WHERE id=%s", (proc["abogado_id"],))
        ab_row = cur.fetchone()
        if ab_row:
            proc["abogado_asignado"] = _row_value(ab_row, "nombre", _row_value(ab_row, 0))
    return proc


def _get_demandantes(cur, proceso):
    out = []
    seen = set()
    if not proceso:
        return out
    raw = proceso.get("id_cliente")
    ids = [str(x).strip() for x in str(raw or "").split("|") if str(x).strip()]
    if ids and _table_exists(cur, "contactos"):
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT identificacion, nombre, tipo, telefono, email, direccion, ciudad "
            f"FROM contactos WHERE identificacion IN ({placeholders}) ORDER BY nombre ASC",
            ids,
        )
        for r in cur.fetchall():
            d = {k: _row_value(r, k) for k in ("identificacion", "nombre", "tipo", "telefono", "email", "direccion", "ciudad")}
            ident = str(d.get("identificacion") or "").strip()
            if ident and ident not in seen:
                seen.add(ident)
                out.append(d)
    if not out and ids:
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append({"identificacion": i, "nombre": proceso.get("demandante") or i})
    return out


def _get_demandados(cur, radicado):
    out = []
    seen = set()
    if _table_exists(cur, "procesos_litisconsorcio") and _table_exists(cur, "contactos"):
        cur.execute("""
            SELECT c.identificacion, c.nombre, c.tipo, c.telefono, c.email, c.direccion, c.ciudad
            FROM procesos_litisconsorcio pl
            JOIN contactos c ON c.identificacion = pl.identificacion_demandado
            WHERE pl.radicado_interno = %s
            ORDER BY c.nombre ASC
        """, (radicado,))
        for r in cur.fetchall():
            d = {k: _row_value(r, k) for k in ("identificacion", "nombre", "tipo", "telefono", "email", "direccion", "ciudad")}
            ident = str(d.get("identificacion") or "").strip()
            if ident and ident not in seen:
                seen.add(ident)
                out.append(d)
    if not out:
        cur.execute("SELECT id_demandado, demandado FROM procesos WHERE radicado_interno=%s LIMIT 1", (radicado,))
        row = cur.fetchone()
        if row:
            raw_ids = _row_value(row, "id_demandado", _row_value(row, 0))
            raw_nom = _row_value(row, "demandado", _row_value(row, 1))
            ids = [str(x).strip() for x in str(raw_ids or "").split("|") if str(x).strip()]
            noms = [str(x).strip() for x in str(raw_nom or "").split("|") if str(x).strip()]
            for idx, ident in enumerate(ids):
                if ident not in seen:
                    seen.add(ident)
                    out.append({
                        "identificacion": ident,
                        "nombre": noms[idx] if idx < len(noms) else ident,
                    })
    return out


def _get_actuaciones(cur, radicado):
    if not _table_exists(cur, "actuaciones"):
        return []
    cols = _cols(cur, "actuaciones")
    wanted = ["id", "radicado_interno", "fecha", "etapa", "descripcion", "usuario", "tipificacion_sugerida", "archivo"]
    avail = [c for c in wanted if c in cols]
    order_col = "fecha" if "fecha" in cols else "id"
    id_clause = ", id DESC" if "id" in cols else ""
    cur.execute(
        f"SELECT {', '.join(avail)} FROM actuaciones WHERE radicado_interno=%s ORDER BY {order_col} DESC NULLS LAST{id_clause}",
        (radicado,),
    )
    rows = cur.fetchall()
    return [{c: _row_value(r, c, _row_value(r, idx)) for idx, c in enumerate(avail)} for r in rows]


def _get_crm_agreements(cur, inmueble_id, identificaciones):
    if not _table_exists(cur, "gestiones_crm"):
        return []
    queries = []
    params = []
    if inmueble_id:
        queries.append("inmueble_id = %s")
        params.append(inmueble_id)
    clean_ids = [str(x).strip() for x in (identificaciones or []) if str(x).strip()]
    if clean_ids:
        placeholders = ",".join(["%s"] * len(clean_ids))
        queries.append(f"identificacion_deudor IN ({placeholders})")
        params.extend(clean_ids)
    if not queries:
        return []
    cur.execute(
        f"SELECT id, fecha, resumen, promesa_pago_fecha, tipo_contacto, usuario, COALESCE(estado,'ACTIVO') as estado "
        f"FROM gestiones_crm WHERE ({' OR '.join(queries)}) AND COALESCE(anulado,FALSE)=FALSE "
        f"ORDER BY fecha DESC LIMIT 50",
        params,
    )
    rows = cur.fetchall()
    return [
        {
            "id": _row_value(r, "id"),
            "fecha": _row_value(r, "fecha"),
            "resumen": _row_value(r, "resumen"),
            "promesa_pago_fecha": _row_value(r, "promesa_pago_fecha"),
            "tipo_contacto": _row_value(r, "tipo_contacto"),
            "usuario": _row_value(r, "usuario"),
            "estado": _row_value(r, "estado"),
        }
        for r in rows
    ]


def _get_abogados(cur):
    if not _table_exists(cur, "abogados"):
        return []
    cur.execute("SELECT id, nombre FROM abogados ORDER BY nombre ASC")
    return [{"id": _row_value(r, "id", _row_value(r, 0)), "nombre": _row_value(r, "nombre", _row_value(r, 1))} for r in cur.fetchall()]


def _contact_options(cur):
    if not _table_exists(cur, "contactos"):
        return []
    cols = _cols(cur, "contactos")
    wanted = ["identificacion", "nombre", "tipo", "telefono", "email", "direccion", "ciudad"]
    avail = [c for c in wanted if c in cols]
    cur.execute(f"SELECT {', '.join(avail)} FROM contactos ORDER BY nombre ASC LIMIT 1000")
    return [{c: _row_value(r, c, _row_value(r, idx)) for idx, c in enumerate(avail)} for r in cur.fetchall()]


def _audit(cur, radicado):
    if not _table_exists(cur, "expediente_ediciones"):
        return []
    cur.execute(
        "SELECT id, fecha, usuario, accion FROM expediente_ediciones WHERE radicado_interno=%s ORDER BY fecha DESC LIMIT 20",
        (radicado,),
    )
    return [
        {
            "id": _row_value(r, "id"),
            "fecha": _row_value(r, "fecha"),
            "usuario": _row_value(r, "usuario"),
            "accion": _row_value(r, "accion"),
        }
        for r in cur.fetchall()
    ]


def cargar_procesos_general_sin_duplicados():
    conn = db.get_connection()
    try:
        query = """
            SELECT
                p.radicado_interno,
                p.radicado_rama,
                p.naturaleza,
                p.juzgado,
                p.etapa_actual,
                p.estado,
                p.pretensiones,
                p.medidas_cautelares,
                p.id_cliente,
                c_dem.nombre AS demandante_db,
                a.nombre AS abogado_asignado,
                STRING_AGG(
                    DISTINCT NULLIF(TRIM(c_ddo.nombre), ''),
                    ' | '
                ) AS demandado,
                STRING_AGG(
                    DISTINCT NULLIF(TRIM(pl.identificacion_demandado), ''),
                    ' | '
                ) AS id_demandado
            FROM procesos p
            LEFT JOIN contactos c_dem
                ON p.id_cliente = c_dem.identificacion
            LEFT JOIN abogados a
                ON p.abogado_id = a.id
            LEFT JOIN procesos_litisconsorcio pl
                ON p.radicado_interno = pl.radicado_interno
            LEFT JOIN contactos c_ddo
                ON pl.identificacion_demandado = c_ddo.identificacion
            GROUP BY
                p.radicado_interno,
                p.radicado_rama,
                p.naturaleza,
                p.juzgado,
                p.etapa_actual,
                p.estado,
                p.pretensiones,
                p.medidas_cautelares,
                p.id_cliente,
                c_dem.nombre,
                a.nombre
            ORDER BY p.radicado_interno DESC
        """
        df = pd.read_sql_query(query, conn)
        return df.fillna("").to_dict(orient="records")
    except Exception as exc:
        print(f"[EXPEDIENTES] Error cargando procesos: {exc}", flush=True)
        return []
    finally:
        conn.release()
