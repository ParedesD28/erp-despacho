"""Cálculo de plazos, desviación y reloj Despacho vs Abogado.

Tras mapear la actuación:
- fin esperado (días hábiles CO + festivos + receso judicial)
- desviación en días hábiles
- upsert en `desviaciones_procesales`
- vencimiento de agenda si el plazo es de la Parte

Idempotente. Best-effort: no falla el sync ni el registro manual si no hay match.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from dbrows import first_value, names_set, rows_as_dicts
from observability import log_msg
import etapas_procesales as ep
import plazos_procesales as pp
from vencimientos_estado import ESTADO_PENDIENTE

# Semáforo del tramo abierto (no es ley procesal; solo pinta).
_AMBAR_PARTE_DIAS = 5
_ROJO_PARTE_DIAS = 8
_AMBAR_JUZGADO_DIAS = 30
_ROJO_JUZGADO_DIAS = 60

_MARCA_VENCIMIENTO = "actuacion_proceso_id="


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
        if "T" in text or " " in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text[:10])
    except ValueError:
        for fmt in ("%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(text[:10], fmt).date()
            except ValueError:
                continue
    return None


def _domingo_pascua(anio: int) -> date:
    a = anio % 19
    b = anio // 100
    c = anio % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    j = c % 4
    k = c % 7
    l = (32 + 2 * e + 2 * i - h - j) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(anio, mes, dia)


def festivos_colombia(anio: int) -> set[date]:
    fijas = {(1, 1), (5, 1), (7, 20), (8, 7), (12, 8), (12, 25)}
    out = {date(anio, mes, dia) for mes, dia in fijas}
    for mes, dia in ((1, 6), (3, 19), (6, 29), (8, 15), (10, 12), (11, 1), (11, 11)):
        candidato = date(anio, mes, dia)
        while candidato.weekday() != 0:
            candidato += timedelta(days=1)
        out.add(candidato)
    pascua = _domingo_pascua(anio)
    out.add(pascua - timedelta(days=3))
    out.add(pascua - timedelta(days=2))
    for dias_despues in (43, 64, 72):
        out.add(pascua + timedelta(days=dias_despues))
    return out


def es_receso_judicial(dia: date) -> bool:
    """Receso de fin de año (20 dic–10 ene) y semana santa (lunes a domingo)."""
    if dia.month == 12 and dia.day >= 20:
        return True
    if dia.month == 1 and dia.day <= 10:
        return True
    pascua = _domingo_pascua(dia.year)
    lunes_santo = pascua - timedelta(days=6)
    return lunes_santo <= dia <= pascua


def es_dia_habil_judicial(dia: date, *, cache_festivos: dict[int, set[date]] | None = None) -> bool:
    if dia.weekday() >= 5:
        return False
    if es_receso_judicial(dia):
        return False
    if cache_festivos is None:
        festivos = festivos_colombia(dia.year)
    else:
        festivos = cache_festivos.setdefault(dia.year, festivos_colombia(dia.year))
    return dia not in festivos


def sumar_dias_habiles_co(fecha_inicial: date, dias: int) -> Optional[date]:
    """Suma días hábiles judiciales (salta fines, festivos y receso).

    El término empieza al día hábil siguiente a `fecha_inicial` (CGP art. 118).
    Fechas inválidas / días no positivos → None.
    """
    if fecha_inicial is None or not isinstance(fecha_inicial, date):
        return None
    try:
        n = int(dias)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    actual = fecha_inicial
    agregados = 0
    cache: dict[int, set[date]] = {}
    guard = 0
    while agregados < n:
        actual += timedelta(days=1)
        guard += 1
        if guard > 800:
            return None
        if es_dia_habil_judicial(actual, cache_festivos=cache):
            agregados += 1
    return actual


def sumar_dias_calendario_habil_final(fecha_inicial: date, dias: int) -> Optional[date]:
    if fecha_inicial is None or not isinstance(fecha_inicial, date):
        return None
    try:
        n = int(dias)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    actual = fecha_inicial + timedelta(days=n)
    cache: dict[int, set[date]] = {}
    guard = 0
    while not es_dia_habil_judicial(actual, cache_festivos=cache):
        actual += timedelta(days=1)
        guard += 1
        if guard > 60:
            break
    return actual


def calcular_fin_esperado(
    fecha_inicio: Any,
    dias: Any,
    unidad: Any,
) -> Optional[date]:
    inicio = _parse_date(fecha_inicio)
    if inicio is None:
        return None
    try:
        n = int(dias) if dias is not None and str(dias).strip() != "" else None
    except (TypeError, ValueError):
        return None
    if n is None or n <= 0:
        return None
    unidad_n = str(unidad or pp.UNIDAD_DIAS_HABILES).strip().lower()
    if unidad_n == pp.UNIDAD_DIAS_CALENDARIO:
        return sumar_dias_calendario_habil_final(inicio, n)
    if unidad_n == pp.UNIDAD_MESES:
        mes = inicio.month + n
        anio = inicio.year + (mes - 1) // 12
        mes = (mes - 1) % 12 + 1
        dia = min(inicio.day, 28)
        try:
            destino = date(anio, mes, dia)
        except ValueError:
            return None
        return sumar_dias_calendario_habil_final(destino, 0) or destino
    return sumar_dias_habiles_co(inicio, n)


def dias_habiles_entre(desde: date, hasta: date) -> int:
    """Días hábiles de `desde` (excluido) a `hasta` (incluido). Negativo si hasta < desde."""
    if desde is None or hasta is None:
        return 0
    if hasta == desde:
        return 0
    paso = 1 if hasta > desde else -1
    actual = desde
    cuenta = 0
    cache: dict[int, set[date]] = {}
    guard = 0
    while actual != hasta:
        actual += timedelta(days=paso)
        guard += 1
        if guard > 4000:
            break
        if es_dia_habil_judicial(actual, cache_festivos=cache):
            cuenta += paso
    return cuenta


def calcular_desviacion_dias(
    fin_esperado: Optional[date],
    fin_real: Optional[date],
    *,
    hoy: Optional[date] = None,
) -> Optional[int]:
    if fin_esperado is None:
        return None
    cierre = fin_real or (hoy or date.today())
    return dias_habiles_entre(fin_esperado, cierre)


def semaforo_dias(responsable: object, dias: Any) -> Optional[str]:
    try:
        n = int(dias)
    except (TypeError, ValueError):
        return None
    n = abs(n)
    if str(responsable or "") == pp.RESPONSABLE_PARTE:
        if n > _ROJO_PARTE_DIAS:
            return "rojo"
        if n > _AMBAR_PARTE_DIAS:
            return "amarillo"
        return "verde"
    if n > _ROJO_JUZGADO_DIAS:
        return "rojo"
    if n > _AMBAR_JUZGADO_DIAS:
        return "amarillo"
    return "verde"


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


def _id_manual_desde_hash(content_hash: Any) -> Optional[int]:
    text = str(content_hash or "")
    for prefix in ("manual:", "migracion:"):
        if text.startswith(prefix) and text[len(prefix) :].isdigit():
            return int(text[len(prefix) :])
    return None


def procesar_actuacion(cur, actuacion_id: Any, *, hoy: Optional[date] = None) -> Optional[dict]:
    """Mapea, calcula, actualiza la fila y hace upsert. Nunca propaga errores de negocio."""
    try:
        return _procesar_actuacion(cur, actuacion_id, hoy=hoy or date.today())
    except Exception as exc:
        log_msg(
            "⚠️ [DESVIACIONES]",
            f"No se pudo calcular actuación {actuacion_id}: {type(exc).__name__}: {exc}",
        )
        return None


def procesar_pendientes(cur, radicado: str, *, hoy: Optional[date] = None) -> int:
    """Procesa filas de un expediente con `calculado_at` vacío. Idempotente."""
    if not _table_exists(cur, "actuaciones_proceso"):
        return 0
    cols = _cols(cur, "actuaciones_proceso")
    if "calculado_at" not in cols:
        return 0
    cur.execute(
        """
        SELECT id FROM actuaciones_proceso
        WHERE radicado_interno=%s AND calculado_at IS NULL
        ORDER BY fecha_actuacion NULLS LAST, id
        """,
        (radicado,),
    )
    ids = [int(first_value(r, "id")) for r in cur.fetchall() if first_value(r, "id") is not None]
    n = 0
    for aid in ids:
        if procesar_actuacion(cur, aid, hoy=hoy):
            n += 1
    return n


def _procesar_actuacion(cur, actuacion_id: Any, *, hoy: date) -> Optional[dict]:
    if not _table_exists(cur, "actuaciones_proceso"):
        return None
    cols = _cols(cur, "actuaciones_proceso")
    if "codigo_plazo" not in cols:
        return None
    try:
        aid = int(actuacion_id)
    except (TypeError, ValueError):
        return None
    cur.execute("SELECT * FROM actuaciones_proceso WHERE id=%s", (aid,))
    row = cur.fetchone()
    if not row:
        return None
    data = dict(row) if not isinstance(row, dict) else row

    tipo = data.get("tipo_actuacion")
    descripcion = data.get("descripcion")
    etapa_prev = data.get("etapa_asociada")
    tiene_termino = bool(data.get("inicio_termino") or data.get("fin_termino"))
    plazo = pp.mapear_actuacion_a_plazo(
        tipo, descripcion, etapa=etapa_prev, tiene_termino=tiene_termino
    )

    fecha_act = _parse_date(data.get("fecha_actuacion"))
    inicio_termino = _parse_date(data.get("inicio_termino")) or fecha_act
    fin_rama = _parse_date(data.get("fin_termino"))

    etapa = etapa_prev
    codigo = data.get("codigo_plazo")
    responsable = data.get("responsable")
    termino_dias = data.get("termino_legal_dias")
    termino_unidad = data.get("termino_legal_unidad")
    genera_vencimiento = False

    if plazo is not None:
        codigo = plazo.codigo
        responsable = plazo.responsable
        termino_dias = plazo.termino_dias
        termino_unidad = plazo.termino_unidad
        genera_vencimiento = plazo.genera_vencimiento
        try:
            etapa_norm = ep.normalizar_etapa(plazo.etapa_asociada)
        except ep.EtapaInvalidaError:
            etapa_norm = None
        if etapa_norm:
            # Manual: no pisar la etapa que eligió el abogado.
            if not data.get("es_manual") or not etapa_prev:
                etapa = etapa_norm
        if data.get("es_manual") and etapa_prev:
            try:
                etapa = ep.normalizar_etapa(etapa_prev) or etapa
            except ep.EtapaInvalidaError:
                pass

    if not responsable:
        # Duda → Parte (la firma). Producto: más seguro pintar "nos toca".
        responsable = pp.RESPONSABLE_PARTE if tiene_termino else pp.RESPONSABLE_JUZGADO

    fin_esperado = fin_rama
    if plazo is not None and plazo.termino_dias:
        calculado = calcular_fin_esperado(inicio_termino, plazo.termino_dias, plazo.termino_unidad)
        if calculado is not None:
            fin_esperado = calculado
    desviacion = calcular_desviacion_dias(fin_esperado, fin_rama, hoy=hoy)
    atribuible = responsable if desviacion is not None else None

    updates = {
        "etapa_asociada": etapa,
        "codigo_plazo": codigo,
        "responsable": responsable,
        "termino_legal_dias": termino_dias,
        "termino_legal_unidad": termino_unidad,
        "fin_termino_esperado": fin_esperado,
        "desviacion_dias": desviacion,
        "desviacion_atribuible": atribuible,
        "calculado_at": datetime.now(timezone.utc),
    }
    set_cols = [c for c in updates if c in cols]
    if set_cols:
        asign = ", ".join(f"{c}=%s" for c in set_cols)
        cur.execute(
            f"UPDATE actuaciones_proceso SET {asign} WHERE id=%s",
            [updates[c] for c in set_cols] + [aid],
        )

    _upsert_desviacion(
        cur,
        radicado=str(data.get("radicado_interno") or ""),
        actuacion_id=aid,
        codigo=codigo,
        etapa=etapa,
        responsable=responsable,
        fecha_actuacion=fecha_act,
        fin_esperado=fin_esperado,
        fin_real=fin_rama,
        desviacion=desviacion,
        atribuible=atribuible,
        justificacion=data.get("justificacion"),
    )

    if (
        genera_vencimiento
        and responsable == pp.RESPONSABLE_PARTE
        and fin_esperado is not None
    ):
        _crear_vencimiento_si_aplica(
            cur,
            radicado=str(data.get("radicado_interno") or ""),
            actuacion_id=aid,
            titulo=(plazo.nombre if plazo else "Término procesal"),
            fecha=fin_esperado,
        )

    return {
        "id": aid,
        "codigo_plazo": codigo,
        "responsable": responsable,
        "etapa_asociada": etapa,
        "fin_termino_esperado": fin_esperado,
        "desviacion_dias": desviacion,
    }


def _upsert_desviacion(
    cur,
    *,
    radicado: str,
    actuacion_id: int,
    codigo,
    etapa,
    responsable,
    fecha_actuacion,
    fin_esperado,
    fin_real,
    desviacion,
    atribuible,
    justificacion,
) -> None:
    if not radicado or not _table_exists(cur, "desviaciones_procesales"):
        return
    estado = "cerrada" if fin_real is not None else "abierta"
    cur.execute(
        """
        INSERT INTO desviaciones_procesales (
            radicado_interno, actuacion_proceso_id, codigo_plazo, etapa_asociada,
            responsable, fecha_actuacion, fin_termino_esperado, fin_termino_real,
            desviacion_dias, desviacion_atribuible, justificacion, estado, updated_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, CURRENT_TIMESTAMP
        )
        ON CONFLICT (actuacion_proceso_id) DO UPDATE SET
            codigo_plazo = EXCLUDED.codigo_plazo,
            etapa_asociada = EXCLUDED.etapa_asociada,
            responsable = EXCLUDED.responsable,
            fecha_actuacion = EXCLUDED.fecha_actuacion,
            fin_termino_esperado = EXCLUDED.fin_termino_esperado,
            fin_termino_real = EXCLUDED.fin_termino_real,
            desviacion_dias = EXCLUDED.desviacion_dias,
            desviacion_atribuible = EXCLUDED.desviacion_atribuible,
            justificacion = COALESCE(desviaciones_procesales.justificacion, EXCLUDED.justificacion),
            estado = EXCLUDED.estado,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            radicado,
            actuacion_id,
            codigo,
            etapa,
            responsable,
            fecha_actuacion,
            fin_esperado,
            fin_real,
            desviacion,
            atribuible,
            justificacion,
            estado,
        ),
    )


def _crear_vencimiento_si_aplica(cur, *, radicado: str, actuacion_id: int, titulo: str, fecha: date) -> None:
    if not radicado or not _table_exists(cur, "vencimientos"):
        return
    cols = _cols(cur, "vencimientos")
    needed = {"radicado_interno", "titulo", "fecha_vencimiento", "estado"}
    if not needed.issubset(cols):
        return
    marca = f"{_MARCA_VENCIMIENTO}{actuacion_id}"
    if "observaciones" in cols:
        cur.execute(
            f"""
            SELECT id FROM vencimientos
            WHERE radicado_interno=%s
              AND observaciones LIKE %s
              AND COALESCE(anulado, FALSE)=FALSE
            LIMIT 1
            """,
            (radicado, f"%{marca}%"),
        )
        if cur.fetchone():
            return
    payload = {
        "radicado_interno": radicado,
        "titulo": (titulo or "Término procesal")[:200],
        "fecha_vencimiento": fecha.isoformat(),
        "estado": ESTADO_PENDIENTE,
        "observaciones": marca,
        "tipo": "PROCESAL",
        "categoria": "TERMINO",
        "anulado": False,
        "valor": 0,
    }
    use = [c for c in payload if c in cols]
    cur.execute(
        f"INSERT INTO vencimientos ({', '.join(use)}) VALUES ({', '.join(['%s'] * len(use))})",
        [payload[c] for c in use],
    )


def dual_write_manual(
    cur,
    *,
    actuacion_id: Any,
    radicado: str,
    fecha: Any,
    etapa: Any,
    descripcion: Any,
    tipificacion: Any = None,
) -> Optional[int]:
    """Espejo en actuaciones_proceso si el trigger no corrió (o para enriquecer)."""
    if not _table_exists(cur, "actuaciones_proceso"):
        return None
    cols = _cols(cur, "actuaciones_proceso")
    if "content_hash" not in cols or "radicado_interno" not in cols:
        return None
    try:
        aid = int(actuacion_id)
    except (TypeError, ValueError):
        return None
    content_hash = f"manual:{aid}"
    fecha_d = _parse_date(fecha)
    payload = {
        "radicado_interno": radicado,
        "content_hash": content_hash,
        "fecha_actuacion": fecha_d,
        "tipo_actuacion": (str(tipificacion).strip() if tipificacion else "") or None,
        "descripcion": (str(descripcion).strip() if descripcion else "") or None,
        "es_manual": True,
        "etapa_asociada": etapa,
        "origen": "manual",
    }
    use = [c for c in payload if c in cols]
    set_cols = [
        c
        for c in ("etapa_asociada", "descripcion", "tipo_actuacion", "fecha_actuacion")
        if c in use
    ]
    conflict = (
        "DO NOTHING"
        if not set_cols
        else "DO UPDATE SET " + ", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, actuaciones_proceso.{c})" for c in set_cols
        )
    )
    cur.execute(
        f"""
        INSERT INTO actuaciones_proceso ({', '.join(use)})
        VALUES ({', '.join(['%s'] * len(use))})
        ON CONFLICT (radicado_interno, content_hash) {conflict}
        RETURNING id
        """,
        [payload[c] for c in use],
    )
    got = cur.fetchone()
    if not got:
        cur.execute(
            """
            SELECT id FROM actuaciones_proceso
            WHERE radicado_interno=%s AND content_hash=%s
            LIMIT 1
            """,
            (radicado, content_hash),
        )
        got = cur.fetchone()
    if not got:
        return None
    new_id = int(first_value(got, "id"))
    procesar_actuacion(cur, new_id)
    return new_id


def etapas_unificadas(cur, radicado: str) -> list[Any]:
    """Etapas de `actuaciones` + `actuaciones_proceso.etapa_asociada`."""
    valores: list[Any] = []
    if _table_exists(cur, "actuaciones"):
        act_cols = _cols(cur, "actuaciones")
        if "etapa" in act_cols:
            cur.execute("SELECT etapa FROM actuaciones WHERE radicado_interno=%s", (radicado,))
            for r in cur.fetchall():
                valores.append(r["etapa"] if isinstance(r, dict) else r[0])
    if _table_exists(cur, "actuaciones_proceso"):
        ap_cols = _cols(cur, "actuaciones_proceso")
        if "etapa_asociada" in ap_cols:
            cur.execute(
                "SELECT etapa_asociada FROM actuaciones_proceso WHERE radicado_interno=%s",
                (radicado,),
            )
            for r in cur.fetchall():
                valores.append(r["etapa_asociada"] if isinstance(r, dict) else r[0])
    return valores


def timeline_unificada(cur, radicado: str, *, hoy: Optional[date] = None) -> dict:
    """Línea de tiempo mezclada + tramos del reloj. Dedup Rama vs manual."""
    hoy = hoy or date.today()
    eventos: list[dict] = []
    if _table_exists(cur, "actuaciones_proceso"):
        ap_cols = _cols(cur, "actuaciones_proceso")
        wanted = [
            "id", "fecha_actuacion", "tipo_actuacion", "descripcion", "etapa_asociada",
            "responsable", "codigo_plazo", "fin_termino", "fin_termino_esperado",
            "inicio_termino", "desviacion_dias", "es_manual", "origen", "content_hash",
            "drive_url", "vencido", "justificacion",
        ]
        avail = [c for c in wanted if c in ap_cols]
        if avail:
            order = "fecha_actuacion" if "fecha_actuacion" in avail else "id"
            cur.execute(
                f"SELECT {', '.join(avail)} FROM actuaciones_proceso "
                f"WHERE radicado_interno=%s ORDER BY {order} ASC NULLS LAST, id ASC",
                (radicado,),
            )
            for r in rows_as_dicts(cur):
                eventos.append(_evento_desde_ap(r, hoy=hoy))

    if not eventos and _table_exists(cur, "actuaciones"):
        # Migración no aplicada: caer al historial manual.
        act_cols = _cols(cur, "actuaciones")
        wanted = ["id", "fecha", "etapa", "descripcion", "usuario", "tipificacion_sugerida"]
        avail = [c for c in wanted if c in act_cols]
        if avail:
            cur.execute(
                f"SELECT {', '.join(avail)} FROM actuaciones WHERE radicado_interno=%s",
                (radicado,),
            )
            for r in rows_as_dicts(cur):
                eventos.append(_evento_desde_manual(r, hoy=hoy))

    eventos = _dedup_timeline(eventos)
    eventos.sort(key=lambda e: (_parse_date(e.get("fecha")) or date.min, int(e.get("id") or 0)))
    tramos = construir_tramos(eventos, hoy=hoy)
    abiertos = [t for t in tramos if t.get("abierto")]
    abierto = abiertos[-1] if abiertos else None
    totales = _totales_reloj(tramos)
    return {
        "eventos": eventos,
        "tramos": tramos,
        "abierto": abierto,
        "totales": totales,
    }


def _evento_desde_ap(r: dict, *, hoy: date) -> dict:
    responsable = r.get("responsable")
    fecha = _parse_date(r.get("fecha_actuacion"))
    fin_esp = _parse_date(r.get("fin_termino_esperado")) or _parse_date(r.get("fin_termino"))
    dias_restantes = None
    if responsable == pp.RESPONSABLE_PARTE and fin_esp is not None:
        dias_restantes = dias_habiles_entre(hoy, fin_esp) if fin_esp >= hoy else -dias_habiles_entre(fin_esp, hoy)
    dias_tramo = dias_habiles_entre(fecha, hoy) if fecha else None
    return {
        "id": r.get("id"),
        "actuacion_id": _id_manual_desde_hash(r.get("content_hash")),
        "fecha": fecha,
        "etapa": r.get("etapa_asociada"),
        "tipo": r.get("tipo_actuacion"),
        "descripcion": r.get("descripcion") or r.get("tipo_actuacion") or "",
        "responsable": responsable,
        "responsable_reloj": pp.etiqueta_reloj(responsable) if responsable else None,
        "codigo_plazo": r.get("codigo_plazo"),
        "fin_termino": _parse_date(r.get("fin_termino")),
        "fin_termino_esperado": _parse_date(r.get("fin_termino_esperado")),
        "desviacion_dias": r.get("desviacion_dias"),
        "dias_restantes": dias_restantes,
        "es_manual": bool(r.get("es_manual")),
        "origen": r.get("origen") or ("manual" if r.get("es_manual") else "sync"),
        "drive_url": r.get("drive_url"),
        "vencido": r.get("vencido"),
        "semaforo": semaforo_dias(responsable, dias_tramo),
        "content_hash": r.get("content_hash"),
    }


def _evento_desde_manual(r: dict, *, hoy: date) -> dict:
    fecha = _parse_date(r.get("fecha"))
    return {
        "id": r.get("id"),
        "actuacion_id": r.get("id"),
        "fecha": fecha,
        "etapa": r.get("etapa"),
        "tipo": r.get("tipificacion_sugerida"),
        "descripcion": r.get("descripcion") or "",
        "responsable": None,
        "responsable_reloj": None,
        "codigo_plazo": None,
        "fin_termino": None,
        "fin_termino_esperado": None,
        "desviacion_dias": None,
        "dias_restantes": None,
        "es_manual": True,
        "origen": "manual",
        "drive_url": None,
        "vencido": None,
        "semaforo": None,
        "content_hash": None,
    }


def _dedup_timeline(eventos: list[dict]) -> list[dict]:
    """Si el mismo hecho está dos veces, queda la fila Rama (término/PDF)."""
    vistos: set[tuple] = set()
    out: list[dict] = []
    # Preferir sync (Rama) sobre espejo manual del mismo día+texto.
    orden = sorted(eventos, key=lambda e: (0 if e.get("origen") == "sync" else 1, int(e.get("id") or 0)))
    for e in orden:
        fecha = _parse_date(e.get("fecha"))
        texto = " ".join(str(e.get("descripcion") or "").split()).casefold()
        clave = (fecha, texto[:180])
        if fecha and texto and clave in vistos:
            continue
        if fecha and texto:
            vistos.add(clave)
        out.append(e)
    return out


def construir_tramos(eventos: Iterable[dict], *, hoy: Optional[date] = None) -> list[dict]:
    """Entre dos actos relevantes: días y dueño = responsable del acto que cierra el tramo anterior."""
    hoy = hoy or date.today()
    relevantes = []
    for e in eventos:
        fecha = _parse_date(e.get("fecha"))
        if fecha is None:
            continue
        etapa = e.get("etapa")
        tipo = str(e.get("tipo") or "")
        # Auto de trámite repetido el mismo día no parte el reloj (salvo el primero).
        relevantes.append({**e, "fecha": fecha, "_tipo_norm": tipo.casefold()})
    relevantes.sort(key=lambda e: (e["fecha"], int(e.get("id") or 0)))
    filtrados: list[dict] = []
    vistos_auto: set[date] = set()
    for e in relevantes:
        clave_tipo = pp._clave(e.get("tipo") or e.get("descripcion") or "")
        if "auto de tramite" in clave_tipo or "auto de tramit" in clave_tipo or clave_tipo == "auto":
            if e["fecha"] in vistos_auto:
                continue
            vistos_auto.add(e["fecha"])
        filtrados.append(e)

    tramos: list[dict] = []
    for i, e in enumerate(filtrados):
        siguiente = filtrados[i + 1] if i + 1 < len(filtrados) else None
        hasta = siguiente["fecha"] if siguiente else hoy
        abierto = siguiente is None
        dias = dias_habiles_entre(e["fecha"], hasta)
        responsable = e.get("responsable") or pp.RESPONSABLE_PARTE
        tramos.append(
            {
                "desde": e["fecha"],
                "hasta": hasta,
                "dias": dias,
                "responsable": responsable,
                "duenio": pp.etiqueta_reloj(responsable),
                "abierto": abierto,
                "actuacion_id": e.get("id"),
                "semaforo": semaforo_dias(responsable, dias) if abierto else None,
            }
        )
        e["dias_tramo"] = dias
        e["semaforo"] = semaforo_dias(responsable, dias) if abierto else e.get("semaforo")
    return tramos


def _totales_reloj(tramos: list[dict]) -> dict:
    despacho = 0
    firma = 0
    for t in tramos:
        dias = int(t.get("dias") or 0)
        if t.get("responsable") == pp.RESPONSABLE_JUZGADO:
            despacho += max(dias, 0)
        else:
            firma += max(dias, 0)
    return {"despacho": despacho, "firma": firma}


def reloj_abierto(timeline: dict) -> dict:
    abierto = timeline.get("abierto") or {}
    totales = timeline.get("totales") or {}
    responsable = abierto.get("responsable")
    return {
        "duenio": abierto.get("duenio") or "—",
        "responsable": responsable,
        "dias": abierto.get("dias"),
        "semaforo": abierto.get("semaforo"),
        "desde": abierto.get("desde"),
        "totales": totales,
        "nos_toca": responsable == pp.RESPONSABLE_PARTE,
    }


def listar_desviaciones_informe(cur, *, limit: int = 2000) -> list[dict]:
    if not _table_exists(cur, "desviaciones_procesales"):
        return []
    limit = max(1, min(int(limit), 5000))
    cur.execute(
        f"""
        SELECT
            d.radicado_interno,
            p.radicado_rama,
            p.etapa_actual,
            d.codigo_plazo,
            d.etapa_asociada,
            d.responsable,
            d.fecha_actuacion,
            d.fin_termino_esperado,
            d.fin_termino_real,
            d.desviacion_dias,
            d.desviacion_atribuible,
            d.justificacion,
            d.estado
        FROM desviaciones_procesales d
        LEFT JOIN procesos p ON p.radicado_interno = d.radicado_interno
        WHERE UPPER(COALESCE(p.estado, 'ACTIVO')) <> 'INACTIVO'
        ORDER BY d.desviacion_dias DESC NULLS LAST, d.fecha_actuacion DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    )
    rows = rows_as_dicts(cur)
    for r in rows:
        r["duenio"] = pp.etiqueta_reloj(r.get("responsable"))
    return rows
