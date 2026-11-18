"""Asignador de tareas: plazos de la Parte + tareas manuales.

No es un CRM aparte. Cuelga del expediente, de la actuación unificada y del
vencimiento (`vencimientos.estado` es la fuente de verdad al completar).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from dbrows import first_value, names_set, rows_as_dicts
from observability import log_msg
from vencimientos_estado import ESTADO_COMPLETADO, ESTADO_PENDIENTE

PRIORIDADES = ("Baja", "Media", "Alta", "Urgente")
ESTADOS = ("Pendiente", "En progreso", "Completada", "Cancelada", "Bloqueada")
ESTADOS_ABIERTOS = ("Pendiente", "En progreso", "Bloqueada")
ORIGENES = ("manual", "plazo", "desviacion", "alerta", "seguimiento")

# Contestación / subsanar → Alta; recurso → Urgente.
_PRIORIDAD_POR_CODIGO = {
    "REPOSICION": "Urgente",
    "APELACION": "Urgente",
    "INADMISION_SUBSANAR": "Alta",
    "EXCEPCIONES_TRASLADO": "Alta",
    "DESISTIMIENTO_TACITO": "Alta",
    "MEDIDAS_CAUTELARES": "Alta",
    "LIQUIDACION_CREDITO": "Media",
    "NOTIFICACION": "Media",
    "TERMINO_RAMA": "Media",
}


class TareasError(ValueError):
    """Error de validación al crear o mover una tarea."""


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name=%s LIMIT 1
        """,
        (table,),
    )
    return cur.fetchone() is not None


def _cols(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (table,),
    )
    return names_set(cur.fetchall(), "column_name")


def _int_id(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _norm_prioridad(valor: Any, default: str = "Media") -> str:
    text = str(valor or "").strip()
    for p in PRIORIDADES:
        if text.casefold() == p.casefold():
            return p
    return default


def _norm_estado(valor: Any, default: str = "Pendiente") -> str:
    text = str(valor or "").strip()
    for e in ESTADOS:
        if text.casefold() == e.casefold():
            return e
    return default


def _norm_origen(valor: Any, default: str = "manual") -> str:
    text = str(valor or "").strip().casefold()
    return text if text in ORIGENES else default


def prioridad_automatica(codigo_plazo: Any = None, titulo: Any = None) -> str:
    """Prioridad por tipo de plazo: recurso Urgente, contestación Alta."""
    codigo = str(codigo_plazo or "").strip().upper()
    if codigo in _PRIORIDAD_POR_CODIGO:
        return _PRIORIDAD_POR_CODIGO[codigo]
    texto = f"{codigo} {titulo or ''}".casefold()
    if any(k in texto for k in ("reposicion", "reposición", "apelacion", "apelación", "recurso")):
        return "Urgente"
    if any(
        k in texto
        for k in (
            "contestacion",
            "contestación",
            "contestar",
            "inadmision",
            "inadmisión",
            "subsanar",
            "excepcion",
            "excepción",
        )
    ):
        return "Alta"
    return "Media"


def _sql_abierta(alias: str = "t") -> str:
    pref = f"{alias}." if alias else ""
    return (
        f"{pref}estado IN ('Pendiente', 'En progreso', 'Bloqueada')"
    )


def _registrar_historial(
    cur,
    *,
    tarea_id: int,
    usuario_id: Optional[int],
    accion: str,
    campo: Optional[str] = None,
    valor_anterior: Any = None,
    valor_nuevo: Any = None,
) -> None:
    cur.execute(
        """
        INSERT INTO tareas_historial (
            tarea_id, usuario_id, accion, campo, valor_anterior, valor_nuevo
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            tarea_id,
            usuario_id,
            (accion or "cambio")[:80],
            (campo or None),
            None if valor_anterior is None else str(valor_anterior)[:500],
            None if valor_nuevo is None else str(valor_nuevo)[:500],
        ),
    )


def _abogado_del_proceso(cur, radicado: str) -> Optional[int]:
    if not radicado or not _table_exists(cur, "procesos"):
        return None
    cols = _cols(cur, "procesos")
    if "abogado_id" not in cols:
        return None
    cur.execute(
        "SELECT abogado_id FROM procesos WHERE radicado_interno=%s LIMIT 1",
        (radicado,),
    )
    row = cur.fetchone()
    return _int_id(first_value(row, "abogado_id") if row else None)


def _marcar_vencimiento(
    cur,
    vencimiento_id: Optional[int],
    estado: str,
) -> None:
    vid = _int_id(vencimiento_id)
    if vid is None:
        return
    cur.execute(
        "UPDATE vencimientos SET estado=%s WHERE id=%s",
        (estado, vid),
    )


def _insertar_tarea(cur, payload: dict) -> Optional[int]:
    cols = _cols(cur, "tareas")
    use = [c for c in payload if c in cols and payload[c] is not None]
    if "titulo" not in use:
        return None
    cur.execute(
        f"INSERT INTO tareas ({', '.join(use)}) VALUES ({', '.join(['%s'] * len(use))}) RETURNING id",
        [payload[c] for c in use],
    )
    got = cur.fetchone()
    return _int_id(first_value(got, "id") if got else None)


def crear_tarea_manual(
    cur,
    *,
    titulo: str,
    asignado_a: Any,
    creado_por: Any = None,
    radicado_interno: Any = None,
    descripcion: Any = None,
    prioridad: Any = None,
    fecha_vencimiento: Any = None,
    recordatorio_dias: Any = None,
    notas_internas: Any = None,
    origen: Any = "manual",
) -> dict:
    if not _table_exists(cur, "tareas"):
        raise TareasError("La tabla de tareas aún no está disponible")
    titulo_n = " ".join(str(titulo or "").split())
    if not titulo_n:
        raise TareasError("El título es obligatorio")
    asignado = _int_id(asignado_a)
    if asignado is None:
        raise TareasError("La tarea debe asignarse a un usuario")
    rec = _int_id(recordatorio_dias)
    payload = {
        "radicado_interno": (str(radicado_interno).strip() or None) if radicado_interno else None,
        "titulo": titulo_n[:200],
        "descripcion": (str(descripcion).strip() or None) if descripcion else None,
        "asignado_a": asignado,
        "creado_por": _int_id(creado_por),
        "prioridad": _norm_prioridad(prioridad),
        "estado": "Pendiente",
        "fecha_vencimiento": _parse_date(fecha_vencimiento),
        "es_automatica": False,
        "origen": _norm_origen(origen, "manual"),
        "recordatorio_dias": rec if rec is not None and rec >= 0 else None,
        "notas_internas": (str(notas_internas).strip() or None) if notas_internas else None,
        "updated_at": datetime.now(timezone.utc),
    }
    tid = _insertar_tarea(cur, payload)
    if tid is None:
        raise TareasError("No se pudo crear la tarea")
    _registrar_historial(
        cur,
        tarea_id=tid,
        usuario_id=_int_id(creado_por),
        accion="crear",
        campo="estado",
        valor_nuevo="Pendiente",
    )
    return obtener_tarea(cur, tid) or {"id": tid}


def crear_tarea_desde_plazo(
    cur,
    *,
    radicado: str,
    actuacion_id: Any,
    vencimiento_id: Any,
    titulo: str,
    fecha: Any,
    codigo_plazo: Any = None,
    asignado_a: Any = None,
) -> Optional[dict]:
    """Upsert de la tarea abierta del plazo. Nunca propaga errores de negocio."""
    try:
        return _crear_tarea_desde_plazo(
            cur,
            radicado=radicado,
            actuacion_id=actuacion_id,
            vencimiento_id=vencimiento_id,
            titulo=titulo,
            fecha=fecha,
            codigo_plazo=codigo_plazo,
            asignado_a=asignado_a,
        )
    except Exception as exc:
        log_msg(
            "⚠️ [TAREAS]",
            f"No se pudo crear tarea de plazo actuacion={actuacion_id}: "
            f"{type(exc).__name__}: {exc}",
        )
        return None


def _crear_tarea_desde_plazo(
    cur,
    *,
    radicado: str,
    actuacion_id: Any,
    vencimiento_id: Any,
    titulo: str,
    fecha: Any,
    codigo_plazo: Any = None,
    asignado_a: Any = None,
) -> Optional[dict]:
    if not _table_exists(cur, "tareas"):
        return None
    aid = _int_id(actuacion_id)
    vid = _int_id(vencimiento_id)
    fecha_n = _parse_date(fecha)
    titulo_n = " ".join(str(titulo or "Término procesal").split())[:200]
    if not titulo_n:
        titulo_n = "Término procesal"
    asignado = _int_id(asignado_a)
    if asignado is None:
        asignado = _abogado_del_proceso(cur, str(radicado or ""))
    prioridad = prioridad_automatica(codigo_plazo, titulo_n)

    existente = _tarea_abierta_de_plazo(cur, actuacion_id=aid, vencimiento_id=vid)
    if existente:
        tid = int(existente["id"])
        cambios = []
        if fecha_n and _parse_date(existente.get("fecha_vencimiento")) != fecha_n:
            cambios.append(("fecha_vencimiento", existente.get("fecha_vencimiento"), fecha_n))
        if vid and _int_id(existente.get("vencimiento_id")) != vid:
            cambios.append(("vencimiento_id", existente.get("vencimiento_id"), vid))
        if asignado and _int_id(existente.get("asignado_a")) is None:
            cambios.append(("asignado_a", existente.get("asignado_a"), asignado))
        if cambios:
            sets = ["updated_at=CURRENT_TIMESTAMP"]
            params: list[Any] = []
            for campo, _ant, nuevo in cambios:
                sets.append(f"{campo}=%s")
                params.append(nuevo)
            params.append(tid)
            cur.execute(f"UPDATE tareas SET {', '.join(sets)} WHERE id=%s", params)
            for campo, ant, nuevo in cambios:
                _registrar_historial(
                    cur,
                    tarea_id=tid,
                    usuario_id=None,
                    accion="recalcular",
                    campo=campo,
                    valor_anterior=ant,
                    valor_nuevo=nuevo,
                )
        return obtener_tarea(cur, tid)

    payload = {
        "radicado_interno": str(radicado or "").strip() or None,
        "actuacion_id": aid,
        "vencimiento_id": vid,
        "titulo": titulo_n,
        "descripcion": f"Plazo de la Parte: {titulo_n}",
        "asignado_a": asignado,
        "prioridad": prioridad,
        "estado": "Pendiente",
        "fecha_vencimiento": fecha_n,
        "es_automatica": True,
        "origen": "plazo",
        "updated_at": datetime.now(timezone.utc),
    }
    tid = _insertar_tarea(cur, payload)
    if tid is None:
        return None
    _registrar_historial(
        cur,
        tarea_id=tid,
        usuario_id=None,
        accion="crear_plazo",
        campo="estado",
        valor_nuevo="Pendiente",
    )
    return obtener_tarea(cur, tid)


def _row_as_dict(cur, row: Any) -> Optional[dict]:
    if row is None:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    names = [d[0] for d in (getattr(cur, "description", None) or [])]
    if names:
        return dict(zip(names, row))
    return None


def _tarea_abierta_de_plazo(
    cur,
    *,
    actuacion_id: Optional[int],
    vencimiento_id: Optional[int],
) -> Optional[dict]:
    clauses = []
    params: list[Any] = []
    if actuacion_id is not None:
        clauses.append("actuacion_id=%s")
        params.append(actuacion_id)
    if vencimiento_id is not None:
        clauses.append("vencimiento_id=%s")
        params.append(vencimiento_id)
    if not clauses:
        return None
    cur.execute(
        f"""
        SELECT * FROM tareas
        WHERE {_sql_abierta('')}
          AND ({' OR '.join(clauses)})
        ORDER BY id ASC
        LIMIT 1
        """,
        params,
    )
    return _row_as_dict(cur, cur.fetchone())


def _cargar_tarea(cur, tarea_id: Any) -> Optional[dict]:
    """Fila de tareas sin historial, comentarios ni information_schema."""
    tid = _int_id(tarea_id)
    if tid is None:
        return None
    cur.execute("SELECT t.* FROM tareas t WHERE t.id=%s", (tid,))
    return _row_as_dict(cur, cur.fetchone())


def obtener_tarea(cur, tarea_id: Any, *, detalle: bool = True) -> Optional[dict]:
    tarea = _cargar_tarea(cur, tarea_id)
    if not tarea:
        return None
    if detalle:
        tid = int(tarea["id"])
        tarea["historial"] = _listar_historial(cur, tid)
        tarea["comentarios"] = _listar_comentarios(cur, tid)
    return tarea


def _listar_historial(cur, tarea_id: int) -> list[dict]:
    if not _table_exists(cur, "tareas_historial"):
        return []
    join = _table_exists(cur, "abogados")
    if join:
        cur.execute(
            """
            SELECT h.*, a.nombre AS usuario_nombre
            FROM tareas_historial h
            LEFT JOIN abogados a ON a.id = h.usuario_id
            WHERE h.tarea_id=%s
            ORDER BY h.created_at ASC, h.id ASC
            """,
            (tarea_id,),
        )
    else:
        cur.execute(
            """
            SELECT * FROM tareas_historial
            WHERE tarea_id=%s
            ORDER BY created_at ASC, id ASC
            """,
            (tarea_id,),
        )
    return rows_as_dicts(cur)


def _listar_comentarios(cur, tarea_id: int) -> list[dict]:
    if not _table_exists(cur, "tareas_comentarios"):
        return []
    join = _table_exists(cur, "abogados")
    if join:
        cur.execute(
            """
            SELECT c.*, a.nombre AS usuario_nombre
            FROM tareas_comentarios c
            LEFT JOIN abogados a ON a.id = c.usuario_id
            WHERE c.tarea_id=%s
            ORDER BY c.created_at ASC, c.id ASC
            """,
            (tarea_id,),
        )
    else:
        cur.execute(
            """
            SELECT * FROM tareas_comentarios
            WHERE tarea_id=%s
            ORDER BY created_at ASC, id ASC
            """,
            (tarea_id,),
        )
    return rows_as_dicts(cur)


def reasignar_tarea(
    cur,
    tarea_id: Any,
    *,
    asignado_a: Any,
    usuario_id: Any = None,
) -> dict:
    tarea = _cargar_tarea(cur, tarea_id)
    if not tarea:
        raise TareasError("Tarea no encontrada")
    nuevo = _int_id(asignado_a)
    if nuevo is None:
        raise TareasError("Debe indicar el usuario asignado")
    anterior = _int_id(tarea.get("asignado_a"))
    if anterior == nuevo:
        return tarea
    cur.execute(
        """
        UPDATE tareas
           SET asignado_a=%s, updated_at=CURRENT_TIMESTAMP
         WHERE id=%s
        """,
        (nuevo, int(tarea["id"])),
    )
    _registrar_historial(
        cur,
        tarea_id=int(tarea["id"]),
        usuario_id=_int_id(usuario_id),
        accion="reasignar",
        campo="asignado_a",
        valor_anterior=anterior,
        valor_nuevo=nuevo,
    )
    tarea["asignado_a"] = nuevo
    return tarea


def cambiar_estado(
    cur,
    tarea_id: Any,
    *,
    estado: Any,
    usuario_id: Any = None,
    sync_vencimiento: bool = True,
) -> dict:
    nuevo = _norm_estado(estado, default="")
    if nuevo not in ESTADOS:
        raise TareasError("Estado no válido")
    if nuevo == "Completada":
        return completar_tarea(
            cur, tarea_id, usuario_id=usuario_id, sync_vencimiento=sync_vencimiento
        )
    tarea = _cargar_tarea(cur, tarea_id)
    if not tarea:
        raise TareasError("Tarea no encontrada")
    anterior = str(tarea.get("estado") or "")
    if anterior == nuevo:
        return tarea
    tid = int(tarea["id"])
    extra = ""
    params: list[Any] = [nuevo]
    if nuevo in ESTADOS_ABIERTOS and anterior == "Completada":
        extra = ", fecha_completada=NULL"
        tarea["fecha_completada"] = None
        if sync_vencimiento and tarea.get("vencimiento_id"):
            _marcar_vencimiento(cur, tarea.get("vencimiento_id"), ESTADO_PENDIENTE)
    params.append(tid)
    cur.execute(
        f"UPDATE tareas SET estado=%s{extra}, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
        params,
    )
    _registrar_historial(
        cur,
        tarea_id=tid,
        usuario_id=_int_id(usuario_id),
        accion="cancelar" if nuevo == "Cancelada" else "estado",
        campo="estado",
        valor_anterior=anterior,
        valor_nuevo=nuevo,
    )
    tarea["estado"] = nuevo
    return tarea


def completar_tarea(
    cur,
    tarea_id: Any,
    *,
    usuario_id: Any = None,
    sync_vencimiento: bool = True,
) -> dict:
    tarea = _cargar_tarea(cur, tarea_id)
    if not tarea:
        raise TareasError("Tarea no encontrada")
    if str(tarea.get("estado") or "") == "Completada":
        return tarea
    tid = int(tarea["id"])
    ahora = datetime.now(timezone.utc)
    cur.execute(
        """
        UPDATE tareas
           SET estado=%s, fecha_completada=%s, updated_at=CURRENT_TIMESTAMP
         WHERE id=%s
        """,
        ("Completada", ahora, tid),
    )
    _registrar_historial(
        cur,
        tarea_id=tid,
        usuario_id=_int_id(usuario_id),
        accion="completar",
        campo="estado",
        valor_anterior=tarea.get("estado"),
        valor_nuevo="Completada",
    )
    if sync_vencimiento and tarea.get("vencimiento_id"):
        _marcar_vencimiento(cur, tarea.get("vencimiento_id"), ESTADO_COMPLETADO)
    tarea["estado"] = "Completada"
    tarea["fecha_completada"] = ahora
    return tarea


def completar_tareas_de_vencimiento(
    cur,
    vencimiento_id: Any,
    *,
    usuario_id: Any = None,
) -> int:
    """Al completar el vencimiento, cierra las tareas abiertas ligadas (sin reescribir estado)."""
    vid = _int_id(vencimiento_id)
    if vid is None:
        return 0
    cur.execute(
        f"""
        SELECT id FROM tareas
        WHERE vencimiento_id=%s AND {_sql_abierta('')}
        """,
        (vid,),
    )
    ids = []
    while True:
        row = cur.fetchone()
        if not row:
            break
        tid = _int_id(first_value(row, "id"))
        if tid is not None:
            ids.append(tid)
    n = 0
    for tid in ids:
        if tid is None:
            continue
        try:
            completar_tarea(cur, tid, usuario_id=usuario_id, sync_vencimiento=False)
            n += 1
        except TareasError:
            continue
    return n


def agregar_comentario(
    cur,
    tarea_id: Any,
    *,
    comentario: str,
    usuario_id: Any = None,
) -> dict:
    tarea = obtener_tarea(cur, tarea_id)
    if not tarea:
        raise TareasError("Tarea no encontrada")
    texto = " ".join(str(comentario or "").split())
    if not texto:
        raise TareasError("El comentario no puede estar vacío")
    if not _table_exists(cur, "tareas_comentarios"):
        raise TareasError("La tabla de comentarios aún no está disponible")
    cur.execute(
        """
        INSERT INTO tareas_comentarios (tarea_id, usuario_id, comentario)
        VALUES (%s, %s, %s)
        """,
        (int(tarea["id"]), _int_id(usuario_id), texto[:4000]),
    )
    _registrar_historial(
        cur,
        tarea_id=int(tarea["id"]),
        usuario_id=_int_id(usuario_id),
        accion="comentario",
        campo="comentario",
        valor_nuevo=texto[:200],
    )
    return obtener_tarea(cur, tarea["id"]) or tarea


def borrar_tarea(cur, tarea_id: Any, *, usuario_id: Any = None) -> None:
    """Las automáticas no se borran: solo cancelar."""
    tarea = obtener_tarea(cur, tarea_id)
    if not tarea:
        raise TareasError("Tarea no encontrada")
    if tarea.get("es_automatica"):
        raise TareasError("Las tareas de plazo no se borran; cancele la tarea")
    cambiar_estado(cur, tarea["id"], estado="Cancelada", usuario_id=usuario_id)


def _select_tareas_join() -> str:
    return """
        SELECT t.*,
               aa.nombre AS asignado_nombre,
               ac.nombre AS creador_nombre
        FROM tareas t
        LEFT JOIN abogados aa ON aa.id = t.asignado_a
        LEFT JOIN abogados ac ON ac.id = t.creado_por
    """


def _select_tareas_plain() -> str:
    return "SELECT t.* FROM tareas t"


def listar_mis_tareas(
    cur,
    usuario_id: Any,
    *,
    hoy: Optional[date] = None,
    incluir_cerradas: bool = False,
) -> dict:
    uid = _int_id(usuario_id)
    hoy = hoy or date.today()
    vacio = {
        "vencidas": [],
        "hoy": [],
        "esta_semana": [],
        "mas_adelante": [],
        "cerradas": [],
        "total_abiertas": 0,
    }
    if uid is None or not _table_exists(cur, "tareas"):
        return vacio
    join = _table_exists(cur, "abogados")
    base = _select_tareas_join() if join else _select_tareas_plain()
    filtro = "WHERE t.asignado_a=%s"
    if not incluir_cerradas:
        filtro += f" AND {_sql_abierta('t')}"
    cur.execute(
        f"{base} {filtro} ORDER BY t.fecha_vencimiento ASC NULLS LAST, t.id ASC",
        (uid,),
    )
    filas = rows_as_dicts(cur)
    return agrupar_por_vencimiento(filas, hoy=hoy)


def listar_tareas_proceso(cur, radicado: str) -> list[dict]:
    if not radicado or not _table_exists(cur, "tareas"):
        return []
    join = _table_exists(cur, "abogados")
    base = _select_tareas_join() if join else _select_tareas_plain()
    cur.execute(
        f"""
        {base}
        WHERE t.radicado_interno=%s
        ORDER BY
            CASE WHEN t.estado IN ('Pendiente','En progreso','Bloqueada') THEN 0 ELSE 1 END,
            CASE t.prioridad
                WHEN 'Urgente' THEN 0
                WHEN 'Alta' THEN 1
                WHEN 'Media' THEN 2
                ELSE 3
            END,
            t.fecha_vencimiento ASC NULLS LAST,
            t.id DESC
        """,
        (radicado,),
    )
    return rows_as_dicts(cur)


def listar_tareas_asignado(cur, usuario_id: Any) -> list[dict]:
    uid = _int_id(usuario_id)
    if uid is None or not _table_exists(cur, "tareas"):
        return []
    join = _table_exists(cur, "abogados")
    base = _select_tareas_join() if join else _select_tareas_plain()
    cur.execute(
        f"{base} WHERE t.asignado_a=%s ORDER BY t.fecha_vencimiento ASC NULLS LAST, t.id ASC",
        (uid,),
    )
    return rows_as_dicts(cur)


def agrupar_por_vencimiento(filas: Iterable[dict], *, hoy: Optional[date] = None) -> dict:
    hoy = hoy or date.today()
    domingo = hoy + timedelta(days=(6 - hoy.weekday()))
    buckets = {
        "vencidas": [],
        "hoy": [],
        "esta_semana": [],
        "mas_adelante": [],
        "cerradas": [],
    }
    for fila in filas:
        estado = str(fila.get("estado") or "")
        if estado not in ESTADOS_ABIERTOS:
            buckets["cerradas"].append(fila)
            continue
        fecha = _parse_date(fila.get("fecha_vencimiento"))
        if fecha is None:
            buckets["mas_adelante"].append(fila)
        elif fecha < hoy:
            buckets["vencidas"].append(fila)
        elif fecha == hoy:
            buckets["hoy"].append(fila)
        elif fecha <= domingo:
            buckets["esta_semana"].append(fila)
        else:
            buckets["mas_adelante"].append(fila)
    buckets["total_abiertas"] = (
        len(buckets["vencidas"])
        + len(buckets["hoy"])
        + len(buckets["esta_semana"])
        + len(buckets["mas_adelante"])
    )
    return buckets


def obtener_carga_trabajo(cur) -> list[dict]:
    if not _table_exists(cur, "tareas") or not _table_exists(cur, "abogados"):
        return []
    cur.execute(
        f"""
        SELECT
            a.id,
            a.nombre,
            COUNT(t.id) FILTER (WHERE {_sql_abierta('t')}) AS abiertas,
            COUNT(t.id) FILTER (
                WHERE {_sql_abierta('t')}
                  AND t.fecha_vencimiento < CURRENT_DATE
            ) AS vencidas,
            COUNT(t.id) FILTER (
                WHERE {_sql_abierta('t')} AND t.prioridad = 'Urgente'
            ) AS urgentes,
            COUNT(t.id) FILTER (
                WHERE {_sql_abierta('t')} AND t.prioridad = 'Alta'
            ) AS altas
        FROM abogados a
        LEFT JOIN tareas t ON t.asignado_a = a.id
        WHERE COALESCE(a.activo, TRUE) = TRUE
        GROUP BY a.id, a.nombre
        ORDER BY abiertas DESC, a.nombre ASC
        """
    )
    filas = rows_as_dicts(cur)
    for f in filas:
        for k in ("abiertas", "vencidas", "urgentes", "altas"):
            try:
                f[k] = int(f.get(k) or 0)
            except (TypeError, ValueError):
                f[k] = 0
    return filas


def listar_usuarios_asignables(cur) -> list[dict]:
    if not _table_exists(cur, "abogados"):
        return []
    cols = _cols(cur, "abogados")
    filtro = "WHERE COALESCE(activo, TRUE)=TRUE" if "activo" in cols else ""
    cur.execute(
        f"SELECT id, nombre FROM abogados {filtro} ORDER BY nombre ASC NULLS LAST"
    )
    out = []
    for r in rows_as_dicts(cur):
        uid = _int_id(r.get("id"))
        if uid is None:
            continue
        out.append({"id": uid, "nombre": r.get("nombre") or f"Usuario {uid}"})
    return out
