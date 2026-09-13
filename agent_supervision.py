"""Puente seguro ERP -> agente de cobranza.

El navegador nunca recibe la clave del agente. Todas las operaciones contra el
agente se realizan servidor-a-servidor usando las variables de entorno de Render.
"""
import os
import requests
from fastapi import Request
from fastapi.responses import JSONResponse
import main

_TIMEOUT = 20


def _agent_url():
    return (os.getenv("AGENT_SUPERVISION_URL") or "").rstrip("/")


def _agent_headers():
    key = os.getenv("AGENT_SUPERVISION_KEY") or os.getenv("LIQUIDADOR_API_KEY") or ""
    return {"X-Agent-Supervision-Key": key, "Accept": "application/json", "Content-Type": "application/json"}


def _proxy(method, path, *, json=None, params=None):
    base = _agent_url()
    if not base:
        return {"status": "error", "mensaje": "AGENT_SUPERVISION_URL no esta configurada en Render"}, 503
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
        return data, response.status_code
    except requests.RequestException as exc:
        print(f"[SUPERVISION AGENTE] Error de conexión: {exc!r}", flush=True)
        return {"status": "error", "mensaje": f"Agente no disponible: {exc}"}, 503


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
        # Alias de presentación para que el front no dependa del nombre SQL.
        fila["phone"] = fila.get("phone") or fila.get("telefono")
        fila["identification"] = fila.get("identification") or fila.get("identificacion")
        fila["mode_current"] = fila.get("mode_current") or fila.get("modo_actual") or "AGENTE"
        fila["started_at"] = fila.get("started_at") or fila.get("fecha_inicio")
        fila["last_activity"] = fila.get("last_activity") or fila.get("fecha_ultima_actividad")
        fila["human_user"] = fila.get("human_user") or fila.get("usuario_humano")
    return data


@main.app.get("/supervision-agente")
def supervision_agente(request: Request):
    return main.templates.TemplateResponse(
        request=request,
        name="supervision_agente.html",
        context={"request": request, "usuario": _usuario(request), "configurado": bool(_agent_url())},
    )


@main.app.get("/supervision-agente/api/conversaciones")
def supervision_conversaciones(limit: int = 200, buscar: str = ""):
    data, status = _proxy(
        "GET",
        "/control/conversaciones",
        params={"limit": min(max(limit, 1), 200), "buscar": buscar},
    )
    return JSONResponse(_normalizar_conversaciones(data), status_code=status)


@main.app.get("/supervision-agente/api/conversaciones/{telefono}/mensajes")
def supervision_mensajes(telefono: str, limit: int = 1000):
    data, status = _proxy(
        "GET",
        f"/control/conversaciones/{telefono}/mensajes",
        params={"limit": min(max(limit, 1), 1000)},
    )
    return JSONResponse(data, status_code=status)


@main.app.get("/supervision-agente/api/conversaciones/{telefono}/control")
def supervision_control(telefono: str):
    data, status = _proxy("GET", f"/control/conversaciones/{telefono}/control")
    return JSONResponse(data, status_code=status)


@main.app.post("/supervision-agente/api/tomar-control")
async def supervision_tomar_control(request: Request):
    payload = await request.json()
    payload["usuario"] = payload.get("usuario") or _usuario(request)
    data, status = _proxy("POST", "/control/tomar", json=payload)
    return JSONResponse(data, status_code=status)


@main.app.post("/supervision-agente/api/devolver-agente")
async def supervision_devolver_agente(request: Request):
    payload = await request.json()
    payload["usuario"] = payload.get("usuario") or _usuario(request)
    data, status = _proxy("POST", "/control/devolver", json=payload)
    return JSONResponse(data, status_code=status)


@main.app.post("/supervision-agente/api/mensaje")
async def supervision_mensaje(request: Request):
    payload = await request.json()
    payload["usuario"] = payload.get("usuario") or _usuario(request)
    if not payload.get("texto") and payload.get("mensaje"):
        payload["texto"] = payload["mensaje"]
    data, status = _proxy("POST", "/control/mensaje", json=payload)
    return JSONResponse(data, status_code=status)


print("[SUPERVISION AGENTE] Puente ERP -> agente cargado", flush=True)
