"""Puente seguro ERP -> agente de cobranza (Supervisión WhatsApp)."""
import os
import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_TIMEOUT = 20
_DEFAULT_AGENT_URL = "https://bot-cobranzas-ph.onrender.com"


def _agent_url():
    return (os.getenv("AGENT_SUPERVISION_URL") or _DEFAULT_AGENT_URL).rstrip("/")


def _agent_headers():
    key = os.getenv("AGENT_SUPERVISION_KEY") or os.getenv("LIQUIDADOR_API_KEY") or ""
    return {"X-Agent-Supervision-Key": key, "Accept": "application/json", "Content-Type": "application/json"}


def _proxy(method, path, *, json=None, params=None):
    base = _agent_url()
    key = os.getenv("AGENT_SUPERVISION_KEY") or os.getenv("LIQUIDADOR_API_KEY") or ""
    if not key:
        print("[SUPERVISION AGENTE][ALERTA] No existe AGENT_SUPERVISION_KEY ni LIQUIDADOR_API_KEY", flush=True)
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
            data = {"status": "error", "mensaje": f"Respuesta no JSON del agente: HTTP {response.status_code}"}
        if response.status_code >= 500:
            print(f"[SUPERVISION AGENTE][ALERTA] Agente respondio HTTP {response.status_code} en {path}", flush=True)
        return data, response.status_code
    except requests.RequestException as exc:
        print(f"[SUPERVISION AGENTE][ALERTA] Error de conexion al agente: {exc!r}", flush=True)
        return {"status": "error", "codigo": "AGENT_UNREACHABLE", "mensaje": f"Agente no disponible: {exc}"}, 503


def _usuario(request: Request):
    token = request.cookies.get("token_erp")
    return str(token or "Supervisor ERP")


def _normalizar_conversaciones(data):
    if not isinstance(data, dict):
        return data
    filas = data.get("conversaciones")
    if not isinstance(filas, list):
        return data
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        fila["phone"] = fila.get("phone") or fila.get("telefono")
        fila["identification"] = fila.get("identification") or fila.get("identificacion")
        fila["mode_current"] = fila.get("mode_current") or fila.get("modo_actual") or "AGENTE"
        fila["started_at"] = fila.get("started_at") or fila.get("fecha_inicio")
        fila["last_activity"] = fila.get("last_activity") or fila.get("fecha_ultima_actividad")
        fila["human_user"] = fila.get("human_user") or fila.get("usuario_humano")
    return data


@router.get("/supervision-agente")
def supervision_agente(request: Request):
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
    data, status = _proxy(
        "GET",
        "/control/conversaciones",
        params={"limit": min(max(limit, 1), 200), "buscar": buscar},
    )
    return JSONResponse(_normalizar_conversaciones(data), status_code=status)


@router.get("/supervision-agente/api/conversaciones/{telefono}/mensajes")
def supervision_mensajes(telefono: str, limit: int = 1000):
    data, status = _proxy(
        "GET",
        f"/control/conversaciones/{telefono}/mensajes",
        params={"limit": min(max(limit, 1), 1000)},
    )
    return JSONResponse(data, status_code=status)


@router.get("/supervision-agente/api/conversaciones/{telefono}/control")
def supervision_control(telefono: str):
    data, status = _proxy("GET", f"/control/conversaciones/{telefono}/control")
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
    payload["usuario"] = payload.get("usuario") or _usuario(request)
    data, status = _proxy("POST", "/control/devolver", json=payload)
    return JSONResponse(data, status_code=status)


@router.post("/supervision-agente/api/mensaje")
async def supervision_mensaje(request: Request):
    payload = await request.json()
    payload["usuario"] = payload.get("usuario") or _usuario(request)
    if not payload.get("texto") and payload.get("mensaje"):
        payload["texto"] = payload["mensaje"]
    data, status = _proxy("POST", "/control/mensaje", json=payload)
    return JSONResponse(data, status_code=status)
