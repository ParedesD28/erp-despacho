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
        actual = {
            str(_row_value(row, "column_name"))
            for row in cur.fetchall()
            if _row_value(row, "column_name") is not None
        }
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
    actual = {
        str(_row_value(row, "column_name"))
        for row in cur.fetchall()
        if _row_value(row, "column_name") is not None
    }
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
        SELECT id, conjunto_residencial, torre_apto, contacto_id, conjunto_id
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
        "conjunto_id": _row_value(row, "conjunto_id", _row_value(row, 4)),
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
    if _table_exists(cur, "proceso_inactivaciones"):
        cur.execute(
            """
            SELECT motivo, fecha, usuario, accion
            FROM proceso_inactivaciones
            WHERE radicado_interno=%s AND accion='INACTIVAR'
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(radicado),),
        )
        evento_inactivo = cur.fetchone()
        proc["inactivacion"] = dict(evento_inactivo) if evento_inactivo else None
    else:
        proc["inactivacion"] = None
    if _table_exists(cur, "abogados") and proc.get("abogado_id"):
        cur.execute("SELECT nombre FROM abogados WHERE id=%s", (proc["abogado_id"],))
        ab_row = cur.fetchone()
        if ab_row:
            proc["abogado_asignado"] = _row_value(ab_row, "nombre", _row_value(ab_row, 0))
    return proc


def _proceso_activo_en_inmueble(cur, inmueble_id: int, excluir_radicado: str):
    """Devuelve otro proceso activo ligado al inmueble, si existe."""
    cur.execute(
        """
        SELECT radicado_interno, estado
        FROM procesos
        WHERE inmueble_id=%s
          AND radicado_interno<>%s
          AND UPPER(COALESCE(estado, 'ACTIVO')) <> 'INACTIVO'
        ORDER BY radicado_interno
        LIMIT 1
        """,
        (int(inmueble_id), str(excluir_radicado)),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _contar_procesos_inmueble(cur, inmueble_id: int) -> int:
    cur.execute(
        "SELECT COUNT(*) AS n FROM procesos WHERE inmueble_id=%s",
        (int(inmueble_id),),
    )
    row = cur.fetchone()
    return int(_row_value(row, "n", _row_value(row, 0, 0)) or 0)


def _upsert_propietarios_inmueble(cur, inmueble_id: int, contactos_demandados: list) -> None:
    """Upsert de demandados como titulares; no duplica (UNIQUE inmueble+contacto)."""
    if not inmueble_id or not contactos_demandados:
        return
    if not _table_exists(cur, "inmueble_propietarios"):
        return

    for idx, contacto in enumerate(contactos_demandados):
        contacto_id = contacto.get("id") if isinstance(contacto, dict) else None
        if not contacto_id:
            continue
        cur.execute(
            """
            INSERT INTO inmueble_propietarios
                (inmueble_id, contacto_id, es_principal)
            VALUES (%s, %s, %s)
            ON CONFLICT (inmueble_id, contacto_id)
            DO UPDATE SET es_principal=EXCLUDED.es_principal
            """,
            (int(inmueble_id), int(contacto_id), idx == 0),
        )


def _limpiar_propietarios_inmueble_anterior(
    cur,
    *,
    inmueble_anterior_id: int,
    contactos_demandados: list,
    radicado_interno: str,
) -> None:
    """Quita del inmueble viejo a demandados de este proceso si nadie más los necesita."""
    if not inmueble_anterior_id or not contactos_demandados:
        return
    if not _table_exists(cur, "inmueble_propietarios"):
        return

    contacto_ids = [
        int(c["id"])
        for c in contactos_demandados
        if isinstance(c, dict) and c.get("id")
    ]
    if not contacto_ids:
        return

    placeholders = ",".join(["%s"] * len(contacto_ids))
    cur.execute(
        f"""
        DELETE FROM inmueble_propietarios ip
        WHERE ip.inmueble_id=%s
          AND ip.contacto_id IN ({placeholders})
          AND NOT EXISTS (
            SELECT 1
            FROM procesos p
            JOIN proceso_partes pp
              ON pp.radicado_interno=p.radicado_interno
             AND pp.contacto_id=ip.contacto_id
             AND UPPER(pp.rol)='DEMANDADO'
            WHERE p.inmueble_id=%s
              AND p.radicado_interno<>%s
              AND UPPER(COALESCE(p.estado, 'ACTIVO')) <> 'INACTIVO'
          )
        """,
        [int(inmueble_anterior_id), *contacto_ids, int(inmueble_anterior_id), str(radicado_interno)],
    )


def _reenlanzar_historial_inmueble(
    cur,
    *,
    radicado_interno: str,
    inmueble_anterior_id: int | None,
    inmueble_nuevo_id: int,
    obligacion_ids: list[int],
) -> None:
    """Re-enlaza acuerdos/vencimientos/gestiones; no borra historial."""
    if inmueble_anterior_id and int(inmueble_anterior_id) == int(inmueble_nuevo_id):
        return

    if obligacion_ids and _table_exists(cur, "acuerdos_pago"):
        placeholders = ",".join(["%s"] * len(obligacion_ids))
        params = [int(inmueble_nuevo_id), *obligacion_ids]
        if inmueble_anterior_id:
            cur.execute(
                f"""
                UPDATE acuerdos_pago
                SET inmueble_id=%s
                WHERE obligacion_id IN ({placeholders})
                  AND (inmueble_id IS NULL OR inmueble_id=%s)
                """,
                params + [int(inmueble_anterior_id)],
            )
        else:
            cur.execute(
                f"""
                UPDATE acuerdos_pago
                SET inmueble_id=%s
                WHERE obligacion_id IN ({placeholders})
                """,
                params,
            )

    for table in ("vencimientos", "gestiones_crm"):
        if not _table_exists(cur, table):
            continue
        if inmueble_anterior_id:
            cur.execute(
                f"""
                UPDATE {table}
                SET inmueble_id=%s
                WHERE radicado_interno=%s
                  AND (inmueble_id IS NULL OR inmueble_id=%s)
                """,
                (int(inmueble_nuevo_id), str(radicado_interno), int(inmueble_anterior_id)),
            )
        else:
            cur.execute(
                f"""
                UPDATE {table}
                SET inmueble_id=%s
                WHERE radicado_interno=%s
                """,
                (int(inmueble_nuevo_id), str(radicado_interno)),
            )


def corregir_inmueble_proceso(
    cur,
    *,
    radicado_interno: str,
    proceso: dict,
    torre_apto: str,
    conjunto_id_raw: str,
    contactos_demandados: list,
    obligaciones: list | None = None,
) -> int | None:
    """Corrige el inmueble de un proceso radicado sin clonar ni dejar basura.

    - Reutiliza inmueble existente (mismo conjunto + torre/apto).
    - Si el destino ya tiene otro proceso activo: rechaza (no rompe vínculos).
    - Si el inmueble actual solo lo usa este proceso y cambia el apto en el
      mismo conjunto: actualiza en sitio (evita huérfanos).
    - Actualiza procesos.inmueble_id y obligaciones vinculadas.
    - Reconcilia inmueble_propietarios con demandados actuales.
    - Re-enlaza acuerdos/vencimientos/gestiones; no borra historial.
    """
    if not _table_exists(cur, "inmuebles_ph"):
        return proceso.get("inmueble_id")

    radicado_interno = str(radicado_interno or "").strip()
    torre_apto = str(torre_apto or "").strip()
    obligaciones = obligaciones or []
    inmueble_anterior_id = proceso.get("inmueble_id")
    if inmueble_anterior_id is not None:
        inmueble_anterior_id = int(inmueble_anterior_id)

    info_actual = (proceso.get("inmueble") or {}) if isinstance(proceso, dict) else {}
    if not info_actual and inmueble_anterior_id:
        info_actual, _ = _get_inmueble_info(cur, inmueble_anterior_id)

    # Sin inmueble previo y sin dato nuevo: no aplicar (p.ej. verbal sin PH).
    if not inmueble_anterior_id and not torre_apto:
        return None

    if not torre_apto:
        raise ValueError("Debes indicar la torre/apartamento del inmueble")

    conjunto_id = None
    conjunto_nombre = str(info_actual.get("conjunto_residencial") or "").strip()
    raw = str(conjunto_id_raw or "").strip()
    if raw.isdigit():
        conjunto_id = int(raw)
    elif info_actual.get("conjunto_id"):
        conjunto_id = int(info_actual["conjunto_id"])

    if not conjunto_id:
        raise ValueError("Debes indicar el conjunto residencial del inmueble")

    # Resolver nombre del conjunto si cambió o faltaba.
    if _table_exists(cur, "conjuntos_residenciales"):
        cur.execute(
            """
            SELECT id, nombre
            FROM conjuntos_residenciales
            WHERE id=%s
            LIMIT 1
            """,
            (conjunto_id,),
        )
        conj = cur.fetchone()
        if not conj:
            raise ValueError("El conjunto residencial seleccionado no es válido")
        conjunto_nombre = str(_row_value(conj, "nombre", _row_value(conj, 1)) or "").strip()

    mismo_apto = (
        inmueble_anterior_id
        and str(info_actual.get("torre_apto") or "").strip() == torre_apto
        and int(info_actual.get("conjunto_id") or 0) == conjunto_id
    )
    if mismo_apto:
        _upsert_propietarios_inmueble(cur, inmueble_anterior_id, contactos_demandados)
        return inmueble_anterior_id

    # ¿Ya existe unidad con ese conjunto + apto?
    cur.execute(
        """
        SELECT id
        FROM inmuebles_ph
        WHERE conjunto_id=%s
          AND torre_apto=%s
        LIMIT 1
        """,
        (conjunto_id, torre_apto),
    )
    existente = cur.fetchone()
    target_id = None
    if existente:
        target_id = int(_row_value(existente, "id", _row_value(existente, 0)))
        if inmueble_anterior_id and target_id == inmueble_anterior_id:
            _upsert_propietarios_inmueble(cur, target_id, contactos_demandados)
            return target_id
        otro = _proceso_activo_en_inmueble(cur, target_id, radicado_interno)
        if otro:
            raise ValueError(
                "El inmueble "
                f"{conjunto_nombre} / {torre_apto} ya tiene el proceso activo "
                f"{otro.get('radicado_interno')}. No se puede reasignar."
            )
    elif (
        inmueble_anterior_id
        and int(info_actual.get("conjunto_id") or 0) == conjunto_id
        and _contar_procesos_inmueble(cur, inmueble_anterior_id) <= 1
    ):
        # Corrección tipográfica en la misma unidad exclusiva: actualizar en sitio.
        cur.execute(
            """
            UPDATE inmuebles_ph
            SET torre_apto=%s,
                conjunto_residencial=%s,
                conjunto_id=%s
            WHERE id=%s
            """,
            (torre_apto, conjunto_nombre, conjunto_id, inmueble_anterior_id),
        )
        target_id = inmueble_anterior_id
    else:
        demandado_principal = contactos_demandados[0] if contactos_demandados else {}
        contacto_id = demandado_principal.get("id") if isinstance(demandado_principal, dict) else None
        if not contacto_id:
            raise ValueError("Se requiere al menos un demandado para crear el inmueble")
        cur.execute(
            """
            INSERT INTO inmuebles_ph
                (contacto_id, conjunto_residencial, conjunto_id, torre_apto)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (int(contacto_id), conjunto_nombre, conjunto_id, torre_apto),
        )
        nuevo = cur.fetchone()
        target_id = int(_row_value(nuevo, "id", _row_value(nuevo, 0)))

    if not target_id:
        raise ValueError("No fue posible resolver el inmueble")

    if inmueble_anterior_id and target_id != inmueble_anterior_id:
        cur.execute(
            "UPDATE procesos SET inmueble_id=%s WHERE radicado_interno=%s",
            (int(target_id), radicado_interno),
        )
        obligacion_ids = [int(o["id"]) for o in obligaciones if o.get("id")]
        if obligacion_ids:
            placeholders = ",".join(["%s"] * len(obligacion_ids))
            cur.execute(
                f"""
                UPDATE obligaciones
                SET inmueble_id=%s
                WHERE id IN ({placeholders})
                """,
                [int(target_id), *obligacion_ids],
            )
        _reenlanzar_historial_inmueble(
            cur,
            radicado_interno=radicado_interno,
            inmueble_anterior_id=inmueble_anterior_id,
            inmueble_nuevo_id=target_id,
            obligacion_ids=obligacion_ids,
        )
        _limpiar_propietarios_inmueble_anterior(
            cur,
            inmueble_anterior_id=inmueble_anterior_id,
            contactos_demandados=contactos_demandados,
            radicado_interno=radicado_interno,
        )
    elif not inmueble_anterior_id:
        cur.execute(
            "UPDATE procesos SET inmueble_id=%s WHERE radicado_interno=%s",
            (int(target_id), radicado_interno),
        )
        obligacion_ids = [int(o["id"]) for o in obligaciones if o.get("id")]
        if obligacion_ids:
            placeholders = ",".join(["%s"] * len(obligacion_ids))
            cur.execute(
                f"""
                UPDATE obligaciones
                SET inmueble_id=%s
                WHERE id IN ({placeholders})
                """,
                [int(target_id), *obligacion_ids],
            )
        _reenlanzar_historial_inmueble(
            cur,
            radicado_interno=radicado_interno,
            inmueble_anterior_id=None,
            inmueble_nuevo_id=target_id,
            obligacion_ids=obligacion_ids,
        )

    _upsert_propietarios_inmueble(cur, target_id, contactos_demandados)
    return target_id


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


def cargar_procesos_general_sin_duplicados(estado_filtro: str = "ACTIVOS"):
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
                        COALESCE(ppdemandante.identificaciones, '') AS demandante_identificacion,
                        COALESCE(ppdemandado.nombres, 'SIN REGISTRO') AS demandado_nombre,
                        COALESCE(ppdemandado.identificaciones, '') AS demandado_identificacion,
                        a.nombre AS abogado_asignado
                    FROM procesos p
                    LEFT JOIN abogados a ON p.abogado_id = a.id
                    LEFT JOIN LATERAL (
                        SELECT
                            STRING_AGG(DISTINCT c.nombre, ' | ' ORDER BY c.nombre) AS nombres,
                            STRING_AGG(DISTINCT c.identificacion, ' | ' ORDER BY c.identificacion) AS identificaciones
                        FROM proceso_partes pp
                        JOIN contactos c ON c.id=pp.contacto_id
                        WHERE pp.radicado_interno=p.radicado_interno
                          AND UPPER(pp.rol)='DEMANDANTE'
                    ) ppdemandante ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT
                            STRING_AGG(DISTINCT c.nombre, ' | ' ORDER BY c.nombre) AS nombres,
                            STRING_AGG(DISTINCT c.identificacion, ' | ' ORDER BY c.identificacion) AS identificaciones
                        FROM proceso_partes pp
                        JOIN contactos c ON c.id=pp.contacto_id
                        WHERE pp.radicado_interno=p.radicado_interno
                          AND UPPER(pp.rol)='DEMANDADO'
                    ) ppdemandado ON TRUE
                    WHERE (
                        %s='TODOS'
                        OR (%s='ACTIVOS' AND UPPER(COALESCE(p.estado,'ACTIVO'))<>'INACTIVO')
                        OR (%s='INACTIVOS' AND UPPER(COALESCE(p.estado,'ACTIVO'))='INACTIVO')
                    )
                    ORDER BY p.radicado_interno DESC
                    """,
                    (str(estado_filtro or "ACTIVOS").upper(), str(estado_filtro or "ACTIVOS").upper(), str(estado_filtro or "ACTIVOS").upper()),
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
