"""Catálogo de plazos legales y mapeo actuación → plazo.

Estilo de `etapas_procesales.py`: única fuente de `PLAZOS_LEGALES` y
`mapear_actuacion_a_plazo`. `etapa_asociada` siempre coincide con
`ETAPAS_PROCESALES` (o la etapa neutra).

El responsable alinea el reloj Despacho vs Abogado:
- Parte   = espera del abogado / la firma
- Juzgado = espera del despacho judicial
"""
from __future__ import annotations

import unicodedata
from typing import NamedTuple, Optional

import etapas_procesales as ep

RESPONSABLE_PARTE = "Parte"
RESPONSABLE_JUZGADO = "Juzgado"
RESPONSABLE_TERCERO = "Tercero"
RESPONSABLE_MIXTO = "Mixto"
RESPONSABLE_JUSTIFICADA = "Justificada"
RESPONSABLES = (
    RESPONSABLE_PARTE,
    RESPONSABLE_JUZGADO,
    RESPONSABLE_TERCERO,
    RESPONSABLE_MIXTO,
    RESPONSABLE_JUSTIFICADA,
)

UNIDAD_DIAS_HABILES = "dias_habiles"
UNIDAD_DIAS_CALENDARIO = "dias_calendario"
UNIDAD_MESES = "meses"

ETIQUETA_RELOJ = {
    RESPONSABLE_PARTE: "Abogado",
    RESPONSABLE_JUZGADO: "Despacho",
    RESPONSABLE_TERCERO: "Tercero",
    RESPONSABLE_MIXTO: "Mixto",
    RESPONSABLE_JUSTIFICADA: "Justificada",
}


class PlazoLegal(NamedTuple):
    codigo: str
    nombre: str
    etapa_asociada: str
    responsable: str
    termino_dias: Optional[int]
    termino_unidad: str
    genera_vencimiento: bool
    keywords: tuple[str, ...]


def _e(idx: int) -> str:
    return ep.ETAPAS_PROCESALES[idx]


# Orden: reglas más específicas primero (inadmisión antes que admisión).
PLAZOS_LEGALES: tuple[PlazoLegal, ...] = (
    PlazoLegal(
        "INADMISION_SUBSANAR",
        "Subsanar inadmisión (CGP art. 90)",
        _e(1),
        RESPONSABLE_PARTE,
        5,
        UNIDAD_DIAS_HABILES,
        True,
        ("inadmision", "inadmite", "inadmitio", "subsanar", "subsanacion"),
    ),
    PlazoLegal(
        "EXCEPCIONES_TRASLADO",
        "Traslado de excepciones / excepciones de mérito (CGP art. 442)",
        _e(5),
        RESPONSABLE_PARTE,
        10,
        UNIDAD_DIAS_HABILES,
        True,
        ("excepciones de merito", "traslado de excepciones", "excepciones"),
    ),
    PlazoLegal(
        "REPOSICION",
        "Recurso de reposición (CGP art. 318)",
        ep.ETAPA_NEUTRA,
        RESPONSABLE_PARTE,
        3,
        UNIDAD_DIAS_HABILES,
        True,
        ("reposicion", "reposición"),
    ),
    PlazoLegal(
        "APELACION",
        "Recurso de apelación (CGP art. 322)",
        ep.ETAPA_NEUTRA,
        RESPONSABLE_PARTE,
        3,
        UNIDAD_DIAS_HABILES,
        True,
        ("apelacion", "apelación"),
    ),
    PlazoLegal(
        "LIQUIDACION_CREDITO",
        "Liquidación del crédito / costas (CGP art. 446)",
        _e(6),
        RESPONSABLE_PARTE,
        10,
        UNIDAD_DIAS_HABILES,
        True,
        ("liquidacion del credito", "liquidacion de costas", "liquidacion"),
    ),
    PlazoLegal(
        "DESISTIMIENTO_TACITO",
        "Desistimiento tácito (CGP art. 317)",
        _e(7),
        RESPONSABLE_PARTE,
        30,
        UNIDAD_DIAS_HABILES,
        True,
        ("desistimiento tacito", "desistimiento tácito"),
    ),
    PlazoLegal(
        "NOTIFICACION",
        "Notificación del auto admisorio / mandamiento",
        _e(4),
        RESPONSABLE_PARTE,
        30,
        UNIDAD_DIAS_HABILES,
        True,
        ("notificacion", "notificación", "notificar", "enterado", "aviso"),
    ),
    PlazoLegal(
        "MEDIDAS_CAUTELARES",
        "Gestión de medidas cautelares (caución, registro, oficio)",
        _e(3),
        RESPONSABLE_PARTE,
        15,
        UNIDAD_DIAS_HABILES,
        True,
        (
            "medida cautelar",
            "medidas cautelares",
            "embargo",
            "secuestro",
            "inscripcion",
            "inscripción",
            "caucion",
            "caución",
        ),
    ),
    PlazoLegal(
        "ADMISION",
        "Auto de admisión / mandamiento de pago",
        _e(2),
        RESPONSABLE_JUZGADO,
        None,
        UNIDAD_DIAS_HABILES,
        False,
        ("admision", "admisión", "admite", "admitio", "mandamiento de pago", "mandamiento"),
    ),
    PlazoLegal(
        "SENTENCIA",
        "Sentencia / fallo",
        _e(6),
        RESPONSABLE_JUZGADO,
        None,
        UNIDAD_DIAS_HABILES,
        False,
        ("sentencia", "fallo", "falla"),
    ),
    PlazoLegal(
        "TERMINACION",
        "Terminación del proceso",
        ep.ETAPA_TERMINACION,
        RESPONSABLE_JUZGADO,
        None,
        UNIDAD_DIAS_HABILES,
        False,
        ("terminacion", "terminación del proceso", "archivo del proceso", "se termina"),
    ),
    PlazoLegal(
        "PRESENTACION",
        "Presentación / radicación de la demanda",
        _e(0),
        RESPONSABLE_JUZGADO,
        None,
        UNIDAD_DIAS_HABILES,
        False,
        ("presentacion de la demanda", "radicacion", "radicación", "demanda presentada", "se presenta demanda"),
    ),
    PlazoLegal(
        "AUTO_TRAMITE",
        "Auto de trámite / general",
        ep.ETAPA_NEUTRA,
        RESPONSABLE_JUZGADO,
        None,
        UNIDAD_DIAS_HABILES,
        False,
        ("auto de tramite", "auto de trámite", "auto general", "constancia"),
    ),
)

PLAZO_TERMINO_RAMA = PlazoLegal(
    "TERMINO_RAMA",
    "Término informado por la Rama Judicial",
    ep.ETAPA_NEUTRA,
    RESPONSABLE_PARTE,
    None,
    UNIDAD_DIAS_HABILES,
    True,
    (),
)

_POR_CODIGO: dict[str, PlazoLegal] = {p.codigo: p for p in PLAZOS_LEGALES}
_POR_ETAPA: dict[str, PlazoLegal] = {}
for _p in PLAZOS_LEGALES:
    _POR_ETAPA.setdefault(_p.etapa_asociada, _p)


def _clave(valor: object) -> str:
    texto = " ".join(str(valor or "").split()).casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def etiqueta_reloj(responsable: object) -> str:
    """Parte → Abogado (firma); Juzgado → Despacho."""
    key = str(responsable or "").strip()
    return ETIQUETA_RELOJ.get(key, key or "Abogado")


def plazo_por_codigo(codigo: object) -> Optional[PlazoLegal]:
    if not codigo:
        return None
    return _POR_CODIGO.get(str(codigo).strip().upper())


def _match_keywords(texto: str, keywords: tuple[str, ...]) -> bool:
    if not texto or not keywords:
        return False
    return any(_clave(k) in texto for k in keywords)


def mapear_actuacion_a_plazo(
    tipo: object = None,
    descripcion: object = None,
    *,
    etapa: object = None,
    tiene_termino: bool = False,
) -> Optional[PlazoLegal]:
    """Best-effort: keywords en tipo+descripción, luego etapa, luego término Rama.

    No lanza. None si no hay indicios. Si hay duda de dueño, Parte (la firma).
    """
    texto = _clave(f"{tipo or ''} {descripcion or ''}")
    for plazo in PLAZOS_LEGALES:
        if _match_keywords(texto, plazo.keywords):
            if plazo.codigo == "AUTO_TRAMITE" and tiene_termino:
                return plazo._replace(responsable=RESPONSABLE_PARTE, genera_vencimiento=True)
            return plazo

    etapa_n = None
    try:
        etapa_n = ep.normalizar_etapa(etapa)
    except ep.EtapaInvalidaError:
        etapa_n = None
    if etapa_n and etapa_n in _POR_ETAPA:
        plazo = _POR_ETAPA[etapa_n]
        if etapa_n == ep.ETAPA_NEUTRA and tiene_termino:
            return PLAZO_TERMINO_RAMA
        return plazo

    if tiene_termino:
        return PLAZO_TERMINO_RAMA
    return None
