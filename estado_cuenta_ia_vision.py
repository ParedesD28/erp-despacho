"""
Fallback Claude (Anthropic) para estados de cuenta PDF sin texto nativo.

Solo se usa cuando pypdf no encuentra texto seleccionable suficiente.
Bolsa Global / mora NO se calculan aquí: la IA solo estructura cabecera +
movimientos; el motor determinista opera después sobre ese JSON.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from typing import Any

# Un PDF imagen a la vez: no tumba lotes de nativos COLON en paralelo.
_IA_SEMAPHORE = threading.Semaphore(1)
_IA_TIMEOUT_S = max(30, min(int(os.environ.get("ESTADO_CUENTA_IA_TIMEOUT_S", "120")), 300))
_DEFAULT_MODEL = os.environ.get(
    "ESTADO_CUENTA_IA_MODEL",
    "claude-sonnet-5-5",
)
# PDFs largos (6–7 págs.) generan JSON grande; 8k truncaba mid-string.
_MAX_TOKENS = max(4096, min(int(os.environ.get("ESTADO_CUENTA_IA_MAX_TOKENS", "32000")), 64000))

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_TOOL_NAME = "extraer_estado_cuenta"

_MOVIMIENTO_SCHEMA = {
    "type": "object",
    "properties": {
        "concepto": {"type": "string"},
        "tipo_documento": {"type": "string"},
        "numero": {"type": "string"},
        "fecha": {"type": "string"},
        "valor": {"type": "number"},
        "abono": {"type": "number"},
        "saldo": {"type": "number"},
    },
    "required": [
        "concepto",
        "tipo_documento",
        "numero",
        "fecha",
        "valor",
        "abono",
        "saldo",
    ],
    "additionalProperties": False,
}

_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": (
        "Devuelve cabecera y movimientos del estado de cuenta COLON. "
        "Única forma de respuesta permitida."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cabecera": {
                "type": "object",
                "properties": {
                    "conjunto": {"type": ["string", "null"]},
                    "titular": {"type": ["string", "null"]},
                    "bloque": {"type": ["string", "null"]},
                    "apartamento": {"type": ["string", "null"]},
                    "codigo_cuenta": {"type": ["string", "null"]},
                    "nit": {"type": ["string", "null"]},
                },
                "required": [
                    "conjunto",
                    "titular",
                    "bloque",
                    "apartamento",
                    "codigo_cuenta",
                    "nit",
                ],
                "additionalProperties": False,
            },
            "movimientos": {
                "type": "array",
                "items": _MOVIMIENTO_SCHEMA,
            },
        },
        "required": ["cabecera", "movimientos"],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = """Eres un extractor determinista de estados de cuenta COLON Contabilidad (Colombia).
Debes leer el PDF/imagen y devolver ÚNICAMENTE un JSON válido (sin markdown, sin comentarios)
con esta forma exacta:

{
  "cabecera": {
    "conjunto": "string o null",
    "titular": "string o null",
    "bloque": "string o null",
    "apartamento": "string o null",
    "codigo_cuenta": "string o null",
    "nit": "string o null"
  },
  "movimientos": [
    {
      "concepto": "string",
      "tipo_documento": "FAC|RDC|otros 2-6 letras",
      "numero": "solo dígitos del documento",
      "fecha": "YYYY.MM.DD",
      "valor": number,
      "abono": number,
      "saldo": number
    }
  ]
}

Reglas estrictas:
- Extrae SOLO filas de movimiento reales (FAC/RDC u otros docs). Ignora "Periodo N", "Saldo anterior", subtotales, totales, pie de página.
- Fechas siempre YYYY.MM.DD (puntos, no guiones).
- Montos como números (sin comas de miles). valor/abono/saldo >= 0.
- No inventes movimientos. Si un campo no se lee, usa null en cabecera o omite la fila dudosa.
- No calcules mora ni reclasifiques conceptos. Copia el texto del concepto tal cual.
- En JSON, escapa comillas internas del concepto con \\". No uses comillas tipográficas.
- codigo_cuenta suele ser la Identificación (ej. 1204). apartamento = Número del inmueble.
- Respuesta completa: no truncar el array movimientos.
"""

_RETRY_USER = (
    "Tu respuesta anterior tenía JSON inválido o incompleto. "
    "Repite SOLO el JSON válido completo (objeto con cabecera y movimientos), "
    "sin markdown, sin comentarios, con comillas escapadas en conceptos."
)


class EstadoCuentaIaError(ValueError):
    """Error controlado del fallback IA (key, API, JSON inválido)."""


def ia_fallback_habilitado() -> bool:
    """
    Default: ON si existe ANTHROPIC_API_KEY.
    Override: ESTADO_CUENTA_IA_FALLBACK=0|1|true|false.
    """
    flag = (os.environ.get("ESTADO_CUENTA_IA_FALLBACK") or "").strip().lower()
    tiene_key = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return tiene_key
    return tiene_key


def _require_api_key() -> str:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise EstadoCuentaIaError(
            "configure ANTHROPIC_API_KEY (variable de entorno; no hardcodear en código)."
        )
    return key


def _escapar_comillas_interiores(raw: str) -> str:
    """
    Escapa comillas no escapadas dentro de strings JSON cuando el cierre
    real aún no llega (lookahead no es , } ]). No altera números.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    in_string = False
    while i < n:
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            # Cierre real de string: sigue , } ] o : (clave JSON).
            if j >= n or raw[j] in ",}]:":
                out.append(ch)
                in_string = False
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def reparar_json_ligero(texto: str) -> str:
    """
    Reparación conservadora: fences, trailing commas, comillas tipográficas
    y comillas sin escapar en strings. No inventa montos ni filas.
    """
    raw = (texto or "").strip()
    if not raw:
        return raw
    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    inicio = raw.find("{")
    fin = raw.rfind("}")
    if inicio >= 0 and fin > inicio:
        raw = raw[inicio : fin + 1]
    raw = (
        raw.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    raw = _escapar_comillas_interiores(raw)
    return raw


def _parse_json_respuesta(texto: str) -> dict[str, Any]:
    raw = (texto or "").strip()
    if not raw:
        raise EstadoCuentaIaError("La IA devolvió respuesta vacía.")

    candidatos = [raw]
    reparado = reparar_json_ligero(raw)
    if reparado and reparado != raw:
        candidatos.append(reparado)

    ultimo_exc: Exception | None = None
    for cand in candidatos:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
            raise EstadoCuentaIaError("JSON de la IA debe ser un objeto.")
        except json.JSONDecodeError as exc:
            ultimo_exc = exc
        # Intento: primer objeto {...} sin repair adicional
        inicio = cand.find("{")
        fin = cand.rfind("}")
        if inicio >= 0 and fin > inicio:
            fragmento = cand[inicio : fin + 1]
            if fragmento not in candidatos:
                try:
                    data = json.loads(fragmento)
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError as exc:
                    ultimo_exc = exc
                try:
                    data = json.loads(reparar_json_ligero(fragmento))
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError as exc:
                    ultimo_exc = exc

    detalle = f"{ultimo_exc}" if ultimo_exc else "no parseable"
    raise EstadoCuentaIaError(f"JSON inválido de la IA: {detalle}") from ultimo_exc


def _normalizar_monto(raw: Any) -> float:
    if raw is None or raw == "":
        raise ValueError("monto vacío")
    if isinstance(raw, (int, float)):
        return float(raw)
    txt = str(raw).strip().replace(" ", "").replace(",", "")
    return float(txt)


def _normalizar_fecha(raw: Any) -> str:
    txt = str(raw or "").strip()
    txt = txt.replace("-", ".").replace("/", ".")
    partes = txt.split(".")
    if len(partes) != 3:
        raise ValueError("fecha inválida")
    y, m, d = (p.zfill(2) if i else p for i, p in enumerate(partes))
    if len(y) != 4:
        raise ValueError("fecha inválida")
    return f"{y}.{m}.{d}"


def validar_y_mapear_respuesta_ia(data: dict[str, Any]) -> dict[str, Any]:
    """
    Valida el JSON de Claude y lo mapea a la estructura interna COLON
    (cabecera + rows con columnas del Excel/Bolsa).
    """
    cab_raw = data.get("cabecera") if isinstance(data.get("cabecera"), dict) else {}
    movs_raw = data.get("movimientos")
    if not isinstance(movs_raw, list) or not movs_raw:
        raise EstadoCuentaIaError("La IA no extrajo movimientos.")

    cabecera = {
        "titular": (str(cab_raw["titular"]).strip() if cab_raw.get("titular") else None),
        "bloque": (str(cab_raw["bloque"]).strip() if cab_raw.get("bloque") is not None else None),
        "apartamento": (
            str(cab_raw["apartamento"]).strip()
            if cab_raw.get("apartamento") is not None
            else None
        ),
        "codigo_cuenta": (
            str(cab_raw["codigo_cuenta"]).strip()
            if cab_raw.get("codigo_cuenta") is not None
            else None
        ),
        "conjunto": (str(cab_raw["conjunto"]).strip() if cab_raw.get("conjunto") else None),
        "advertencia_encoding": False,
    }

    rows: list[dict] = []
    omitidos: list[dict] = []
    for idx, item in enumerate(movs_raw):
        if not isinstance(item, dict):
            omitidos.append({"indice": idx, "motivo": "elemento no es objeto"})
            continue
        try:
            concepto = str(item.get("concepto") or "").strip()
            tipo = str(item.get("tipo_documento") or item.get("tipo") or "").strip().upper()
            numero = re.sub(r"\D", "", str(item.get("numero") or ""))
            fecha = _normalizar_fecha(item.get("fecha"))
            valor = _normalizar_monto(item.get("valor", 0))
            abono = _normalizar_monto(item.get("abono", 0))
            saldo = _normalizar_monto(item.get("saldo", 0))
            if len(concepto) < 3:
                raise ValueError("concepto inválido")
            if not re.match(r"^[A-Z]{2,6}$", tipo):
                raise ValueError("tipo documento inválido")
            if not re.match(r"^\d{4,12}$", numero):
                raise ValueError("número documento inválido")
            if valor < 0 or abono < 0:
                raise ValueError("montos negativos")
            if not re.match(r"^\d{4}\.\d{2}\.\d{2}$", fecha):
                raise ValueError("fecha inválida")
            rows.append(
                {
                    "Archivo": None,
                    "Titular": cabecera.get("titular"),
                    "Bloque": cabecera.get("bloque"),
                    "Apartamento": cabecera.get("apartamento"),
                    "Codigo Cuenta": cabecera.get("codigo_cuenta"),
                    "Concepto": concepto,
                    "Tipo Documento": tipo,
                    "Número": numero,
                    "Fecha": fecha,
                    "Valor": valor,
                    "Abono": abono,
                    "Saldo": saldo,
                }
            )
        except (ValueError, TypeError) as exc:
            omitidos.append(
                {
                    "indice": idx,
                    "fecha": str(item.get("fecha") or ""),
                    "motivo": str(exc) or "fila inválida",
                }
            )

    if not rows:
        raise EstadoCuentaIaError(
            "Ningún movimiento de la IA pasó validación estructural."
        )

    return {
        "cabecera": cabecera,
        "rows": rows,
        "omitidos_detalle": omitidos[:20],
        "bloques_omitidos": len(omitidos),
        "fechas_detectadas": len(rows) + len(omitidos),
    }


def _extraer_payload_mensaje(message: Any) -> dict[str, Any] | str:
    """
    Preferir tool_use (JSON ya estructurado por la API).
    Si no hay tool, devolver texto para parseo/repair.
    """
    for block in message.content or []:
        btype = getattr(block, "type", None)
        if btype == "tool_use" and getattr(block, "name", None) == _TOOL_NAME:
            inp = getattr(block, "input", None)
            if isinstance(inp, dict):
                return inp
            if isinstance(inp, str):
                return inp
    partes: list[str] = []
    for block in message.content or []:
        texto = getattr(block, "text", None)
        if texto:
            partes.append(texto)
    return "\n".join(partes).strip()


def _mapear_error_api(exc: Exception, modelo: str) -> EstadoCuentaIaError | None:
    nombre = type(exc).__name__
    msg = str(exc)
    if "authentication" in msg.lower() or nombre == "AuthenticationError":
        return EstadoCuentaIaError(
            "ANTHROPIC_API_KEY inválida o rechazada por Anthropic. "
            "Revise el secret en Render / entorno."
        )
    if "not_found" in msg.lower() or ("model" in msg.lower() and "404" in msg):
        return EstadoCuentaIaError(
            f"Modelo Claude no disponible ({modelo}). "
            "Ajuste ESTADO_CUENTA_IA_MODEL."
        )
    return None


def _llamar_claude_pdf(
    pdf_bytes: bytes,
    *,
    model: str | None = None,
    retry_json: bool = False,
) -> dict[str, Any] | str:
    """Envía el PDF a Claude (document + tool JSON) y retorna dict o texto."""
    api_key = _require_api_key()
    try:
        import anthropic
    except ImportError as exc:
        raise EstadoCuentaIaError(
            "Falta el paquete anthropic. Instálelo en el entorno (requirements.txt)."
        ) from exc

    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    modelo = model or _DEFAULT_MODEL
    client = anthropic.Anthropic(api_key=api_key, timeout=_IA_TIMEOUT_S)

    user_text = (
        _RETRY_USER
        if retry_json
        else (
            "Extrae cabecera y movimientos del estado de cuenta. "
            "Usa la herramienta extraer_estado_cuenta con el JSON completo."
        )
    )
    content: list[dict[str, Any]] = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": b64,
            },
        },
        {"type": "text", "text": user_text},
    ]

    kwargs: dict[str, Any] = {
        "model": modelo,
        "max_tokens": _MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "tools": [_TOOL_SCHEMA],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        "messages": [{"role": "user", "content": content}],
    }
    # effort opcional (modelos nuevos); ignorar si el SDK/API lo rechaza
    try:
        kwargs["output_config"] = {"effort": "medium"}
    except Exception:
        pass

    with _IA_SEMAPHORE:
        try:
            message = client.messages.create(**kwargs)
        except TypeError:
            # SDK viejo sin output_config / tool params distintos
            kwargs.pop("output_config", None)
            try:
                message = client.messages.create(**kwargs)
            except Exception as exc:
                mapped = _mapear_error_api(exc, modelo)
                if mapped:
                    raise mapped from exc
                raise
        except Exception as exc:
            # Reintento sin output_config si el API lo rechaza
            if "output_config" in str(exc).lower() or "effort" in str(exc).lower():
                kwargs.pop("output_config", None)
                try:
                    message = client.messages.create(**kwargs)
                except Exception as exc2:
                    mapped = _mapear_error_api(exc2, modelo)
                    if mapped:
                        raise mapped from exc2
                    raise
            else:
                mapped = _mapear_error_api(exc, modelo)
                if mapped:
                    raise mapped from exc
                raise

    return _extraer_payload_mensaje(message)


def extraer_estado_cuenta_via_ia(pdf_bytes: bytes, *, model: str | None = None) -> dict[str, Any]:
    """
    Llama a Claude, valida JSON y retorna estructura parcial compatible
    con analizar_estado_cuenta_pdf (cabecera, rows, métricas base).
    """
    if not pdf_bytes:
        raise EstadoCuentaIaError("PDF vacío para fallback IA.")

    payload = _llamar_claude_pdf(pdf_bytes, model=model, retry_json=False)
    try:
        if isinstance(payload, dict):
            data = payload
        else:
            data = _parse_json_respuesta(payload)
        return validar_y_mapear_respuesta_ia(data)
    except EstadoCuentaIaError as first_exc:
        msg = str(first_exc).lower()
        # Reintento solo si falló el parseo JSON (comillas/truncado), no validación de filas.
        if "json" not in msg and "vacía" not in msg and "parseable" not in msg:
            raise
        payload2 = _llamar_claude_pdf(pdf_bytes, model=model, retry_json=True)
        if isinstance(payload2, dict):
            data2 = payload2
        else:
            data2 = _parse_json_respuesta(payload2)
        return validar_y_mapear_respuesta_ia(data2)
