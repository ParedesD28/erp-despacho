"""Rutas web del asignador de tareas (Mis Tareas + acciones)."""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg2.extras import RealDictCursor

import db
import permisos
import tareas_service as ts

router = APIRouter(tags=["Tareas"])
templates = Jinja2Templates(directory="templates")


def _render(name: str, context: dict, status_code: int = 200):
    try:
        return templates.TemplateResponse(name, context, status_code=status_code)
    except TypeError:
        return templates.TemplateResponse(
            request=context.get("request"),
            name=name,
            context=context,
            status_code=status_code,
        )


def _redirect(path: str = "/tareas", **params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    if not query:
        return RedirectResponse(url=path, status_code=303)
    sep = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{path}{sep}{query}", status_code=303)


def _usuario_id(request: Request) -> Optional[int]:
    raw = getattr(request.state, "user_id", None)
    try:
        return int(raw) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _perfil(request: Request) -> str:
    return str(getattr(request.state, "perfil_codigo", "") or "")


def _es_coordinador(request: Request) -> bool:
    return _perfil(request) in {permisos.PERFIL_ADMIN, permisos.PERFIL_ABOGADO}


def _destino(next_url: str, fallback: str = "/tareas") -> str:
    destino = (next_url or "").strip()
    if destino.startswith("/") and not destino.startswith("//"):
        return destino
    return fallback


@router.get("/tareas")
def mis_tareas(request: Request, asignado: str = ""):
    uid = _usuario_id(request)
    coordinador = _es_coordinador(request)
    ver_id = uid
    if coordinador and str(asignado).strip().isdigit():
        ver_id = int(asignado)
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            grupos = ts.listar_mis_tareas(cur, ver_id, incluir_cerradas=True)
            usuarios = ts.listar_usuarios_asignables(cur)
            carga = ts.obtener_carga_trabajo(cur) if coordinador else []
            nombre_visto = None
            if ver_id and ver_id != uid:
                for u in usuarios:
                    if int(u["id"]) == ver_id:
                        nombre_visto = u.get("nombre")
                        break
    finally:
        conn.release()
    return _render(
        "tareas.html",
        {
            "request": request,
            "grupos": grupos,
            "usuarios": usuarios,
            "carga": carga,
            "coordinador": coordinador,
            "usuario_id": uid,
            "ver_id": ver_id,
            "nombre_visto": nombre_visto,
            "prioridades": ts.PRIORIDADES,
            "estados": ts.ESTADOS,
        },
    )


@router.post("/tareas/nueva")
def nueva_tarea(
    request: Request,
    titulo: str = Form(...),
    asignado_a: str = Form(...),
    radicado_interno: str = Form(""),
    descripcion: str = Form(""),
    prioridad: str = Form("Media"),
    fecha_vencimiento: str = Form(""),
    recordatorio_dias: str = Form(""),
    notas_internas: str = Form(""),
    next: str = Form(""),
):
    uid = _usuario_id(request)
    destino = _destino(next)
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                ts.crear_tarea_manual(
                    cur,
                    titulo=titulo,
                    asignado_a=asignado_a,
                    creado_por=uid,
                    radicado_interno=radicado_interno,
                    descripcion=descripcion,
                    prioridad=prioridad,
                    fecha_vencimiento=fecha_vencimiento or None,
                    recordatorio_dias=recordatorio_dias or None,
                    notas_internas=notas_internas or None,
                )
        return _redirect(destino, mensaje="Tarea+creada")
    except ts.TareasError as exc:
        return _redirect(destino, error=str(exc).replace(" ", "+"))
    except Exception:
        return _redirect(destino, error="No+fue+posible+crear+la+tarea")
    finally:
        conn.release()


@router.post("/tareas/{tarea_id}/completar")
def completar(request: Request, tarea_id: int, next: str = Form("")):
    destino = _destino(next)
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                ts.completar_tarea(cur, tarea_id, usuario_id=_usuario_id(request))
        return _redirect(destino, mensaje="Tarea+completada")
    except ts.TareasError as exc:
        return _redirect(destino, error=str(exc).replace(" ", "+"))
    except Exception:
        return _redirect(destino, error="No+fue+posible+completar+la+tarea")
    finally:
        conn.release()


@router.post("/tareas/{tarea_id}/estado")
def cambiar_estado_ruta(
    request: Request,
    tarea_id: int,
    estado: str = Form(...),
    next: str = Form(""),
):
    destino = _destino(next)
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                ts.cambiar_estado(
                    cur, tarea_id, estado=estado, usuario_id=_usuario_id(request)
                )
        return _redirect(destino, mensaje="Estado+actualizado")
    except ts.TareasError as exc:
        return _redirect(destino, error=str(exc).replace(" ", "+"))
    except Exception:
        return _redirect(destino, error="No+fue+posible+cambiar+el+estado")
    finally:
        conn.release()


@router.post("/tareas/{tarea_id}/reasignar")
def reasignar(
    request: Request,
    tarea_id: int,
    asignado_a: str = Form(...),
    next: str = Form(""),
):
    destino = _destino(next)
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                ts.reasignar_tarea(
                    cur,
                    tarea_id,
                    asignado_a=asignado_a,
                    usuario_id=_usuario_id(request),
                )
        return _redirect(destino, mensaje="Tarea+reasignada")
    except ts.TareasError as exc:
        return _redirect(destino, error=str(exc).replace(" ", "+"))
    except Exception:
        return _redirect(destino, error="No+fue+posible+reasignar+la+tarea")
    finally:
        conn.release()


@router.post("/tareas/{tarea_id}/comentario")
def comentar(
    request: Request,
    tarea_id: int,
    comentario: str = Form(...),
    next: str = Form(""),
):
    destino = _destino(next)
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                ts.agregar_comentario(
                    cur,
                    tarea_id,
                    comentario=comentario,
                    usuario_id=_usuario_id(request),
                )
        return _redirect(destino, mensaje="Comentario+guardado")
    except ts.TareasError as exc:
        return _redirect(destino, error=str(exc).replace(" ", "+"))
    except Exception:
        return _redirect(destino, error="No+fue+posible+guardar+el+comentario")
    finally:
        conn.release()
