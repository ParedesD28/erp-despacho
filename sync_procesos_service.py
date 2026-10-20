"""Motor de sincronización diaria de actuaciones (Rama / API legal).

Reglas de negocio:
- Solo alertas in-ERP (dashboard / novedades). Sin SMS ni canales externos.
- Rate limit: asyncio.sleep entre cada llamada a LegalAPIClient.
- Dedupe idempotente por id_actuacion o content_hash (fecha+tipo+descripcion).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import date, datetime
from typing import Any, Optional

from psycopg2.extras import RealDictCursor

import db
from observability import _json_log, log_msg

# Pausa mínima entre llamadas HTTP a la API legal (por radicado y entre radicados).
SYNC_SLEEP_S = float(os.getenv("SYNC_RAMA_SLEEP_S") or "3")
RADICADO_DIGITS_RE = re.compile(r"^\d{23}$")


def normalizar_radicado_rama(raw: Any) -> str:
    """Extrae solo dígitos y valida longitud 23 (radicado judicial)."""
    digits = re.sub(r"\D", "", str(raw or "").strip())
    return digits


def es_radicado_rama_valido(raw: Any) -> bool:
    return bool(RADICADO_DIGITS_RE.match(normalizar_radicado_rama(raw)))


def content_hash_actuacion(
    *,
    fecha: Any,
    tipo: Any,
    descripcion: Any,
) -> str:
    """Hash estable para dedupe cuando no hay id externo."""
    fecha_s = _norm_text(fecha)
    tipo_s = _norm_text(tipo)
    desc_s = _norm_text(descripcion)
    payload = f"{fecha_s}|{tipo_s}|{desc_s}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    # ISO date o datetime
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


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        d = _parse_date(text)
        if d:
            return datetime(d.year, d.month, d.day)
    return None


def listar_expedientes_activos_con_radicado(cur) -> list[dict]:
    """Expedientes activos con radicado_rama de exactamente 23 dígitos."""
    cur.execute(
        """
        SELECT
            p.radicado_interno,
            p.radicado_rama,
            p.abogado_id
        FROM procesos p
        WHERE UPPER(COALESCE(p.estado, 'ACTIVO')) <> 'INACTIVO'
          AND p.radicado_rama IS NOT NULL
          AND length(trim(p.radicado_rama)) > 0
        ORDER BY p.radicado_interno
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    return [r for r in rows if es_radicado_rama_valido(r.get("radicado_rama"))]


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        LIMIT 1
        """,
        (table,),
    )
    return cur.fetchone() is not None


def _cols(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    return {str(r[0]).lower() for r in cur.fetchall()}


def _actuacion_ya_existe(cur, radicado_interno: str, *, id_actuacion: Optional[str], content_hash: str) -> bool:
    if id_actuacion:
        cur.execute(
            """
            SELECT 1 FROM actuaciones_proceso
            WHERE radicado_interno = %s AND id_actuacion = %s
            LIMIT 1
            """,
            (radicado_interno, id_actuacion),
        )
        if cur.fetchone():
            return True
    cur.execute(
        """
        SELECT 1 FROM actuaciones_proceso
        WHERE radicado_interno = %s AND content_hash = %s
        LIMIT 1
        """,
        (radicado_interno, content_hash),
    )
    return cur.fetchone() is not None


def _insertar_actuacion(cur, row: dict) -> Optional[int]:
    cols = _cols(cur, "actuaciones_proceso")
    has_drive = "drive_url" in cols and "drive_file_id" in cols
    if has_drive:
        cur.execute(
            """
            INSERT INTO actuaciones_proceso (
                radicado_interno, radicado_rama, id_actuacion, content_hash,
                fecha_actuacion, fecha_registro, tipo_actuacion, descripcion,
                con_documentos, inicio_termino, fin_termino, tipo_termino, vencido,
                drive_url, drive_file_id
            ) VALUES (
                %(radicado_interno)s, %(radicado_rama)s, %(id_actuacion)s, %(content_hash)s,
                %(fecha_actuacion)s, %(fecha_registro)s, %(tipo_actuacion)s, %(descripcion)s,
                %(con_documentos)s, %(inicio_termino)s, %(fin_termino)s, %(tipo_termino)s, %(vencido)s,
                %(drive_url)s, %(drive_file_id)s
            )
            ON CONFLICT (radicado_interno, content_hash) DO NOTHING
            RETURNING id
            """,
            {
                **row,
                "drive_url": row.get("drive_url"),
                "drive_file_id": row.get("drive_file_id"),
            },
        )
    else:
        cur.execute(
            """
            INSERT INTO actuaciones_proceso (
                radicado_interno, radicado_rama, id_actuacion, content_hash,
                fecha_actuacion, fecha_registro, tipo_actuacion, descripcion,
                con_documentos, inicio_termino, fin_termino, tipo_termino, vencido
            ) VALUES (
                %(radicado_interno)s, %(radicado_rama)s, %(id_actuacion)s, %(content_hash)s,
                %(fecha_actuacion)s, %(fecha_registro)s, %(tipo_actuacion)s, %(descripcion)s,
                %(con_documentos)s, %(inicio_termino)s, %(fin_termino)s, %(tipo_termino)s, %(vencido)s
            )
            ON CONFLICT (radicado_interno, content_hash) DO NOTHING
            RETURNING id
            """,
            row,
        )
    got = cur.fetchone()
    if not got:
        return None
    return int(got["id"] if isinstance(got, dict) else got[0])


def _actualizar_drive_actuacion(
    cur,
    actuacion_id: int,
    *,
    drive_url: Optional[str],
    drive_file_id: Optional[str],
) -> None:
    cols = _cols(cur, "actuaciones_proceso")
    if "drive_url" not in cols or "drive_file_id" not in cols:
        return
    cur.execute(
        """
        UPDATE actuaciones_proceso
        SET drive_url = %s, drive_file_id = %s
        WHERE id = %s
        """,
        (drive_url, drive_file_id, int(actuacion_id)),
    )


def _truthy_con_documentos(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in {"1", "true", "t", "yes", "si", "sí"}


async def _sincronizar_documentos_actuacion(
    client,
    drive,
    row: dict,
) -> dict:
    """Descarga PDFs de la actuación y sube el primero a Drive.

    Retorna `{drive_url, drive_file_id, docs_ok, docs_fail}` (urls pueden ser None).
    Respeta rate limit entre llamadas a la API legal.
    """
    result = {
        "drive_url": None,
        "drive_file_id": None,
        "docs_ok": 0,
        "docs_fail": 0,
    }
    id_act = row.get("id_actuacion")
    if not id_act:
        log_msg(
            "⚠️ [SYNC RAMA]",
            "Actuación con documentos sin id_actuacion — no se puede descargar",
            radicado=row.get("radicado_rama"),
        )
        return result

    try:
        await _sleep_rate_limit()
        paired = await client.descargar_pdfs_actuacion(id_act)
    except Exception as exc:
        log_msg(
            "💥 [SYNC RAMA]",
            f"Descarga documentos falló id_actuacion={id_act}: "
            f"{type(exc).__name__}: {exc}",
        )
        result["docs_fail"] += 1
        return result

    if not paired:
        log_msg(
            "⚠️ [SYNC RAMA]",
            f"Sin PDFs descargables para id_actuacion={id_act}",
        )
        return result

    from expediente_docs_storage import build_actuacion_filename

    radicado_rama = str(row.get("radicado_rama") or row.get("radicado_interno") or "")
    for idx, (doc_meta, content) in enumerate(paired):
        fname = build_actuacion_filename(
            fecha=row.get("fecha_actuacion"),
            tipo=row.get("tipo_actuacion"),
            id_actuacion=f"{id_act}" + (f"_{idx + 1}" if idx else ""),
            nombre_doc=getattr(doc_meta, "nombre", None),
        )
        try:
            uploaded = drive.upload_pdf(
                content,
                radicado=radicado_rama,
                filename=fname,
            )
            result["docs_ok"] += 1
            # Persistimos el primer PDF exitoso como enlace principal.
            if not result["drive_url"]:
                result["drive_url"] = uploaded.get("webViewLink")
                result["drive_file_id"] = uploaded.get("file_id")
        except Exception as exc:
            result["docs_fail"] += 1
            log_msg(
                "💥 [SYNC RAMA]",
                f"Upload Drive falló id_actuacion={id_act} file={fname}: "
                f"{type(exc).__name__}: {exc}",
            )
    return result


def _crear_alerta(
    cur,
    *,
    radicado_interno: str,
    actuacion_proceso_id: Optional[int],
    asignado_a: Any,
    titulo: str,
    detalle: str,
) -> None:
    assignee = None
    if asignado_a is not None and str(asignado_a).strip():
        try:
            assignee = int(asignado_a)
        except (TypeError, ValueError):
            assignee = None
    cur.execute(
        """
        INSERT INTO alertas_seguimiento (
            radicado_interno, actuacion_proceso_id, asignado_a, titulo, detalle
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (radicado_interno, actuacion_proceso_id, assignee, titulo, detalle),
    )


def _marcar_requiere_revision(cur, radicado_interno: str) -> None:
    cols = _cols(cur, "procesos")
    if "requiere_revision" not in cols:
        return
    cur.execute(
        "UPDATE procesos SET requiere_revision = TRUE WHERE radicado_interno = %s",
        (radicado_interno,),
    )


def mapear_actuacion_api(act: Any, *, radicado_interno: str, radicado_rama: str) -> dict:
    """Normaliza ActuacionResponse / dict hacia fila de actuaciones_proceso."""
    if hasattr(act, "model_dump"):
        data = act.model_dump()
    elif isinstance(act, dict):
        data = act
    else:
        data = dict(act)

    fecha = data.get("fecha_actuacion") or data.get("fechaActuacion")
    tipo = data.get("actuacion") or data.get("tipo_actuacion") or data.get("tipo")
    descripcion = data.get("anotacion") or data.get("descripcion") or data.get("comentario")
    id_raw = (
        data.get("id_actuacion")
        or data.get("idActuacion")
        or data.get("idRegActuacion")
        or data.get("id")
    )
    id_actuacion = str(id_raw).strip() if id_raw is not None and str(id_raw).strip() else None

    inicio = _parse_date(data.get("inicio_termino") or data.get("fechaInicial") or data.get("inicioTermino"))
    fin = _parse_date(data.get("fin_termino") or data.get("fechaFinal") or data.get("finTermino"))
    vencido = None
    if fin is not None:
        vencido = fin < date.today()

    ch = content_hash_actuacion(fecha=fecha, tipo=tipo, descripcion=descripcion)
    return {
        "radicado_interno": radicado_interno,
        "radicado_rama": radicado_rama,
        "id_actuacion": id_actuacion,
        "content_hash": ch,
        "fecha_actuacion": _parse_date(fecha),
        "fecha_registro": _parse_datetime(data.get("fecha_registro") or data.get("fechaRegistro")),
        "tipo_actuacion": (str(tipo).strip() if tipo else None) or None,
        "descripcion": (str(descripcion).strip() if descripcion else None) or None,
        "con_documentos": data.get("con_documentos") if data.get("con_documentos") is not None else data.get("conDocumentos"),
        "inicio_termino": inicio,
        "fin_termino": fin,
        "tipo_termino": None,
        "vencido": vencido,
    }


async def _sleep_rate_limit() -> None:
    await asyncio.sleep(max(0.0, SYNC_SLEEP_S))


async def sincronizar_un_expediente(
    client,
    cur,
    expediente: dict,
    *,
    drive=None,
) -> dict:
    """Sincroniza un expediente. Retorna métricas; no propaga excepciones de negocio HTTP."""
    radicado_interno = str(expediente.get("radicado_interno") or "").strip()
    radicado_rama = normalizar_radicado_rama(expediente.get("radicado_rama"))
    nuevas = 0
    alertas = 0
    docs_ok = 0
    docs_fail = 0

    id_interno = await client.consultar_id_por_radicado(radicado_rama)
    await _sleep_rate_limit()

    actuaciones = await client.obtener_actuaciones(id_interno)
    await _sleep_rate_limit()

    # Orden cronológico: más recientes al final para alertar la última novedad.
    mapped = [
        mapear_actuacion_api(a, radicado_interno=radicado_interno, radicado_rama=radicado_rama)
        for a in (actuaciones or [])
    ]
    mapped.sort(key=lambda r: (r["fecha_actuacion"] or date.min, r["content_hash"]))

    last_new_id: Optional[int] = None
    last_new_row: Optional[dict] = None
    for row in mapped:
        if _actuacion_ya_existe(
            cur,
            radicado_interno,
            id_actuacion=row.get("id_actuacion"),
            content_hash=row["content_hash"],
        ):
            continue
        new_id = _insertar_actuacion(cur, row)
        if new_id is None:
            continue
        nuevas += 1
        last_new_id = new_id
        last_new_row = row

        # PDF → Drive solo en actuaciones nuevas con documentos (si Drive habilitado).
        if drive is not None and _truthy_con_documentos(row.get("con_documentos")):
            drive_meta = await _sincronizar_documentos_actuacion(client, drive, row)
            docs_ok += int(drive_meta.get("docs_ok") or 0)
            docs_fail += int(drive_meta.get("docs_fail") or 0)
            if drive_meta.get("drive_url") or drive_meta.get("drive_file_id"):
                _actualizar_drive_actuacion(
                    cur,
                    new_id,
                    drive_url=drive_meta.get("drive_url"),
                    drive_file_id=drive_meta.get("drive_file_id"),
                )
                row["drive_url"] = drive_meta.get("drive_url")
                row["drive_file_id"] = drive_meta.get("drive_file_id")

    if nuevas > 0 and last_new_row is not None:
        tipo = last_new_row.get("tipo_actuacion") or "Actuación"
        fecha_txt = str(last_new_row.get("fecha_actuacion") or "")
        titulo = f"Nueva actuación: {tipo}"
        detalle = (
            f"Radicado {radicado_rama}. "
            f"{nuevas} movimiento(s) nuevo(s). "
            f"Última: {fecha_txt} — {(last_new_row.get('descripcion') or '')[:240]}"
        )
        if last_new_row.get("drive_url"):
            detalle = f"{detalle} PDF: {last_new_row['drive_url']}"
        _crear_alerta(
            cur,
            radicado_interno=radicado_interno,
            actuacion_proceso_id=last_new_id,
            asignado_a=expediente.get("abogado_id"),
            titulo=titulo,
            detalle=detalle,
        )
        alertas += 1
        _marcar_requiere_revision(cur, radicado_interno)

    return {
        "radicado_interno": radicado_interno,
        "radicado_rama": radicado_rama,
        "nuevas": nuevas,
        "alertas": alertas,
        "docs_ok": docs_ok,
        "docs_fail": docs_fail,
        "ok": True,
    }


async def sincronizar_expedientes_activos(db_session=None) -> dict:
    """Sincroniza todos los expedientes activos con radicado 23 dígitos.

    `db_session` se ignora (compat con firma pedida); este ERP usa
    `db.get_connection()` / psycopg2, no SQLAlchemy Session.
    """
    _ = db_session
    from external_legal_client import LegalAPIClient

    started = datetime.utcnow().isoformat() + "Z"
    log_msg("🔄 [SYNC RAMA]", "Inicio sincronización diaria de actuaciones")
    _json_log("INFO", "sync_rama_inicio", started=started)

    conn = db.get_connection()
    resumen = {
        "started": started,
        "candidatos": 0,
        "ok": 0,
        "errores": 0,
        "nuevas_actuaciones": 0,
        "alertas": 0,
        "docs_ok": 0,
        "docs_fail": 0,
        "docs_backend": "none",
        "omitidos_sin_tablas": False,
    }
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur, "actuaciones_proceso") or not _table_exists(cur, "alertas_seguimiento"):
                resumen["omitidos_sin_tablas"] = True
                log_msg(
                    "⚠️ [SYNC RAMA]",
                    "Faltan tablas actuaciones_proceso/alertas_seguimiento — aplicar migración",
                )
                _json_log("ERROR", "sync_rama_schema_missing")
                return resumen

            expedientes = listar_expedientes_activos_con_radicado(cur)
            resumen["candidatos"] = len(expedientes)

        from expediente_docs_storage import get_document_storage, storage_backend_name

        drive = get_document_storage()
        resumen["docs_backend"] = storage_backend_name() if drive is not None else "none"
        if drive is not None:
            log_msg(
                "☁️ [SYNC RAMA]",
                f"Almacenamiento PDFs activo: {resumen['docs_backend']}",
            )
        else:
            log_msg(
                "ℹ️ [SYNC RAMA]",
                "Sin almacenamiento PDF (configura OAuth Drive o EXPEDIENTE_DOCS_*)",
            )

        async with LegalAPIClient() as client:
            for idx, exp in enumerate(expedientes):
                rad = str(exp.get("radicado_interno") or "")
                try:
                    with conn:
                        with conn.cursor(cursor_factory=RealDictCursor) as cur:
                            metrics = await sincronizar_un_expediente(
                                client, cur, exp, drive=drive
                            )
                    resumen["ok"] += 1
                    resumen["nuevas_actuaciones"] += int(metrics.get("nuevas") or 0)
                    resumen["alertas"] += int(metrics.get("alertas") or 0)
                    resumen["docs_ok"] += int(metrics.get("docs_ok") or 0)
                    resumen["docs_fail"] += int(metrics.get("docs_fail") or 0)
                    log_msg(
                        "✅ [SYNC RAMA]",
                        f"Expediente {rad}",
                        nuevas=metrics.get("nuevas"),
                        alertas=metrics.get("alertas"),
                        docs_ok=metrics.get("docs_ok"),
                    )
                except Exception as exc:
                    resumen["errores"] += 1
                    log_msg(
                        "💥 [SYNC RAMA]",
                        f"Error en expediente {rad}: {type(exc).__name__}: {exc}",
                    )
                    _json_log(
                        "ERROR",
                        "sync_rama_expediente_error",
                        radicado_interno=rad,
                        error=type(exc).__name__,
                    )
                    # Continuar con el siguiente; rate limit igualmente.
                    await _sleep_rate_limit()

                # Pausa adicional entre radicados (excepto el último).
                if idx < len(expedientes) - 1:
                    await _sleep_rate_limit()

    finally:
        conn.release()

    resumen["finished"] = datetime.utcnow().isoformat() + "Z"
    log_msg(
        "🏁 [SYNC RAMA]",
        "Fin sincronización",
        candidatos=resumen["candidatos"],
        ok=resumen["ok"],
        errores=resumen["errores"],
        nuevas=resumen["nuevas_actuaciones"],
        alertas=resumen["alertas"],
        docs_ok=resumen["docs_ok"],
        docs_fail=resumen["docs_fail"],
    )
    _json_log("INFO", "sync_rama_fin", **{k: v for k, v in resumen.items() if k != "started"})
    return resumen


def contar_alertas_no_leidas(cur) -> int:
    if not _table_exists(cur, "alertas_seguimiento"):
        return 0
    cur.execute(
        """
        SELECT COUNT(*) AS total
        FROM alertas_seguimiento
        WHERE leida = FALSE AND atendida = FALSE
        """
    )
    row = cur.fetchone()
    if not row:
        return 0
    return int(row["total"] if isinstance(row, dict) else row[0])


def listar_alertas_recientes(cur, *, limit: int = 50, solo_no_leidas: bool = False) -> list[dict]:
    if not _table_exists(cur, "alertas_seguimiento"):
        return []
    limit = max(1, min(int(limit), 200))
    where = "WHERE a.leida = FALSE AND a.atendida = FALSE" if solo_no_leidas else ""
    drive_select = ""
    drive_join = ""
    if _table_exists(cur, "actuaciones_proceso"):
        ap_cols = _cols(cur, "actuaciones_proceso")
        if "drive_url" in ap_cols:
            drive_select = ", ap.drive_url, ap.drive_file_id"
            drive_join = (
                " LEFT JOIN actuaciones_proceso ap"
                " ON ap.id = a.actuacion_proceso_id"
            )
    cur.execute(
        f"""
        SELECT
            a.id,
            a.radicado_interno,
            a.titulo,
            a.detalle,
            a.leida,
            a.atendida,
            a.asignado_a,
            a.created_at,
            p.radicado_rama,
            p.requiere_revision
            {drive_select}
        FROM alertas_seguimiento a
        LEFT JOIN procesos p ON p.radicado_interno = a.radicado_interno
        {drive_join}
        {where}
        ORDER BY a.created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def marcar_alerta_leida(cur, alerta_id: int) -> bool:
    cur.execute(
        """
        UPDATE alertas_seguimiento
        SET leida = TRUE, leida_at = CURRENT_TIMESTAMP
        WHERE id = %s
        RETURNING id
        """,
        (int(alerta_id),),
    )
    return cur.fetchone() is not None
