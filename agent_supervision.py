"""Puente seguro ERP -> agente de cobranza (Supervisión WhatsApp).

Incluye agenda local teléfono↔nombre, normalización de mensajes/media,
y avisos in-app / webhook opcional cuando la IA atiende.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from psycopg2.extras import RealDictCursor

import db

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_TIMEOUT = 20
_DEFAULT_AGENT_URL = "https://bot-cobranzas-ph.onrender.com"
_AGENDA_LOCK = threading.Lock()
_AGENDA_READY = False

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# Placeholders que deja Agentecobranza cuando llega media sin URL.
_IMG_PLACEHOLDER_RE = re.compile(
    r"^\s*\[?\s*(Imagen recibida|El usuario envi[oó] una imagen)",
    re.IGNORECASE,
)


def _agent_url():
    return (os.getenv("AGENT_SUPERVISION_URL") or _DEFAULT_AGENT_URL).rstrip("/")


def _agent_headers():
    key = os.getenv("AGENT_SUPERVISION_KEY") or os.getenv("LIQUIDADOR_API_KEY") or ""
    return {
        "X-Agent-Supervision-Key": key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _proxy(method, path, *, json=None, params=None):
    base = _agent_url()
    key = os.getenv("AGENT_SUPERVISION_KEY") or os.getenv("LIQUIDADOR_API_KEY") or ""
    if not key:
        print(
            "[SUPERVISION AGENTE][ALERTA] No existe AGENT_SUPERVISION_KEY ni LIQUIDADOR_API_KEY",
            flush=True,
        )
        return {
            "status": "error",
            "codigo": "AGENT_SUPERVISION_KEY_MISSING",
            "mensaje": "Falta configurar AGENT_SUPERVISION_KEY (o el fallback LIQUIDADOR_API_KEY) en Render.",
        }, 503
    try:
        response = requests.request(
            method,
            f"{base}{path}",
            headers=_agent_headers(),
            json=json,
            params=params,
            timeout=_TIMEOUT,
        )
        try:
            data = response.json()
        except ValueError:
            data = {
                "status": "error",
                "mensaje": f"Respuesta no JSON del agente: HTTP {response.status_code}",
            }
        if response.status_code >= 500:
            print(
                f"[SUPERVISION AGENTE][ALERTA] Agente respondio HTTP {response.status_code} en {path}",
                flush=True,
            )
        return data, response.status_code
    except requests.RequestException as exc:
        print(f"[SUPERVISION AGENTE][ALERTA] Error de conexion al agente: {exc!r}", flush=True)
        return {
            "status": "error",
            "codigo": "AGENT_UNREACHABLE",
            "mensaje": f"Agente no disponible: {exc}",
        }, 503


def _usuario(request: Request):
    token = request.cookies.get("token_erp")
    return str(token or "Supervisor ERP")


def normalizar_telefono_digits(value: Any) -> str:
    """Solo dígitos; clave estable para agenda (alineada al bot)."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def telefono_claves_match(digits: str) -> list[str]:
    """Variantes de matching (con/sin 57) para cruzar agenda y contactos."""
    d = normalizar_telefono_digits(digits)
    if not d:
        return []
    claves = {d}
    if d.startswith("57") and len(d) >= 12:
        claves.add(d[2:])
    elif len(d) == 10:
        claves.add("57" + d)
    # últimos 10 para cruzar formatos raros
    if len(d) > 10:
        claves.add(d[-10:])
    return list(claves)


def es_telefono_whatsapp_legible(value: Any) -> bool:
    """True si parece E.164 / celular CO; False para PSID/wa_id opacos de Meta."""
    d = normalizar_telefono_digits(value)
    if not d:
        return False
    # IDs de página / PSID de Meta suelen tener ≥15 dígitos.
    if len(d) >= 15:
        return False
    if d.startswith("57") and len(d) == 12 and d[2] == "3":
        return True
    if len(d) == 10 and d.startswith("3"):
        return True
    return 10 <= len(d) <= 14


def formatear_telefono_display(value: Any) -> str:
    """Formato legible para UI; si no es teléfono, cadena vacía."""
    d = normalizar_telefono_digits(value)
    if not es_telefono_whatsapp_legible(d):
        return ""
    if d.startswith("57") and len(d) == 12:
        local = d[2:]
        return f"+57 {local[:3]} {local[3:6]} {local[6:]}"
    if len(d) == 10:
        return f"{d[:3]} {d[3:6]} {d[6:]}"
    return d


def preview_ultimo_mensaje(fila: dict) -> str:
    """Preview del listado: campos del bot o fallback por total_mensajes."""
    raw = (
        fila.get("last_message")
        or fila.get("ultimo_mensaje")
        or fila.get("ultimo_contenido")
        or fila.get("preview_mensaje")
        or fila.get("snippet")
        or fila.get("last_contenido")
    )
    if raw is not None and str(raw).strip():
        s = " ".join(str(raw).split())
        if _IMG_PLACEHOLDER_RE.search(s) or s.lower().startswith("[imagen"):
            return "📷 Imagen / comprobante"
        if s.startswith("[Documento PDF"):
            return "📄 Documento PDF"
        if len(s) > 80:
            return s[:77] + "…"
        return s
    total = fila.get("total_mensajes") or fila.get("message_count") or fila.get("mensajes_total")
    try:
        n = int(total)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return f"{n} mensaje(s) en el hilo"
    return ""


def ensure_whatsapp_agenda(conn=None) -> None:
    """Crea la tabla de agenda si aún no existe (idempotente)."""
    global _AGENDA_READY
    if _AGENDA_READY:
        return
    with _AGENDA_LOCK:
        if _AGENDA_READY:
            return
        owns = conn is None
        if owns:
            conn = db.get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS whatsapp_agenda (
                            id BIGSERIAL PRIMARY KEY,
                            telefono_digits TEXT NOT NULL,
                            telefono_display TEXT,
                            nombre TEXT NOT NULL,
                            notas TEXT,
                            contacto_id BIGINT,
                            updated_by TEXT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            CONSTRAINT uq_whatsapp_agenda_telefono_digits UNIQUE (telefono_digits)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_whatsapp_agenda_nombre
                            ON whatsapp_agenda (nombre)
                        """
                    )
            _AGENDA_READY = True
        finally:
            if owns and conn is not None:
                conn.release()


def _parse_metadata(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _es_url_imagen(url: str) -> bool:
    low = (url or "").lower().split("?", 1)[0]
    return any(low.endswith(ext) for ext in _IMAGE_EXTS)


def _extraer_urls(texto: str) -> list[str]:
    return [m.group(0).rstrip(").,;]") for m in _URL_RE.finditer(texto or "")]


def enriquecer_mensaje(mensaje: dict) -> dict:
    """Normaliza campos de mensaje y expone media/adjuntos para la UI."""
    if not isinstance(mensaje, dict):
        return mensaje
    m = dict(mensaje)
    meta = _parse_metadata(m.get("metadata") or m.get("meta") or {})
    m["metadata"] = meta

    tipo = str(m.get("tipo_mensaje") or m.get("type") or m.get("tipo") or "text").lower()
    contenido = str(m.get("contenido") or m.get("content") or m.get("mensaje") or "")
    # El bot a veces deja tipo=text con placeholder, o pierde tipo_mensaje.
    if tipo in {"", "text", "texto"} and _IMG_PLACEHOLDER_RE.search(contenido):
        tipo = "image"
    media_id = meta.get("media_id") or meta.get("id_media") or meta.get("image_id") or m.get("media_id")
    if tipo in {"", "text", "texto"} and media_id and not meta.get("url_pdf"):
        tipo = "image"

    m["contenido"] = contenido
    m["content"] = contenido
    m["tipo_mensaje"] = tipo
    m["fecha"] = m.get("fecha") or m.get("created_at") or m.get("timestamp")
    m["created_at"] = m.get("created_at") or m["fecha"]
    m["direccion"] = m.get("direccion") or m.get("direction")
    m["autor"] = m.get("autor") or m.get("author") or m.get("role")

    media_url = (
        meta.get("media_url")
        or meta.get("url_media")
        or meta.get("url_imagen")
        or meta.get("image_url")
        or meta.get("url")
        or m.get("media_url")
        or m.get("image_url")
    )
    url_pdf = meta.get("url_pdf") or meta.get("pdf_url") or m.get("url_pdf")
    mime = meta.get("mime_type") or meta.get("mime") or m.get("mime_type")

    urls_contenido = _extraer_urls(contenido)
    if not media_url:
        for u in urls_contenido:
            if _es_url_imagen(u):
                media_url = u
                break

    adjuntos: list[dict] = []
    if media_url:
        adjuntos.append(
            {
                "tipo": "image" if (tipo == "image" or _es_url_imagen(str(media_url)) or (mime or "").startswith("image/")) else "file",
                "url": str(media_url),
                "mime": mime,
                "caption": meta.get("caption") or "",
            }
        )
    if url_pdf:
        adjuntos.append({"tipo": "document", "url": str(url_pdf), "mime": "application/pdf", "caption": "PDF"})
    if tipo == "image" and not any(a.get("tipo") == "image" for a in adjuntos):
        # Placeholder: el bot hoy no persiste la URL; la UI muestra tarjeta.
        caption = "Imagen recibida (comprobante)"
        if media_id:
            caption = "Imagen recibida (comprobante) — falta media_url en el bot"
        adjuntos.append(
            {
                "tipo": "image_placeholder",
                "url": None,
                "media_id": media_id,
                "caption": caption,
            }
        )
    elif tipo in {"document", "documento"} and url_pdf is None and not adjuntos:
        adjuntos.append(
            {
                "tipo": "document_placeholder",
                "url": None,
                "caption": contenido or "Documento",
            }
        )

    m["adjuntos"] = adjuntos
    m["tiene_imagen"] = any(a.get("tipo") in {"image", "image_placeholder"} for a in adjuntos)
    m["tiene_media"] = bool(adjuntos)
    return m


def _cargar_nombres_agenda(telefonos: list[str]) -> dict[str, dict]:
    """Mapa telefono_digits -> {nombre, fuente, telefono_display}."""
    result: dict[str, dict] = {}
    digits_list = [normalizar_telefono_digits(t) for t in telefonos if normalizar_telefono_digits(t)]
    if not digits_list:
        return result

    claves: set[str] = set()
    for d in digits_list:
        claves.update(telefono_claves_match(d))

    conn = db.get_connection()
    try:
        ensure_whatsapp_agenda(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1) Agenda WhatsApp (prioridad)
            cur.execute(
                """
                SELECT telefono_digits, telefono_display, nombre, notas, contacto_id
                FROM whatsapp_agenda
                WHERE telefono_digits = ANY(%s)
                """,
                (list(claves),),
            )
            for row in cur.fetchall():
                info = {
                    "nombre": str(row["nombre"] or "").strip(),
                    "fuente": "agenda",
                    "telefono_display": row.get("telefono_display"),
                    "notas": row.get("notas") or "",
                    "contacto_id": row.get("contacto_id"),
                }
                for k in telefono_claves_match(row["telefono_digits"]):
                    result[k] = info

            # 2) Contactos ERP (si aún no hay nombre en agenda)
            cur.execute(
                """
                SELECT id, nombre, telefono
                FROM contactos
                WHERE COALESCE(telefono, '') <> ''
                  AND COALESCE(nombre, '') <> ''
                LIMIT 5000
                """
            )
            for row in cur.fetchall():
                tel_d = normalizar_telefono_digits(row.get("telefono"))
                if not tel_d:
                    continue
                nombre = str(row.get("nombre") or "").strip()
                if not nombre:
                    continue
                for k in telefono_claves_match(tel_d):
                    if k in claves and k not in result:
                        result[k] = {
                            "nombre": nombre,
                            "fuente": "contactos",
                            "telefono_display": row.get("telefono"),
                            "notas": "",
                            "contacto_id": row.get("id"),
                        }
    except Exception as exc:
        print(f"[SUPERVISION AGENTE] No se pudo cargar agenda: {exc!r}", flush=True)
    finally:
        conn.release()
    return result


def _nombre_para_telefono(phone: str, nombres: dict[str, dict]) -> Optional[dict]:
    for k in telefono_claves_match(phone):
        if k in nombres and nombres[k].get("nombre"):
            return nombres[k]
    return None


def _normalizar_identificacion(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _cargar_contactos_por_identificacion(idents: list[str]) -> dict[str, dict]:
    """Mapa identificación (solo dígitos) -> {nombre, telefono} desde contactos ERP."""
    result: dict[str, dict] = {}
    keys = sorted({_normalizar_identificacion(i) for i in idents if _normalizar_identificacion(i)})
    if not keys:
        return result
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, nombre, telefono, identificacion
                FROM contactos
                WHERE COALESCE(identificacion, '') <> ''
                LIMIT 8000
                """
            )
            wanted = set(keys)
            for row in cur.fetchall():
                ident = _normalizar_identificacion(row.get("identificacion"))
                if ident not in wanted:
                    continue
                result[ident] = {
                    "nombre": str(row.get("nombre") or "").strip(),
                    "telefono": row.get("telefono") or "",
                    "contacto_id": row.get("id"),
                    "fuente": "contactos_cedula",
                }
    except Exception as exc:
        print(f"[SUPERVISION AGENTE] No se pudo cruzar cédulas: {exc!r}", flush=True)
    finally:
        conn.release()
    return result


def _normalizar_conversaciones(data, buscar: str = ""):
    if not isinstance(data, dict):
        return data
    filas = data.get("conversaciones")
    if not isinstance(filas, list):
        return data

    phones = []
    idents = []
    for fila in filas:
        if isinstance(fila, dict):
            phones.append(fila.get("phone") or fila.get("telefono") or "")
            idents.append(fila.get("identification") or fila.get("identificacion") or "")

    nombres = _cargar_nombres_agenda(phones)
    por_cedula = _cargar_contactos_por_identificacion(idents)
    out = []
    buscar_l = (buscar or "").strip().lower()

    for fila in filas:
        if not isinstance(fila, dict):
            continue
        fila = dict(fila)
        # conversation_key: clave del bot para /control/* (puede ser PSID). No sustituir por wa_id.
        conversation_key = (
            fila.get("conversation_key")
            or fila.get("telefono")
            or fila.get("phone")
            or ""
        )
        phone = str(conversation_key)
        fila["conversation_key"] = phone
        fila["phone"] = phone
        fila["telefono"] = phone
        fila["wa_id"] = fila.get("wa_id") or fila.get("phone_legible")
        fila["identification"] = fila.get("identification") or fila.get("identificacion")
        fila["identificacion"] = fila["identification"]
        fila["mode_current"] = (
            fila.get("mode_current") or fila.get("modo_actual") or fila.get("modo") or "AGENTE"
        )
        fila["modo_actual"] = fila["mode_current"]
        fila["started_at"] = fila.get("started_at") or fila.get("fecha_inicio")
        fila["last_activity"] = fila.get("last_activity") or fila.get("fecha_ultima_actividad")
        fila["human_user"] = fila.get("human_user") or fila.get("usuario_humano")

        ident_digits = _normalizar_identificacion(fila.get("identificacion"))
        contacto_cedula = por_cedula.get(ident_digits) if ident_digits else None

        info = _nombre_para_telefono(phone, nombres)
        if not info and fila.get("wa_id"):
            info = _nombre_para_telefono(str(fila.get("wa_id")), nombres)
        if not info and contacto_cedula and contacto_cedula.get("nombre"):
            info = {
                "nombre": contacto_cedula["nombre"],
                "fuente": contacto_cedula["fuente"],
                "telefono_display": contacto_cedula.get("telefono"),
                "notas": "",
                "contacto_id": contacto_cedula.get("contacto_id"),
            }

        legible = es_telefono_whatsapp_legible(phone) or es_telefono_whatsapp_legible(fila.get("wa_id"))
        fila["es_telefono_legible"] = bool(legible)
        telefono_resuelto = ""
        if es_telefono_whatsapp_legible(fila.get("wa_id")):
            telefono_resuelto = normalizar_telefono_digits(fila.get("wa_id"))
        elif info and info.get("telefono_display") and es_telefono_whatsapp_legible(info["telefono_display"]):
            telefono_resuelto = normalizar_telefono_digits(info["telefono_display"])
        elif contacto_cedula and es_telefono_whatsapp_legible(contacto_cedula.get("telefono")):
            telefono_resuelto = normalizar_telefono_digits(contacto_cedula.get("telefono"))
        elif es_telefono_whatsapp_legible(phone):
            telefono_resuelto = normalizar_telefono_digits(phone)

        fila["telefono_resuelto"] = telefono_resuelto or None
        fila["telefono_display"] = (
            formatear_telefono_display(telefono_resuelto)
            if telefono_resuelto
            else (formatear_telefono_display(phone) if legible else "")
        )
        if not fila["telefono_display"] and not legible and phone:
            # No mostrar el PSID crudo como “teléfono”.
            fila["telefono_display"] = (
                f"CC {fila['identificacion']}" if fila.get("identificacion") else "ID WhatsApp (sin número)"
            )
            fila["telefono_es_id_meta"] = True
        else:
            fila["telefono_es_id_meta"] = not legible

        preview = preview_ultimo_mensaje(fila)
        fila["last_message"] = preview
        fila["ultimo_mensaje"] = preview

        if info:
            fila["nombre"] = info["nombre"]
            fila["name"] = info["nombre"]
            fila["nombre_fuente"] = info["fuente"]
            fila["display_name"] = info["nombre"]
        else:
            fila["nombre"] = fila.get("nombre") or fila.get("name") or ""
            fila["name"] = fila["nombre"]
            fila["nombre_fuente"] = None
            if fila["nombre"]:
                fila["display_name"] = fila["nombre"]
            elif telefono_resuelto:
                fila["display_name"] = fila["telefono_display"] or telefono_resuelto
            elif fila.get("identificacion"):
                fila["display_name"] = f"CC {fila['identificacion']}"
            elif legible:
                fila["display_name"] = fila["telefono_display"] or phone
            else:
                fila["display_name"] = "Contacto WhatsApp"

        modo = str(fila["mode_current"]).upper()
        fila["ia_atiende"] = modo != "HUMANO"

        if buscar_l:
            haystack = " ".join(
                [
                    str(phone),
                    str(fila.get("telefono_display") or ""),
                    str(fila.get("telefono_resuelto") or ""),
                    str(fila.get("identificacion") or ""),
                    str(fila.get("nombre") or ""),
                    str(fila.get("display_name") or ""),
                    str(fila.get("last_message") or ""),
                ]
            ).lower()
            if buscar_l not in haystack:
                continue
        out.append(fila)

    # KPIs de aviso IA
    ia_count = sum(1 for f in out if f.get("ia_atiende"))
    data["conversaciones"] = out
    data["aviso_ia"] = {
        "activo": ia_count > 0,
        "total_ia": ia_count,
        "total_humano": len(out) - ia_count,
        "mensaje": (
            f"La IA está atendiendo {ia_count} conversación(es)."
            if ia_count
            else "No hay conversaciones bajo atención de la IA."
        ),
        "canal_implementado": "in_app",
        "canales_pendientes": ["email", "whatsapp", "slack"],
    }
    return data


def _normalizar_mensajes(data):
    if not isinstance(data, dict):
        return data
    mensajes = data.get("mensajes") or data.get("messages") or data.get("data")
    if not isinstance(mensajes, list):
        return data
    # El bot hace JOIN y repite c.usuario_humano en cada fila; eso no implica
    # que el mensaje lo haya escrito el abogado (rompe clasificarEmisor).
    conv_usuario = None
    out = []
    for m in mensajes:
        if not isinstance(m, dict):
            continue
        m = dict(m)
        if conv_usuario is None and m.get("usuario_humano"):
            conv_usuario = m.get("usuario_humano")
        autor = str(m.get("autor") or m.get("author") or "").upper()
        if autor != "HUMANO":
            m.pop("usuario_humano", None)
        out.append(enriquecer_mensaje(m))
    data["mensajes"] = out
    if conv_usuario and not data.get("usuario_humano"):
        data["usuario_humano"] = conv_usuario
        data["human_user"] = conv_usuario
    return data


def _disparar_webhook_ia(evento: str, payload: dict) -> None:
    """Webhook opcional (Slack/Zapier/n8n). No bloquea la respuesta al usuario."""
    url = (os.getenv("SUPERVISION_IA_WEBHOOK_URL") or "").strip()
    if not url:
        return
    body = {
        "evento": evento,
        "origen": "erp-despacho/supervision-agente",
        "ts": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    try:
        requests.post(url, json=body, timeout=5)
        print(f"[SUPERVISION AGENTE] Webhook IA enviado: {evento}", flush=True)
    except Exception as exc:
        print(f"[SUPERVISION AGENTE] Webhook IA falló: {exc!r}", flush=True)


def guardar_nombre_agenda(
    telefono: str,
    nombre: str,
    *,
    usuario: str = "",
    notas: str = "",
) -> dict:
    digits = normalizar_telefono_digits(telefono)
    nombre = (nombre or "").strip()
    if not digits:
        raise ValueError("Teléfono inválido")
    if not nombre:
        raise ValueError("Nombre requerido")
    if len(nombre) > 160:
        nombre = nombre[:160]

    conn = db.get_connection()
    try:
        ensure_whatsapp_agenda(conn)
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO whatsapp_agenda
                        (telefono_digits, telefono_display, nombre, notas, updated_by, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (telefono_digits) DO UPDATE SET
                        telefono_display = EXCLUDED.telefono_display,
                        nombre = EXCLUDED.nombre,
                        notas = COALESCE(NULLIF(EXCLUDED.notas, ''), whatsapp_agenda.notas),
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW()
                    RETURNING id, telefono_digits, telefono_display, nombre, notas, updated_at
                    """,
                    (digits, str(telefono), nombre, (notas or "").strip(), (usuario or "")[:120]),
                )
                row = dict(cur.fetchone())
        return {"status": "success", "contacto": row}
    finally:
        conn.release()


def obtener_nombre_agenda(telefono: str) -> Optional[dict]:
    digits = normalizar_telefono_digits(telefono)
    if not digits:
        return None
    nombres = _cargar_nombres_agenda([telefono])
    return _nombre_para_telefono(telefono, nombres)


# ---------------------------------------------------------------------------
# Rutas UI / API
# ---------------------------------------------------------------------------


@router.get("/supervision-agente")
def supervision_agente(request: Request):
    try:
        ensure_whatsapp_agenda()
    except Exception as exc:
        print(f"[SUPERVISION AGENTE] Agenda no disponible al cargar UI: {exc!r}", flush=True)
    return templates.TemplateResponse(
        request=request,
        name="supervision_agente.html",
        context={"request": request, "usuario": _usuario(request), "configurado": True},
    )


@router.get("/supervision-agente/api/estado")
def supervision_estado():
    data, status = _proxy("GET", "/control/health")
    return JSONResponse(data, status_code=status)


@router.get("/supervision-agente/api/conversaciones")
def supervision_conversaciones(limit: int = 200, buscar: str = ""):
    # Traemos el set del bot y filtramos aquí (permite buscar por nombre de agenda).
    data, status = _proxy(
        "GET",
        "/control/conversaciones",
        params={"limit": min(max(limit, 1), 200), "buscar": ""},
    )
    if status < 400:
        data = _normalizar_conversaciones(data, buscar=buscar)
    return JSONResponse(data, status_code=status)


@router.get("/supervision-agente/api/conversaciones/{telefono}/mensajes")
def supervision_mensajes(telefono: str, limit: int = 1000):
    data, status = _proxy(
        "GET",
        f"/control/conversaciones/{quote(telefono, safe='')}/mensajes",
        params={"limit": min(max(limit, 1), 1000)},
    )
    if status < 400:
        data = _normalizar_mensajes(data)
        info = obtener_nombre_agenda(telefono)
        if info:
            data["nombre_contacto"] = info.get("nombre")
            data["nombre_fuente"] = info.get("fuente")
    return JSONResponse(data, status_code=status)


@router.get("/supervision-agente/api/conversaciones/{telefono}/control")
def supervision_control(telefono: str):
    data, status = _proxy("GET", f"/control/conversaciones/{quote(telefono, safe='')}/control")
    return JSONResponse(data, status_code=status)


@router.post("/supervision-agente/api/tomar-control")
async def supervision_tomar_control(request: Request):
    payload = await request.json()
    payload["usuario"] = payload.get("usuario") or _usuario(request)
    data, status = _proxy("POST", "/control/tomar", json=payload)
    return JSONResponse(data, status_code=status)


@router.post("/supervision-agente/api/devolver-agente")
async def supervision_devolver_agente(request: Request):
    payload = await request.json()
    usuario = payload.get("usuario") or _usuario(request)
    payload["usuario"] = usuario
    data, status = _proxy("POST", "/control/devolver", json=payload)
    if status < 400:
        telefono = payload.get("telefono") or ""
        _disparar_webhook_ia(
            "ia_atiende",
            {
                "telefono": telefono,
                "usuario": usuario,
                "modo": "AGENTE",
                "mensaje": f"La IA retomó la atención de {telefono}",
            },
        )
    return JSONResponse(data, status_code=status)


@router.post("/supervision-agente/api/mensaje")
async def supervision_mensaje(request: Request):
    payload = await request.json()
    payload["usuario"] = payload.get("usuario") or _usuario(request)
    if not payload.get("texto") and payload.get("mensaje"):
        payload["texto"] = payload["mensaje"]
    data, status = _proxy("POST", "/control/mensaje", json=payload)
    return JSONResponse(data, status_code=status)


@router.get("/supervision-agente/api/agenda")
def supervision_agenda_listar(buscar: str = ""):
    conn = db.get_connection()
    try:
        ensure_whatsapp_agenda(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if buscar.strip():
                term = f"%{buscar.strip()}%"
                cur.execute(
                    """
                    SELECT id, telefono_digits, telefono_display, nombre, notas, updated_at
                    FROM whatsapp_agenda
                    WHERE nombre ILIKE %s OR telefono_digits ILIKE %s OR COALESCE(telefono_display,'') ILIKE %s
                    ORDER BY updated_at DESC
                    LIMIT 500
                    """,
                    (term, term, term),
                )
            else:
                cur.execute(
                    """
                    SELECT id, telefono_digits, telefono_display, nombre, notas, updated_at
                    FROM whatsapp_agenda
                    ORDER BY updated_at DESC
                    LIMIT 500
                    """
                )
            rows = [dict(r) for r in cur.fetchall()]
        return JSONResponse({"status": "success", "contactos": rows})
    except Exception as exc:
        return JSONResponse({"status": "error", "mensaje": str(exc)}, status_code=500)
    finally:
        conn.release()


@router.get("/supervision-agente/api/agenda/{telefono}")
def supervision_agenda_obtener(telefono: str):
    info = obtener_nombre_agenda(telefono)
    if not info:
        return JSONResponse(
            {
                "status": "success",
                "contacto": None,
                "telefono": telefono,
                "telefono_digits": normalizar_telefono_digits(telefono),
            }
        )
    return JSONResponse(
        {
            "status": "success",
            "contacto": {
                "telefono": telefono,
                "telefono_digits": normalizar_telefono_digits(telefono),
                **info,
            },
        }
    )


@router.post("/supervision-agente/api/agenda")
async def supervision_agenda_guardar(request: Request):
    payload = await request.json()
    telefono = payload.get("telefono") or payload.get("phone") or ""
    nombre = payload.get("nombre") or payload.get("name") or ""
    notas = payload.get("notas") or ""
    try:
        data = guardar_nombre_agenda(
            telefono,
            nombre,
            usuario=_usuario(request),
            notas=notas,
        )
        return JSONResponse(data)
    except ValueError as exc:
        return JSONResponse({"status": "error", "mensaje": str(exc)}, status_code=400)
    except Exception as exc:
        print(f"[SUPERVISION AGENTE] Error guardando agenda: {exc!r}", flush=True)
        return JSONResponse(
            {"status": "error", "mensaje": "No fue posible guardar el contacto"},
            status_code=500,
        )


@router.get("/supervision-agente/api/aviso-ia")
def supervision_aviso_ia(limit: int = 200):
    """Resumen para badge/banner: conversaciones donde la IA atiende."""
    data, status = _proxy(
        "GET",
        "/control/conversaciones",
        params={"limit": min(max(limit, 1), 200), "buscar": ""},
    )
    if status >= 400:
        return JSONResponse(data, status_code=status)
    data = _normalizar_conversaciones(data, buscar="")
    ia = [
        {
            "phone": c.get("phone"),
            "nombre": c.get("display_name") or c.get("nombre") or c.get("phone"),
            "last_activity": c.get("last_activity"),
            "identificacion": c.get("identificacion"),
        }
        for c in (data.get("conversaciones") or [])
        if c.get("ia_atiende")
    ]
    return JSONResponse(
        {
            "status": "success",
            "aviso_ia": data.get("aviso_ia"),
            "conversaciones_ia": ia,
            "implementado": {
                "in_app_badge": True,
                "in_app_toast": True,
                "webhook_env": "SUPERVISION_IA_WEBHOOK_URL",
                "webhook_activo": bool((os.getenv("SUPERVISION_IA_WEBHOOK_URL") or "").strip()),
            },
            "pendiente_decision": {
                "email": "No hay SMTP/SendGrid en el ERP; requiere canal y destinatarios (Paredes / otro abogado).",
                "whatsapp": "Requiere plantilla Meta + números destino; el bot no expone API de aviso a abogados.",
                "slack": "Usar SUPERVISION_IA_WEBHOOK_URL apuntando a Incoming Webhook de Slack.",
            },
        }
    )
