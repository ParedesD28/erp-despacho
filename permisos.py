"""Perfiles humanos y permisos del ERP (RBAC).

El bot/API key NO usa estos perfiles: sigue con LIQUIDADOR_API_KEY.
Los permisos viven en código; en BD solo se asigna el perfil al usuario (`abogados`).
"""
from __future__ import annotations

from typing import Iterable, Optional

# Códigos canónicos (columna perfiles.codigo)
PERFIL_ADMIN = "ADMIN"
PERFIL_ABOGADO = "ABOGADO"
PERFIL_AUXILIAR = "AUXILIAR_COBRO"
PERFIL_CONSULTA = "CONSULTA"

PERFILES_HUMANOS = (
    PERFIL_ADMIN,
    PERFIL_ABOGADO,
    PERFIL_AUXILIAR,
    PERFIL_CONSULTA,
)

PERFIL_NOMBRES = {
    PERFIL_ADMIN: "Administrador",
    PERFIL_ABOGADO: "Abogado",
    PERFIL_AUXILIAR: "Auxiliar de cobro",
    PERFIL_CONSULTA: "Solo consulta",
}

PERFIL_DESCRIPCIONES = {
    PERFIL_ADMIN: "Todo el ERP: usuarios, menús, configuración y acciones fuertes.",
    PERFIL_ABOGADO: "Expedientes, cartera, cobro completo, acuerdos, cartas y SMS.",
    PERFIL_AUXILIAR: "Seguimiento diario de cobro (CRM, SMS, cartas, vencimientos); sin borrar lo crítico ni administrar usuarios.",
    PERFIL_CONSULTA: "Ver cartera e informes; no editar ni enviar SMS/cartas.",
}

# Permisos atómicos
NAV_DASHBOARD = "nav.dashboard"
NAV_EXPEDIENTES = "nav.expedientes"
NAV_COBRO_CRM = "nav.cobro.crm"
NAV_COBRO_ACUERDOS = "nav.cobro.acuerdos"
NAV_COBRO_VENCIMIENTOS = "nav.cobro.vencimientos"
NAV_COBRO_SUPERVISION = "nav.cobro.supervision"
NAV_COBRO_SMS = "nav.cobro.sms"
NAV_COBRO_CARTAS = "nav.cobro.cartas"
NAV_INFORMES = "nav.informes"
NAV_CONTACTOS = "nav.contactos"
NAV_CONJUNTOS = "nav.conjuntos"
NAV_PROCESOS = "nav.procesos"
NAV_ADMIN_USUARIOS = "nav.admin_usuarios"

ACCION_EDITAR = "accion.editar"
ACCION_BORRAR = "accion.borrar"
ACCION_SMS = "accion.sms"
ACCION_CARTAS = "accion.cartas"
ACCION_SUPERVISION = "accion.supervision"

TODOS_LOS_PERMISOS = frozenset(
    {
        NAV_DASHBOARD,
        NAV_EXPEDIENTES,
        NAV_COBRO_CRM,
        NAV_COBRO_ACUERDOS,
        NAV_COBRO_VENCIMIENTOS,
        NAV_COBRO_SUPERVISION,
        NAV_COBRO_SMS,
        NAV_COBRO_CARTAS,
        NAV_INFORMES,
        NAV_CONTACTOS,
        NAV_CONJUNTOS,
        NAV_PROCESOS,
        NAV_ADMIN_USUARIOS,
        ACCION_EDITAR,
        ACCION_BORRAR,
        ACCION_SMS,
        ACCION_CARTAS,
        ACCION_SUPERVISION,
    }
)

_COBRO_BASE = frozenset(
    {
        NAV_COBRO_CRM,
        NAV_COBRO_ACUERDOS,
        NAV_COBRO_VENCIMIENTOS,
        NAV_COBRO_SMS,
        NAV_COBRO_CARTAS,
    }
)

PERMISOS_POR_PERFIL: dict[str, frozenset[str]] = {
    PERFIL_ADMIN: TODOS_LOS_PERMISOS,
    PERFIL_ABOGADO: frozenset(
        TODOS_LOS_PERMISOS - {NAV_ADMIN_USUARIOS}
    ),
    PERFIL_AUXILIAR: frozenset(
        {
            NAV_DASHBOARD,
            NAV_EXPEDIENTES,
            *_COBRO_BASE,
            NAV_INFORMES,
            NAV_CONTACTOS,
            NAV_CONJUNTOS,
            ACCION_EDITAR,
            ACCION_SMS,
            ACCION_CARTAS,
        }
    ),
    PERFIL_CONSULTA: frozenset(
        {
            NAV_DASHBOARD,
            NAV_EXPEDIENTES,
            NAV_COBRO_CRM,
            NAV_COBRO_ACUERDOS,
            NAV_COBRO_VENCIMIENTOS,
            NAV_INFORMES,
            NAV_CONTACTOS,
            NAV_CONJUNTOS,
        }
    ),
}


def normalizar_perfil(codigo: Optional[str]) -> str:
    """Devuelve un código de perfil válido; por defecto ADMIN (rollout seguro)."""
    valor = str(codigo or "").strip().upper()
    if valor in PERMISOS_POR_PERFIL:
        return valor
    return PERFIL_ADMIN


def permisos_de_perfil(codigo: Optional[str]) -> frozenset[str]:
    return PERMISOS_POR_PERFIL[normalizar_perfil(codigo)]


def tiene_permiso(permisos: Optional[Iterable[str]], permiso: str) -> bool:
    if not permiso:
        return False
    bag = frozenset(permisos or ())
    return permiso in bag


def puede_ver_cobro(permisos: Optional[Iterable[str]]) -> bool:
    bag = frozenset(permisos or ())
    return bool(
        bag
        & {
            NAV_COBRO_CRM,
            NAV_COBRO_ACUERDOS,
            NAV_COBRO_VENCIMIENTOS,
            NAV_COBRO_SUPERVISION,
            NAV_COBRO_SMS,
            NAV_COBRO_CARTAS,
        }
    )


# Prefijos sensibles → permiso requerido (primer match gana).
# Rutas públicas (/api/bot/, /sms/api/, /login, /static/) se excluyen antes.
_ROUTE_RULES: tuple[tuple[str, str], ...] = (
    ("/admin/usuarios", NAV_ADMIN_USUARIOS),
    ("/supervision-agente", NAV_COBRO_SUPERVISION),
    ("/cartas-cobro", NAV_COBRO_CARTAS),
    ("/sms", NAV_COBRO_SMS),
    ("/crm", NAV_COBRO_CRM),
    ("/acuerdos", NAV_COBRO_ACUERDOS),
    ("/vencimientos", NAV_COBRO_VENCIMIENTOS),
    ("/expedientes", NAV_EXPEDIENTES),
    ("/expediente", NAV_EXPEDIENTES),
    ("/informes", NAV_INFORMES),
    ("/contactos", NAV_CONTACTOS),
    ("/conjuntos", NAV_CONJUNTOS),
    ("/procesos", NAV_PROCESOS),
    ("/dashboard", NAV_DASHBOARD),
)

# Acciones fuertes por método+ruta exacta o prefijo.
_WRITE_RULES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/acuerdos/eliminar", ACCION_BORRAR),
    ("POST", "/actuacion/eliminar", ACCION_BORRAR),
    ("POST", "/sms/cola/limpiar", ACCION_BORRAR),
    ("POST", "/sms/generar-cola", ACCION_SMS),
    ("POST", "/sms/wizard/confirmar-cola", ACCION_SMS),
    ("POST", "/cartas-cobro/generar", ACCION_CARTAS),
    ("POST", "/cartas-cobro/plantillas", ACCION_CARTAS),
)


def permiso_requerido_para_ruta(method: str, path: str) -> Optional[str]:
    """Devuelve el permiso necesario o None si la ruta no está restringida por perfil."""
    metodo = (method or "GET").upper()
    ruta = path or "/"

    for m, prefix, perm in _WRITE_RULES:
        if metodo == m and (ruta == prefix or ruta.startswith(prefix + "/")):
            return perm

    # Consulta: bloquear mutaciones genéricas salvo logout/login.
    if metodo in ("POST", "PUT", "PATCH", "DELETE"):
        if ruta in ("/login", "/logout"):
            return None
        # Las mutaciones exigen al menos ACCION_EDITAR; reglas más fuertes ya cubiertas.
        for prefix, perm in _ROUTE_RULES:
            if ruta == prefix or ruta.startswith(prefix + "/"):
                if perm in (NAV_COBRO_SMS, NAV_COBRO_CARTAS, NAV_COBRO_SUPERVISION, NAV_ADMIN_USUARIOS):
                    return perm
                return ACCION_EDITAR
        return ACCION_EDITAR

    for prefix, perm in _ROUTE_RULES:
        if ruta == prefix or ruta.startswith(prefix + "/"):
            return perm
    return None


def denegar_acceso(permisos: Optional[Iterable[str]], method: str, path: str) -> bool:
    """True si el usuario NO puede acceder a la ruta."""
    requerido = permiso_requerido_para_ruta(method, path)
    if requerido is None:
        return False
    return not tiene_permiso(permisos, requerido)
