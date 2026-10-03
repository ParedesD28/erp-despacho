"""
Fallback IA (visión/PDF) para estados de cuenta sin texto nativo.

Proveedores:
  - gemini (default / gratis generoso): Google Gemini Flash multimodal
  - anthropic (opcional): Claude vía document PDF

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

_PROVEEDOR_GEMINI = "gemini"
_PROVEEDOR_ANTHROPIC = "anthropic"
_MODELO_GEMINI_DEFAULT = "gemini-2.5-flash"
_MODELO_ANTHROPIC_DEFAULT = "claude-sonnet-5-5"


def proveedor_ia() -> str:
    """
    PDF_IA_PROVIDER / ESTADO_CUENTA_IA_PROVIDER → gemini|anthropic.
    Sin override: Gemini si hay GEMINI_API_KEY; si no, Anthropic si hay key;
    si ninguna key, default gemini (mensaje de error apunta al gratis).
    """
    raw = (
        os.environ.get("PDF_IA_PROVIDER")
        or os.environ.get("ESTADO_CUENTA_IA_PROVIDER")
        or ""
    ).strip().lower()
    if raw in ("gemini", "google", "google-genai"):
        return _PROVEEDOR_GEMINI
    if raw in ("anthropic", "claude"):
        return _PROVEEDOR_ANTHROPIC
    if (os.environ.get("GEMINI_API_KEY") or "").strip():
        return _PROVEEDOR_GEMINI
    if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return _PROVEEDOR_ANTHROPIC
    return _PROVEEDOR_GEMINI


def _modelo_default(provider: str | None = None) -> str:
    """ESTADO_CUENTA_IA_MODEL → (GEMINI_MODEL|ANTHROPIC_MODEL) → default del proveedor."""
    prov = provider or proveedor_ia()
    explicit = (os.environ.get("ESTADO_CUENTA_IA_MODEL") or "").strip()
    if explicit:
        return explicit
    if prov == _PROVEEDOR_GEMINI:
        return (os.environ.get("GEMINI_MODEL") or "").strip() or _MODELO_GEMINI_DEFAULT
    return (os.environ.get("ANTHROPIC_MODEL") or "").strip() or _MODELO_ANTHROPIC_DEFAULT


# PDFs largos (6–7 págs.) generan JSON grande; 8k truncaba mid-string.
_MAX_TOKENS = max(4096, min(int(os.environ.get("ESTADO_CUENTA_IA_MAX_TOKENS", "32000")), 64000))

# tool_choice type "tool"/"any" rompe prod en modelos Anthropic que no lo soportan.
# Default seguro: texto JSON + parse/repair/retry (sin tools).
# Opt-in: ESTADO_CUENTA_IA_USE_TOOLS=1 solo si el modelo sí soporta tools forzados.
def _use_tools_habilitado() -> bool:
    flag = (os.environ.get("ESTADO_CUENTA_IA_USE_TOOLS") or "").strip().lower()
    return flag in ("1", "true", "yes", "on")


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_TOOL_NAME = "extraer_estado_cuenta"
_TOOL_CHOICE_UNSUPPORTED_RE = re.compile(
    r"tool_choice|type\s*[\"']?(?:tool|any)[\"']?\s*are not supported",
    re.IGNORECASE,
)

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


def _api_key_opcional(provider: str | None = None) -> str:
    prov = provider or proveedor_ia()
    if prov == _PROVEEDOR_GEMINI:
        return (os.environ.get("GEMINI_API_KEY") or "").strip()
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def ia_fallback_habilitado() -> bool:
    """
    Default: ON si el proveedor resuelto tiene API key.
    Override: ESTADO_CUENTA_IA_FALLBACK=0|1|true|false.
    """
    flag = (os.environ.get("ESTADO_CUENTA_IA_FALLBACK") or "").strip().lower()
    tiene_key = bool(_api_key_opcional())
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return tiene_key
    return tiene_key


def _require_api_key(provider: str | None = None) -> str:
    prov = provider or proveedor_ia()
    key = _api_key_opcional(prov)
    if key:
        return key
    if prov == _PROVEEDOR_GEMINI:
        raise EstadoCuentaIaError(
            "configure GEMINI_API_KEY (variable de entorno; no hardcodear en código). "
            "Ver docs/ia-fallback-pdf-modelo.md. "
            "Opcional: PDF_IA_PROVIDER=anthropic + ANTHROPIC_API_KEY."
        )
    raise EstadoCuentaIaError(
        "configure ANTHROPIC_API_KEY (variable de entorno; no hardcodear en código)."
    )


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
    Valida el JSON de la IA y lo mapea a la estructura interna COLON
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


def _mapear_error_api(exc: Exception, modelo: str, provider: str) -> EstadoCuentaIaError | None:
    nombre = type(exc).__name__
    msg = str(exc)
    msg_l = msg.lower()
    if provider == _PROVEEDOR_GEMINI:
        if (
            "api key" in msg_l
            or "api_key" in msg_l
            or "unauthenticated" in msg_l
            or "401" in msg
            or nombre in ("UnauthenticatedError", "PermissionDeniedError")
        ):
            return EstadoCuentaIaError(
                "GEMINI_API_KEY inválida o rechazada por Google. "
                "Revise el secret en Render / entorno."
            )
        if "404" in msg or "not found" in msg_l or ("model" in msg_l and "invalid" in msg_l):
            return EstadoCuentaIaError(
                f"Modelo Gemini no disponible ({modelo}). "
                "Ajuste ESTADO_CUENTA_IA_MODEL o GEMINI_MODEL."
            )
        if "resource_exhausted" in msg_l or "429" in msg or "quota" in msg_l:
            return EstadoCuentaIaError(
                "Cuota/rate-limit de Gemini agotada (free tier). "
                "Reintente más tarde o revise límites en AI Studio."
            )
        return None

    if "authentication" in msg_l or nombre == "AuthenticationError":
        return EstadoCuentaIaError(
            "ANTHROPIC_API_KEY inválida o rechazada por Anthropic. "
            "Revise el secret en Render / entorno."
        )
    if "not_found" in msg_l or ("model" in msg_l and "404" in msg):
        return EstadoCuentaIaError(
            f"Modelo Claude no disponible ({modelo}). "
            "Ajuste ESTADO_CUENTA_IA_MODEL o ANTHROPIC_MODEL."
        )
    return None


def _es_error_tool_choice_no_soportado(exc: Exception) -> bool:
    msg = str(exc)
    nombre = type(exc).__name__
    if _TOOL_CHOICE_UNSUPPORTED_RE.search(msg):
        return True
    # BadRequest 400 típico de Anthropic cuando el modelo no acepta tool/any
    if nombre in ("BadRequestError", "APIError") and "tool_choice" in msg.lower():
        return True
    return False


def _construir_kwargs_mensaje(
    *,
    modelo: str,
    content: list[dict[str, Any]],
    use_tools: bool,
    include_output_config: bool,
) -> dict[str, Any]:
    """
    Arma kwargs de messages.create (Anthropic).
    Default prod-safe: sin tools ni tool_choice (JSON por prompt + parse).
    """
    kwargs: dict[str, Any] = {
        "model": modelo,
        "max_tokens": _MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
    }
    if use_tools:
        kwargs["tools"] = [_TOOL_SCHEMA]
        # type "any" también falla en modelos sin tools; solo "tool" opt-in.
        kwargs["tool_choice"] = {"type": "tool", "name": _TOOL_NAME}
    if include_output_config:
        kwargs["output_config"] = {"effort": "medium"}
    return kwargs


def _llamar_claude_pdf(
    pdf_bytes: bytes,
    *,
    model: str | None = None,
    retry_json: bool = False,
    use_tools: bool | None = None,
) -> dict[str, Any] | str:
    """
    Envía el PDF a Claude y retorna dict (tool_use) o texto JSON.

    Por defecto NO usa tool_choice (portable). Opt-in tools vía
    ESTADO_CUENTA_IA_USE_TOOLS=1; si la API responde 400 por tool_choice,
    reintenta automáticamente en modo texto.
    """
    api_key = _require_api_key(_PROVEEDOR_ANTHROPIC)
    try:
        import anthropic
    except ImportError as exc:
        raise EstadoCuentaIaError(
            "Falta el paquete anthropic. Instálelo en el entorno (requirements.txt)."
        ) from exc

    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    modelo = model or _modelo_default(_PROVEEDOR_ANTHROPIC)
    client = anthropic.Anthropic(api_key=api_key, timeout=_IA_TIMEOUT_S)
    tools_on = _use_tools_habilitado() if use_tools is None else use_tools

    if retry_json:
        user_text = _RETRY_USER
    elif tools_on:
        user_text = (
            "Extrae cabecera y movimientos del estado de cuenta. "
            "Usa la herramienta extraer_estado_cuenta con el JSON completo."
        )
    else:
        user_text = (
            "Extrae cabecera y movimientos del estado de cuenta. "
            "Responde ÚNICAMENTE con el objeto JSON completo "
            "(cabecera + movimientos), sin markdown ni texto adicional."
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

    def _create(use_tools_flag: bool, with_output_config: bool):
        kwargs = _construir_kwargs_mensaje(
            modelo=modelo,
            content=content,
            use_tools=use_tools_flag,
            include_output_config=with_output_config,
        )
        return client.messages.create(**kwargs), kwargs

    # output_config/effort solo en modelos nuevos; default OFF para portabilidad.
    # Tools/tool_choice solo si use_tools=True (opt-in); si 400 → texto.
    with _IA_SEMAPHORE:
        try:
            message, used_kwargs = _create(tools_on, False)
        except Exception as exc:
            if tools_on and _es_error_tool_choice_no_soportado(exc):
                # Modelo sin tool_choice → fallback texto (prod-safe)
                try:
                    message, used_kwargs = _create(False, False)
                except Exception as exc2:
                    mapped = _mapear_error_api(exc2, modelo, _PROVEEDOR_ANTHROPIC)
                    if mapped:
                        raise mapped from exc2
                    raise
            else:
                mapped = _mapear_error_api(exc, modelo, _PROVEEDOR_ANTHROPIC)
                if mapped:
                    raise mapped from exc
                raise

    _ = used_kwargs  # disponible si se instrumenta logging
    return _extraer_payload_mensaje(message)


def _llamar_gemini_pdf(
    pdf_bytes: bytes,
    *,
    model: str | None = None,
    retry_json: bool = False,
) -> str:
    """
    Envía el PDF a Gemini (inline application/pdf) y retorna texto JSON.
    Usa response_mime_type=application/json para forzar objeto JSON.
    """
    api_key = _require_api_key(_PROVEEDOR_GEMINI)
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise EstadoCuentaIaError(
            "Falta el paquete google-genai. Instálelo en el entorno (requirements.txt)."
        ) from exc

    modelo = model or _modelo_default(_PROVEEDOR_GEMINI)
    user_text = _RETRY_USER if retry_json else (
        "Extrae cabecera y movimientos del estado de cuenta. "
        "Responde ÚNICAMENTE con el objeto JSON completo "
        "(cabecera + movimientos), sin markdown ni texto adicional."
    )

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=_IA_TIMEOUT_S * 1000),
    )
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        max_output_tokens=_MAX_TOKENS,
        response_mime_type="application/json",
        temperature=0,
    )
    contents = [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        user_text,
    ]

    with _IA_SEMAPHORE:
        try:
            response = client.models.generate_content(
                model=modelo,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            mapped = _mapear_error_api(exc, modelo, _PROVEEDOR_GEMINI)
            if mapped:
                raise mapped from exc
            raise

    texto = (getattr(response, "text", None) or "").strip()
    if not texto:
        # Algunos SDKs exponen candidates[0].content.parts
        partes: list[str] = []
        for cand in getattr(response, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                t = getattr(part, "text", None)
                if t:
                    partes.append(t)
        texto = "\n".join(partes).strip()
    return texto


def _llamar_proveedor_pdf(
    pdf_bytes: bytes,
    *,
    model: str | None = None,
    retry_json: bool = False,
) -> dict[str, Any] | str:
    """Despacha al proveedor configurado (gemini|anthropic)."""
    prov = proveedor_ia()
    if prov == _PROVEEDOR_GEMINI:
        return _llamar_gemini_pdf(pdf_bytes, model=model, retry_json=retry_json)
    if prov == _PROVEEDOR_ANTHROPIC:
        return _llamar_claude_pdf(pdf_bytes, model=model, retry_json=retry_json)
    raise EstadoCuentaIaError(
        f"PDF_IA_PROVIDER desconocido: {prov!r}. Use gemini o anthropic."
    )


def extraer_estado_cuenta_via_ia(pdf_bytes: bytes, *, model: str | None = None) -> dict[str, Any]:
    """
    Llama al proveedor IA, valida JSON y retorna estructura parcial compatible
    con analizar_estado_cuenta_pdf (cabecera, rows, métricas base).
    """
    if not pdf_bytes:
        raise EstadoCuentaIaError("PDF vacío para fallback IA.")

    payload = _llamar_proveedor_pdf(pdf_bytes, model=model, retry_json=False)
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
        payload2 = _llamar_proveedor_pdf(pdf_bytes, model=model, retry_json=True)
        if isinstance(payload2, dict):
            data2 = payload2
        else:
            data2 = _parse_json_respuesta(payload2)
        return validar_y_mapear_respuesta_ia(data2)
