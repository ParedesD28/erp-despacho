"""Catálogo canónico y cálculo de la etapa procesal.

Única fuente de verdad del orden de etapas. Todo el sistema (formularios,
servicios, scripts de reconciliación) debe importar de aquí.

Reglas:
- La etapa del proceso es la de mayor rango entre las etapas válidas de sus
  actuaciones. La fecha de la actuación no influye: una actuación con fecha
  posterior pero etapa menor NO hace retroceder el proceso.
- "Terminación del Proceso" es la etapa de mayor rango y siempre prevalece.
- "Auto de Trámite / General" es una etapa válida pero neutra: no avanza ni
  retrocede el proceso.
- Los valores fuera del catálogo se rechazan con EtapaInvalidaError en las rutas
  de entrada; al recalcular sobre datos históricos se ignoran y se reportan.
"""
from __future__ import annotations

import unicodedata
from typing import Iterable, NamedTuple, Optional

ETAPA_TERMINACION = "Terminación del Proceso"
ETAPA_NEUTRA = "Auto de Trámite / General"

ETAPAS_PROCESALES: tuple[str, ...] = (
    "1. Presentación de la demanda",
    "2. Inadmisión",
    "3. Admisión",
    "4. Medidas Cautelares",
    "5. Notificación",
    "6. Excepciones",
    "7. Sentencia",
    "8. Desistimiento tácito",
    ETAPA_TERMINACION,
)

# Etapas que se pueden elegir al registrar una actuación (orden del catálogo
# más la etapa neutra al final).
ETAPAS_SELECCIONABLES: tuple[str, ...] = ETAPAS_PROCESALES + (ETAPA_NEUTRA,)

RANGO_ETAPA: dict[str, int] = {nombre: i for i, nombre in enumerate(ETAPAS_PROCESALES, start=1)}

# Valores históricos que existen en actuaciones.etapa (p. ej. la actuación
# inicial que creaba la radicación). Se aceptan y se normalizan.
_ALIAS_HISTORICOS: dict[str, str] = {
    "inicio": ETAPAS_PROCESALES[0],
}


class EtapaInvalidaError(ValueError):
    """La etapa no pertenece al catálogo."""


def _clave(valor: object) -> str:
    texto = " ".join(str(valor or "").split()).casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


_POR_CLAVE: dict[str, str] = {_clave(n): n for n in ETAPAS_SELECCIONABLES}
_POR_CLAVE.update({_clave(alias): destino for alias, destino in _ALIAS_HISTORICOS.items()})


def normalizar_etapa(valor: object, *, permitir_vacio: bool = True) -> Optional[str]:
    """Devuelve el nombre canónico de la etapa.

    Ignora mayúsculas, tildes y espacios repetidos. Valor vacío -> None si
    `permitir_vacio`, de lo contrario error. Cualquier otro valor lanza
    EtapaInvalidaError con el listado de etapas válidas.
    """
    if valor is None or not str(valor).strip():
        if permitir_vacio:
            return None
        raise EtapaInvalidaError("La etapa es obligatoria. Válidas: " + "; ".join(ETAPAS_SELECCIONABLES))
    canonica = _POR_CLAVE.get(_clave(valor))
    if canonica is None:
        raise EtapaInvalidaError(
            f"Etapa procesal no válida: {str(valor).strip()!r}. Válidas: " + "; ".join(ETAPAS_SELECCIONABLES)
        )
    return canonica


class ResultadoEtapa(NamedTuple):
    etapa: Optional[str]
    invalidos: tuple[str, ...]


def calcular_resultado(valores: Iterable[object]) -> ResultadoEtapa:
    """Etapa de mayor rango entre `valores` e inválidos encontrados (sin duplicar)."""
    mejor: Optional[str] = None
    invalidos: list[str] = []
    for valor in valores:
        try:
            etapa = normalizar_etapa(valor)
        except EtapaInvalidaError:
            texto = str(valor).strip()
            if texto not in invalidos:
                invalidos.append(texto)
            continue
        rango = RANGO_ETAPA.get(etapa or "")
        if rango is None:
            continue
        if mejor is None or rango > RANGO_ETAPA[mejor]:
            mejor = etapa
    return ResultadoEtapa(mejor, tuple(invalidos))


def calcular_etapa(valores: Iterable[object]) -> Optional[str]:
    """Etapa del proceso a partir de las etapas de todas sus actuaciones.

    None si ninguna actuación aporta una etapa del catálogo con rango (vacías,
    neutras o inválidas): en ese caso la etapa actual no debe modificarse.
    """
    return calcular_resultado(valores).etapa
