"""Fuente única de verdad del estado de un vencimiento.

`vencimientos.estado` es la única columna escribible. `completado` es una
columna derivada (GENERATED ALWAYS, ver migración
20261026_0236_vencimientos_estado_fuente_unica.sql) y el código nunca la escribe.
"""
from __future__ import annotations

ESTADO_PENDIENTE = "Pendiente"
ESTADO_COMPLETADO = "Completado"
ESTADOS_VALIDOS = (ESTADO_PENDIENTE, ESTADO_COMPLETADO)


def normalizar_estado(estado) -> str:
    """Devuelve el estado canónico; cualquier valor desconocido o vacío es Pendiente."""
    if str(estado or "").strip().upper() == ESTADO_COMPLETADO.upper():
        return ESTADO_COMPLETADO
    return ESTADO_PENDIENTE


def esta_completado(estado) -> bool:
    """Misma derivación que la columna generada `vencimientos.completado`."""
    return normalizar_estado(estado) == ESTADO_COMPLETADO


def sql_abierto(alias: str = "") -> str:
    """Predicado SQL: vencimiento aún no completado.

    Se basa solo en `estado`, por lo que funciona antes y después de convertir
    `completado` en columna generada.
    """
    prefijo = f"{alias}." if alias else ""
    return f"UPPER(BTRIM(COALESCE({prefijo}estado,'{ESTADO_PENDIENTE}'))) <> 'COMPLETADO'"
