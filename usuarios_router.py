"""Panel de administración de usuarios (solo Administrador)."""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import catalogos_service
import db
import permisos
import portal_ph_service
import usuarios_service
from psycopg2.extras import RealDictCursor

router = APIRouter(tags=["Admin usuarios"])
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


def _redirect(path: str = "/admin/usuarios", **params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    url = f"{path}?{query}" if query else path
    return RedirectResponse(url=url, status_code=303)


def _actor_id(request: Request):
    return getattr(request.state, "user_id", None)


def _listar_conjuntos_admin() -> list[dict]:
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            return catalogos_service.listar_conjuntos(cur, activos=True)
    finally:
        conn.release()


def _sync_conjuntos_usuario(user_id, perfil_codigo: str, conjunto_ids: Optional[list]) -> None:
    codigo = permisos.normalizar_perfil(perfil_codigo)
    if codigo == permisos.PERFIL_CLIENTE_PH:
        portal_ph_service.reemplazar_conjuntos_usuario(user_id, conjunto_ids or [])
    else:
        portal_ph_service.reemplazar_conjuntos_usuario(user_id, [])


@router.get("/admin/usuarios")
def admin_usuarios(request: Request, editar: str = ""):
    usuarios = usuarios_service.listar_usuarios(incluir_inactivos=True)
    perfiles = usuarios_service.listar_perfiles()
    conjuntos = _listar_conjuntos_admin()
    editando = None
    conjuntos_habilitados: list[int] = []
    if editar:
        for u in usuarios:
            if str(u.get("id")) == str(editar):
                editando = u
                break
        if editando:
            conjuntos_habilitados = portal_ph_service.listar_conjunto_ids_usuario(
                editando["id"]
            )
    return _render(
        "admin_usuarios.html",
        {
            "request": request,
            "usuarios": usuarios,
            "perfiles": perfiles,
            "editando": editando,
            "conjuntos": conjuntos,
            "conjuntos_habilitados": conjuntos_habilitados,
            "perfil_nombres": permisos.PERFIL_NOMBRES,
            "perfil_desc": permisos.PERFIL_DESCRIPCIONES,
            "perfil_cliente_ph": permisos.PERFIL_CLIENTE_PH,
        },
    )


@router.post("/admin/usuarios/crear")
def admin_usuarios_crear(
    request: Request,
    nombre: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    perfil_codigo: str = Form(permisos.PERFIL_CONSULTA),
    activo: str = Form("1"),
    conjunto_id: List[str] = Form(default=[]),
):
    try:
        row = usuarios_service.crear_usuario(
            nombre=nombre,
            email=email,
            password=password,
            perfil_codigo=perfil_codigo,
            activo=activo in ("1", "true", "on", "True"),
        )
        _sync_conjuntos_usuario(row["id"], perfil_codigo, conjunto_id)
        return _redirect(mensaje="Usuario creado")
    except usuarios_service.UsuariosError as exc:
        return _redirect(error=str(exc))
    except Exception:
        return _redirect(error="No fue posible crear el usuario")


@router.post("/admin/usuarios/actualizar")
def admin_usuarios_actualizar(
    request: Request,
    user_id: str = Form(...),
    nombre: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    perfil_codigo: str = Form(permisos.PERFIL_CONSULTA),
    activo: str = Form("0"),
    conjunto_id: List[str] = Form(default=[]),
):
    try:
        usuarios_service.actualizar_usuario(
            user_id,
            nombre=nombre,
            email=email,
            password=password or None,
            perfil_codigo=perfil_codigo,
            activo=activo in ("1", "true", "on", "True"),
            actor_id=_actor_id(request),
        )
        _sync_conjuntos_usuario(user_id, perfil_codigo, conjunto_id)
        return _redirect(mensaje="Usuario actualizado")
    except usuarios_service.UsuariosError as exc:
        return _redirect(editar=user_id, error=str(exc))
    except Exception:
        return _redirect(editar=user_id, error="No fue posible actualizar el usuario")


@router.post("/admin/usuarios/desactivar")
def admin_usuarios_desactivar(request: Request, user_id: str = Form(...)):
    try:
        usuarios_service.desactivar_usuario(user_id, actor_id=_actor_id(request))
        return _redirect(mensaje="Usuario desactivado")
    except usuarios_service.UsuariosError as exc:
        return _redirect(error=str(exc))
    except Exception:
        return _redirect(error="No fue posible desactivar el usuario")


@router.post("/admin/usuarios/activar")
def admin_usuarios_activar(request: Request, user_id: str = Form(...)):
    try:
        usuarios_service.actualizar_usuario(
            user_id,
            activo=True,
            actor_id=_actor_id(request),
        )
        return _redirect(mensaje="Usuario reactivado")
    except usuarios_service.UsuariosError as exc:
        return _redirect(error=str(exc))
    except Exception:
        return _redirect(error="No fue posible reactivar el usuario")
