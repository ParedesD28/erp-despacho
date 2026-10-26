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
PERFIL_CLIENTE_PH = "CLIENTE_PH"

# Perfil interno (nunca asignable ni guardado en BD): usuario sin perfil válido.
# Sin permisos: la sesión se rechaza (fail-closed).
PERFIL_SIN_ACCESO = "SIN_ACCESO"

PERFILES_HUMANOS = (
    PERFIL_ADMIN,
    PERFIL_ABOGADO,
    PERFIL_AUXILIAR,
    PERFIL_CONSULTA,
    PERFIL_CLIENTE_PH,
)

PERFIL_NOMBRES = {
    PERFIL_ADMIN: "Administrador",
    PERFIL_ABOGADO: "Abogado",
    PERFIL_AUXILIAR: "Auxiliar de cobro",
    PERFIL_CONSULTA: "Solo consulta",
    PERFIL_CLIENTE_PH: "Cliente PH",
    PERFIL_SIN_ACCESO: "Sin perfil (bloqueado)",
}

PERFIL_DESCRIPCIONES = {
    PERFIL_ADMIN: "Todo el ERP: usuarios, menús, configuración y acciones fuertes.",
    PERFIL_ABOGADO: "Expedientes, cartera, cobro completo, acuerdos, cartas y SMS.",
    PERFIL_AUXILIAR: "Seguimiento diario de cobro (CRM, SMS, cartas, vencimientos); sin borrar lo crítico ni administrar usuarios.",
    PERFIL_CONSULTA: "Ver cartera e informes; no editar ni enviar SMS/cartas.",
    PERFIL_CLIENTE_PH: "Portal PH: solo unidades (torre/apto + demandados) de los conjuntos que el Admin le habilite.",
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
NAV_PORTAL_PH = "nav.portal_ph"

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
    PERFIL_CLIENTE_PH: frozenset({NAV_PORTAL_PH}),
}


def perfil_valido(codigo: Optional[str]) -> bool:
    return str(codigo or "").strip().upper() in PERMISOS_POR_PERFIL


def normalizar_perfil(codigo: Optional[str]) -> str:
    """Devuelve un código de perfil válido o PERFIL_SIN_ACCESO (fail-closed).

    Un perfil ausente o desconocido NUNCA se eleva a ADMIN.
    """
    valor = str(codigo or "").strip().upper()
    if valor in PERMISOS_POR_PERFIL:
        return valor
    return PERFIL_SIN_ACCESO


def permisos_de_perfil(codigo: Optional[str]) -> frozenset[str]:
    return PERMISOS_POR_PERFIL.get(normalizar_perfil(codigo), frozenset())


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


# Rutas públicas (sin sesión). Cada una se protege con su propio mecanismo:
# login/landing/health son abiertas; /static no sirve PDFs sensibles;
# /api/bot/ y /api/recaudos/bot/ usan llave M2M (m2m_keys); /sms/api/ usa token;
# /api/v1/cron/ usa X-Cron-Secret. tests/test_m2m_rutas_publicas.py audita que toda
# ruta registrada bajo estos prefijos se autentique por sí misma.
PUBLIC_EXACT_PATHS: frozenset[str] = frozenset({"/", "/login", "/health", "/landing"})
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/api/bot/",
    "/api/recaudos/bot/",
    "/sms/api/",
    "/api/v1/cron/",
)
PUBLIC_EXCLUDED_PREFIXES: tuple[str, ...] = ("/static/pdfs/",)

# Autenticadas pero válidas para cualquier perfil activo (cerrar sesión).
AUTHENTICATED_ANY_PATHS: frozenset[str] = frozenset({"/logout"})

# Sentinela: ruta sin clasificar => ningún perfil la posee (default-deny).
PERMISO_DENEGADO = "__ruta_sin_clasificar__"


def es_ruta_publica(path: str) -> bool:
    ruta = path or "/"
    if ruta in PUBLIC_EXACT_PATHS:
        return True
    if any(ruta.startswith(p) for p in PUBLIC_EXCLUDED_PREFIXES):
        return False
    return any(ruta.startswith(p) for p in PUBLIC_PREFIXES)


# Prefijos autenticados → permiso requerido (primer match gana).
# Toda ruta que no sea pública y no coincida con un prefijo se DENIEGA.
_ROUTE_RULES: tuple[tuple[str, str], ...] = (
    ("/admin/usuarios", NAV_ADMIN_USUARIOS),
    ("/portal-ph", NAV_PORTAL_PH),
    ("/supervision-agente", NAV_COBRO_SUPERVISION),
    ("/cartas-cobro", NAV_COBRO_CARTAS),
    ("/sms", NAV_COBRO_SMS),
    ("/crm", NAV_COBRO_CRM),
    ("/acuerdos", NAV_COBRO_ACUERDOS),
    ("/vencimientos", NAV_COBRO_VENCIMIENTOS),
    ("/expedientes", NAV_EXPEDIENTES),
    ("/expediente", NAV_EXPEDIENTES),
    ("/expediente-docs", NAV_EXPEDIENTES),
    ("/actuacion", NAV_EXPEDIENTES),
    ("/crear_expediente_completo", NAV_EXPEDIENTES),
    ("/crear_proceso_cascada", NAV_EXPEDIENTES),
    ("/alertas-seguimiento", NAV_EXPEDIENTES),
    ("/pdfs", NAV_EXPEDIENTES),
    ("/informes", NAV_INFORMES),
    ("/herramientas", NAV_INFORMES),
    ("/phasecob", NAV_INFORMES),
    # /api/recaudos/bot/ es M2M público (llave propia) y no pasa por estas reglas.
    ("/liquidador", NAV_INFORMES),
    ("/descargar-excel", NAV_INFORMES),
    ("/api/recaudos", NAV_INFORMES),
    ("/contactos", NAV_CONTACTOS),
    ("/conjuntos", NAV_CONJUNTOS),
    ("/procesos", NAV_PROCESOS),
    ("/dashboard", NAV_DASHBOARD),
    ("/__interno", NAV_ADMIN_USUARIOS),
    # Documentación interactiva de la API: solo administradores.
    ("/docs", NAV_ADMIN_USUARIOS),
    ("/redoc", NAV_ADMIN_USUARIOS),
    ("/openapi.json", NAV_ADMIN_USUARIOS),
)


def _coincide_prefijo(ruta: str, prefix: str) -> bool:
    return ruta == prefix or ruta.startswith(prefix + "/")


def _regla_para_ruta(ruta: str) -> Optional[tuple[str, str]]:
    for prefix, perm in _ROUTE_RULES:
        if _coincide_prefijo(ruta, prefix):
            return prefix, perm
    return None


def home_path_para_perfil(codigo: Optional[str]) -> str:
    """Destino post-login / al denegar acceso según perfil."""
    if normalizar_perfil(codigo) == PERFIL_CLIENTE_PH:
        return "/portal-ph"
    return "/dashboard"

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
    """Permiso necesario para la ruta.

    - None: ruta pública o permitida a cualquier sesión válida (/logout).
    - PERMISO_DENEGADO: ruta sin clasificar (default-deny; nadie la posee).
    - Cualquier otro valor: permiso que el perfil debe tener.
    """
    metodo = (method or "GET").upper()
    ruta = path or "/"

    if es_ruta_publica(ruta) or ruta in AUTHENTICATED_ANY_PATHS:
        return None

    for m, prefix, perm in _WRITE_RULES:
        if metodo == m and _coincide_prefijo(ruta, prefix):
            return perm

    regla = _regla_para_ruta(ruta)
    if regla is None:
        return PERMISO_DENEGADO
    _, perm = regla

    # Las mutaciones exigen al menos ACCION_EDITAR salvo permisos más fuertes ya propios.
    if metodo in ("POST", "PUT", "PATCH", "DELETE"):
        if perm in (NAV_COBRO_SMS, NAV_COBRO_CARTAS, NAV_COBRO_SUPERVISION, NAV_ADMIN_USUARIOS):
            return perm
        return ACCION_EDITAR
    return perm


def denegar_acceso(permisos: Optional[Iterable[str]], method: str, path: str) -> bool:
    """True si el usuario NO puede acceder a la ruta."""
    requerido = permiso_requerido_para_ruta(method, path)
    if requerido is None:
        return False
    if requerido == PERMISO_DENEGADO:
        return True
    return not tiene_permiso(permisos, requerido)
