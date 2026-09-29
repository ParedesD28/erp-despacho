"""Servicio de Certificados de Deuda (.docx vía docxtpl).

Usa la plantilla oficial del despacho (`static/plantillas/CERTIFICADO_DE_DEUDA.docx`),
convertida a placeholders Jinja conservando tipografía/tablas Century Gothic.

Transforma `capital_limpio_a_demandar` (Bolsa Global), valida maestros en Neon
y genera Word en memoria. I/O bloqueante: el router debe usar `asyncio.to_thread`.
"""
from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from certificados_deuda_repository import describir_busqueda, resolver_datos_certificado

PLANTILLA_PATH = (
    Path(__file__).resolve().parent / "static" / "plantillas" / "CERTIFICADO_DE_DEUDA.docx"
)

# Placeholders de la plantilla oficial (docxtpl). Ver docs/certificados-deuda-word.md.
PLANTILLA_PLACEHOLDERS: tuple[str, ...] = (
    "copropiedad_nombre",
    "copropiedad_nit",
    "ciudad",
    "ciudad_mayus",
    "torre_apto",
    "conjunto_nombre",
    "titular_nombre",
    "titular_cedula",
    "dia_emision",
    "mes_emision",
    "anio_emision",
    "representante_nombre",
    "representante_cedula",
    "filas",  # mes, anio, cuotas_ordinarias, cuotas_extraordinarias, vencimiento, saldo
    "total_saldo",
)

# Contexto extra compatible (plantilla actual no los usa; no rompe docxtpl).
PLANTILLA_PLACEHOLDERS_MULTI: tuple[str, ...] = (
    "propietarios",
    "propietarios_texto",
    "propietarios_nombres",
    "hay_varios_propietarios",
)

_MESES_ES = (
    "",
    "ENERO",
    "FEBRERO",
    "MARZO",
    "ABRIL",
    "MAYO",
    "JUNIO",
    "JULIO",
    "AGOSTO",
    "SEPTIEMBRE",
    "OCTUBRE",
    "NOVIEMBRE",
    "DICIEMBRE",
)

_MESES_ABBR = (
    "",
    "ene",
    "feb",
    "mar",
    "abr",
    "may",
    "jun",
    "jul",
    "ago",
    "sep",
    "oct",
    "nov",
    "dic",
)

_MESES_EMISION = (
    "",
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


class CertificadoNoEncontradoError(LookupError):
    """Inmueble / cuenta no encontrada en Neon."""

    def __init__(self, message: str, *, criterios: str = ""):
        self.criterios = criterios
        super().__init__(message)


class CertificadoDatosFaltantesError(ValueError):
    """Faltan campos críticos para emitir el certificado."""

    def __init__(self, faltantes: list[str], *, fuente: str = "neon"):
        self.faltantes = list(faltantes)
        self.fuente = fuente
        if fuente == "pdf":
            detalle = "; ".join(self.faltantes)
            super().__init__(
                "Faltan datos críticos para emitir con datos del PDF "
                f"(sin inventar NIT/cédula): {detalle}. "
                "Indique copropiedad_nit y titular_cedula en el body, "
                "o cargue el maestro en Neon."
            )
        else:
            detalle = "; ".join(self.faltantes)
            super().__init__(f"Faltan datos críticos en Neon: {detalle}")


def datos_certificado_desde_pdf(
    *,
    conjunto: str = "",
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
    titular: str = "",
    titular_cedula: str = "",
    copropiedad_nombre: str = "",
    copropiedad_nit: str = "",
    ciudad: str = "",
) -> dict[str, Any]:
    """
    Fallback documentado: arma el contexto con datos del PDF cuando Neon
    no tiene la unidad. No inventa NIT ni cédula.
    """
    from certificados_deuda_repository import clave_canonica_unidad

    torre = clave_canonica_unidad(
        torre_apto, bloque=bloque, apartamento=apartamento
    )
    if not torre:
        torre = (torre_apto or "").strip()
    if not torre and (bloque or "").strip() and (apartamento or "").strip():
        torre = f"{str(bloque).strip()}-{str(apartamento).strip()}"
    nombre_conjunto = (conjunto or "").strip() or None
    nombre_copropiedad = (copropiedad_nombre or "").strip() or nombre_conjunto
    return {
        "inmueble_id": None,
        "torre_apto": torre or None,
        "conjunto_id": None,
        "conjunto_nombre": nombre_conjunto,
        "copropiedad_nombre": nombre_copropiedad,
        "copropiedad_nit": (copropiedad_nit or "").strip() or None,
        "ciudad": (ciudad or "").strip() or None,
        "titular_nombre": (titular or "").strip() or None,
        "titular_cedula": (titular_cedula or "").strip() or None,
        "fuente": "pdf",
    }


def validar_datos_criticos(datos: dict[str, Any]) -> None:
    """
    Early-return validation: exige nombre+NIT copropiedad, nombre+cédula titular
    e identificación del inmueble (torre/apto).
    """
    fuente = str(datos.get("fuente") or "neon")
    faltantes: list[str] = []
    if not (datos.get("copropiedad_nombre") or "").strip():
        faltantes.append(
            "nombre de la copropiedad"
            + (
                " (conjunto / copropiedad_nombre del PDF)"
                if fuente == "pdf"
                else " (conjuntos_residenciales→contactos.nombre)"
            )
        )
    if not (datos.get("copropiedad_nit") or "").strip():
        faltantes.append(
            "NIT de la copropiedad"
            + (
                " (indique copropiedad_nit; el PDF COLON suele no traerlo)"
                if fuente == "pdf"
                else " (conjuntos_residenciales→contactos.identificacion)"
            )
        )
    if not (datos.get("titular_nombre") or "").strip():
        faltantes.append(
            "nombre del titular"
            + (" (PDF)" if fuente == "pdf" else " (inmueble_propietarios/contactos)")
        )
    if not (datos.get("titular_cedula") or "").strip():
        faltantes.append(
            "cédula del titular"
            + (
                " (indique titular_cedula; el PDF COLON suele no traerla)"
                if fuente == "pdf"
                else " (contactos.identificacion)"
            )
        )
    if not (datos.get("torre_apto") or "").strip():
        faltantes.append(
            "identificación del inmueble torre/apto"
            + (
                " (bloque+apartamento del PDF)"
                if fuente == "pdf"
                else " (inmuebles_ph.torre_apto)"
            )
        )
    if faltantes:
        raise CertificadoDatosFaltantesError(faltantes, fuente=fuente)


@dataclass(frozen=True)
class FilaCertificado:
    """Fila de la tabla oficial: MES | AÑO | ORDINARIAS | EXTRAORDINARIAS | VENCIMIENTO | SALDO."""

    mes: str
    anio: int
    cuotas_ordinarias: float
    cuotas_extraordinarias: float
    vencimiento: str
    saldo: float

    @property
    def periodo(self) -> str:
        """Compat: etiqueta MES AÑO (tests / logs)."""
        return f"{self.mes} {self.anio}"

    @property
    def total_mes(self) -> float:
        return round(self.cuotas_ordinarias + self.cuotas_extraordinarias, 2)

    @property
    def saldo_acumulado(self) -> float:
        return self.saldo


def _normalizar_texto(texto: str) -> str:
    base = unicodedata.normalize("NFKD", str(texto or ""))
    sin = "".join(c for c in base if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sin).upper().strip()


def _es_cuota_ordinaria(concepto: str) -> bool:
    """Heurística: CUOTA + ADMIN*/ADMON → ordinaria; resto del capital limpio → extraordinaria."""
    normal = _normalizar_texto(concepto)
    if not normal or "INTERES" in normal:
        return False
    tokens = normal.split()
    tiene_cuota = any(tok.startswith("CUOT") or tok.startswith("COUT") for tok in tokens)
    tiene_admin = any(
        tok.startswith("ADMIN")
        or tok.startswith("ADMON")
        or tok in {"ADMINISTRATIVA", "ADMINISTRATIVO"}
        for tok in tokens
    )
    return bool(tiene_cuota and tiene_admin)


def _parse_mes_anio(fecha: Any) -> Optional[tuple[int, int]]:
    """Extrae (año, mes) de fecha Bolsa (`2024.02.10`, `2024.02`, date, etc.)."""
    if fecha is None:
        return None
    if isinstance(fecha, date):
        return fecha.year, fecha.month
    texto = str(fecha).strip()
    if not texto:
        return None
    m = re.match(r"^(\d{4})[./-](\d{1,2})(?:[./-]\d{1,2})?", texto)
    if m:
        anio, mes = int(m.group(1)), int(m.group(2))
        if 1 <= mes <= 12:
            return anio, mes
    return None


def _fmt_cop(valor: float) -> str:
    entero = int(round(float(valor or 0)))
    return f"$ {entero:,}".replace(",", ".")


def _fmt_extra(valor: float) -> str:
    """Vacío tipográfico (nbsp) cuando no hay extraordinarias, como en el Word original."""
    if abs(float(valor or 0)) < 1e-9:
        return "\u00a0"
    return _fmt_cop(valor)


def _fmt_vencimiento(anio: int, mes: int, dia: int = 5) -> str:
    """Formato del original: `5-jul-23`."""
    abbr = _MESES_ABBR[mes] if 1 <= mes <= 12 else "mes"
    return f"{int(dia)}-{abbr}-{str(anio)[-2:]}"


def agrupar_capital_limpio(
    items: list[dict[str, Any]] | None,
    *,
    dia_vencimiento: int = 5,
) -> list[FilaCertificado]:
    """
    Agrupa ítems de `capital_limpio_a_demandar` por mes/año.

    - CUOTAS ORDINARIAS: conceptos tipo cuota de administración.
    - CUOTAS EXTRAORDINARIAS: resto del capital limpio (extras, gastos demandables, etc.).
    - SALDO: acumulado fila a fila (orden cronológico).
    - VENCIMIENTO: día fijo del mes (default 5, como el Word del usuario).
    """
    buckets: "OrderedDict[tuple[int, int], dict[str, float]]" = OrderedDict()
    sin_fecha_ord = 0.0
    sin_fecha_ext = 0.0
    dia = max(1, min(28, int(dia_vencimiento or 5)))

    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        try:
            valor = float(raw.get("valor_a_demandar") or 0)
        except (TypeError, ValueError):
            continue
        if abs(valor) < 1e-9:
            continue
        concepto = str(raw.get("concepto") or "")
        ordinarias = _es_cuota_ordinaria(concepto)
        clave = _parse_mes_anio(raw.get("fecha") or raw.get("mes_corte"))
        if clave is None:
            if ordinarias:
                sin_fecha_ord += valor
            else:
                sin_fecha_ext += valor
            continue
        if clave not in buckets:
            buckets[clave] = {"ord": 0.0, "ext": 0.0}
        if ordinarias:
            buckets[clave]["ord"] += valor
        else:
            buckets[clave]["ext"] += valor

    ordenados = sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    filas: list[FilaCertificado] = []
    acum = 0.0
    for (anio, mes), montos in ordenados:
        total = round(montos["ord"] + montos["ext"], 2)
        acum = round(acum + total, 2)
        filas.append(
            FilaCertificado(
                mes=_MESES_ES[mes] if 1 <= mes <= 12 else f"MES{mes}",
                anio=anio,
                cuotas_ordinarias=round(montos["ord"], 2),
                cuotas_extraordinarias=round(montos["ext"], 2),
                vencimiento=_fmt_vencimiento(anio, mes, dia),
                saldo=acum,
            )
        )
    if abs(sin_fecha_ord) > 1e-9 or abs(sin_fecha_ext) > 1e-9:
        total = round(sin_fecha_ord + sin_fecha_ext, 2)
        acum = round(acum + total, 2)
        filas.append(
            FilaCertificado(
                mes="SIN MES",
                anio=0,
                cuotas_ordinarias=round(sin_fecha_ord, 2),
                cuotas_extraordinarias=round(sin_fecha_ext, 2),
                vencimiento="—",
                saldo=acum,
            )
        )
    return filas


def _safe_filename(texto: str) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", (texto or "").strip(), flags=re.UNICODE)
    return cleaned.strip("_")[:80] or "SIN_TITULAR"


def nombre_archivo_certificado(titular_nombre: str) -> str:
    return f"Certificado_{_safe_filename(titular_nombre)}.docx"


def _formatear_propietarios_contexto(
    datos_neon: dict[str, Any],
) -> dict[str, Any]:
    """Lista completa de propietarios para docxtpl (compatible / opcional)."""
    raw = list(
        datos_neon.get("propietarios")
        or datos_neon.get("deudores")
        or []
    )
    if not raw and (datos_neon.get("titular_nombre") or datos_neon.get("titular_cedula")):
        raw = [
            {
                "nombre": datos_neon.get("titular_nombre"),
                "cedula": datos_neon.get("titular_cedula"),
                "es_principal": True,
                "rol": "principal",
            }
        ]
    propietarios: list[dict[str, Any]] = []
    for p in raw:
        es_prin = bool(p.get("es_principal"))
        propietarios.append(
            {
                "nombre": (p.get("nombre") or "").strip(),
                "cedula": (p.get("cedula") or "").strip(),
                "es_principal": es_prin,
                "rol": (p.get("rol") or ("principal" if es_prin else "co_propietario")),
            }
        )
    partes_texto: list[str] = []
    nombres: list[str] = []
    for p in propietarios:
        nom = p["nombre"] or "—"
        nombres.append(nom)
        ced = p["cedula"] or "s/d"
        tag = " (principal)" if p["es_principal"] else ""
        partes_texto.append(f"{nom} (CC {ced}){tag}")
    if len(nombres) <= 1:
        nombres_join = nombres[0] if nombres else ""
    elif len(nombres) == 2:
        nombres_join = f"{nombres[0]} y {nombres[1]}"
    else:
        nombres_join = ", ".join(nombres[:-1]) + f" y {nombres[-1]}"
    return {
        "propietarios": propietarios,
        "propietarios_texto": "; ".join(partes_texto),
        "propietarios_nombres": nombres_join,
        "hay_varios_propietarios": len(propietarios) > 1,
    }


def construir_contexto_plantilla(
    *,
    datos_neon: dict[str, Any],
    filas: list[FilaCertificado],
    ciudad: str = "",
    representante_nombre: str = "",
    representante_cedula: str = "",
    fecha_emision: Optional[date] = None,
) -> dict[str, Any]:
    """Arma el dict que consume la plantilla oficial docxtpl."""
    emision = fecha_emision or date.today()
    total = filas[-1].saldo if filas else 0.0
    ciudad_final = (
        (ciudad or "").strip()
        or (datos_neon.get("ciudad") or "").strip()
        or "Pereira"
    )
    copropiedad = (datos_neon.get("copropiedad_nombre") or "").strip()
    conjunto = (datos_neon.get("conjunto_nombre") or copropiedad).strip()
    multi = _formatear_propietarios_contexto(datos_neon)
    return {
        "copropiedad_nombre": copropiedad,
        "copropiedad_nit": (datos_neon.get("copropiedad_nit") or "").strip(),
        "ciudad": ciudad_final,
        "ciudad_mayus": ciudad_final.upper(),
        "torre_apto": (datos_neon.get("torre_apto") or "").strip(),
        "conjunto_nombre": conjunto,
        # Destinatario plantilla oficial = principal (o único).
        "titular_nombre": (datos_neon.get("titular_nombre") or "").strip(),
        "titular_cedula": (datos_neon.get("titular_cedula") or "").strip(),
        "dia_emision": str(emision.day),
        "mes_emision": _MESES_EMISION[emision.month],
        "anio_emision": str(emision.year),
        "representante_nombre": (representante_nombre or "").strip() or "________________",
        "representante_cedula": (representante_cedula or "").strip() or "________________",
        "filas": [
            {
                "mes": f.mes,
                "anio": str(f.anio) if f.anio else "",
                "cuotas_ordinarias": _fmt_cop(f.cuotas_ordinarias),
                "cuotas_extraordinarias": _fmt_extra(f.cuotas_extraordinarias),
                "vencimiento": f.vencimiento,
                "saldo": _fmt_cop(f.saldo),
            }
            for f in filas
        ],
        "total_saldo": _fmt_cop(total),
        **multi,
    }


def renderizar_docx(contexto: dict[str, Any], *, plantilla: Optional[Path] = None) -> BytesIO:
    """Renderiza plantilla docxtpl → BytesIO (bloqueante)."""
    from docxtpl import DocxTemplate

    path = plantilla or PLANTILLA_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Plantilla de certificado no encontrada: {path}")
    tpl = DocxTemplate(str(path))
    tpl.render(contexto)
    buf = BytesIO()
    tpl.save(buf)
    buf.seek(0)
    return buf


def generar_certificado_deuda(
    *,
    capital_limpio_a_demandar: list[dict[str, Any]] | None,
    inmueble_id: Optional[int] = None,
    conjunto_id: Optional[int] = None,
    conjunto: str = "",
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
    titular: str = "",
    titular_cedula: str = "",
    copropiedad_nombre: str = "",
    copropiedad_nit: str = "",
    codigo_cuenta: str = "",
    ciudad: str = "",
    representante_nombre: str = "",
    representante_cedula: str = "",
    dia_vencimiento: int = 5,
    permitir_datos_pdf: bool = False,
    conn=None,
) -> tuple[BytesIO, str, dict[str, Any]]:
    """
    Orquesta validación Neon → (opcional fallback PDF) → agrupación → Word.

    Prioriza match Neon. Si no hay fila y `permitir_datos_pdf`, usa titular /
    bloque-apto / conjunto del PDF; NIT y cédula deben venir en el body
    (no se inventan).

    Returns:
        (buffer_docx, nombre_archivo, meta)
    """
    datos = resolver_datos_certificado(
        inmueble_id=inmueble_id,
        conjunto_id=conjunto_id,
        conjunto_nombre=conjunto,
        torre_apto=torre_apto,
        bloque=bloque,
        apartamento=apartamento,
        titular=titular,
        conn=conn,
        incluir_propietarios=True,
    )
    if not datos:
        criterios = describir_busqueda(
            inmueble_id=inmueble_id,
            conjunto_id=conjunto_id,
            conjunto_nombre=conjunto,
            torre_apto=torre_apto,
            bloque=bloque,
            apartamento=apartamento,
            titular=titular,
            codigo_cuenta=codigo_cuenta,
        )
        if permitir_datos_pdf:
            datos = datos_certificado_desde_pdf(
                conjunto=conjunto,
                torre_apto=torre_apto,
                bloque=bloque,
                apartamento=apartamento,
                titular=titular,
                titular_cedula=titular_cedula,
                copropiedad_nombre=copropiedad_nombre,
                copropiedad_nit=copropiedad_nit,
                ciudad=ciudad,
            )
            datos["propietarios"] = [
                {
                    "nombre": datos.get("titular_nombre"),
                    "cedula": datos.get("titular_cedula"),
                    "es_principal": True,
                    "rol": "principal",
                }
            ]
            datos["deudores"] = list(datos["propietarios"])
        else:
            raise CertificadoNoEncontradoError(
                "No se encontró inmueble/titular en Neon. "
                "El cruce usa inmueble_id o conjunto+unidad (torre/apto o bloque+apartamento); "
                f"`codigo_cuenta` COLON no indexa maestros. Buscado: {criterios}. "
                "Verifique que el conjunto exista en conjuntos_residenciales / "
                "inmuebles_ph.conjunto_residencial y que torre_apto en Neon "
                "corresponda a bloque-apartamento del PDF (p.ej. '02-042' ≡ '2-42'). "
                "Active `permitir_datos_pdf` para emitir con datos del PDF "
                "(requiere NIT y cédula explícitos; no se inventan).",
                criterios=criterios,
            )
    # Overrides explícitos del body (fallback / corrección manual) sin inventar.
    if (titular or "").strip():
        # No reemplaza principal Neon salvo fallback PDF (sin inmueble_id).
        if datos.get("fuente") == "pdf" or not datos.get("titular_nombre"):
            datos["titular_nombre"] = titular.strip()
    if (titular_cedula or "").strip() and (
        datos.get("fuente") == "pdf" or not (datos.get("titular_cedula") or "").strip()
    ):
        datos["titular_cedula"] = titular_cedula.strip()
    if (copropiedad_nombre or "").strip() and not (datos.get("copropiedad_nombre") or "").strip():
        datos["copropiedad_nombre"] = copropiedad_nombre.strip()
    if (copropiedad_nit or "").strip() and not (datos.get("copropiedad_nit") or "").strip():
        datos["copropiedad_nit"] = copropiedad_nit.strip()

    validar_datos_criticos(datos)

    filas = agrupar_capital_limpio(
        capital_limpio_a_demandar,
        dia_vencimiento=dia_vencimiento,
    )
    contexto = construir_contexto_plantilla(
        datos_neon=datos,
        filas=filas,
        ciudad=ciudad,
        representante_nombre=representante_nombre,
        representante_cedula=representante_cedula,
    )
    buffer = renderizar_docx(contexto)
    nombre = nombre_archivo_certificado(str(datos.get("titular_nombre") or ""))
    meta = {
        "inmueble_id": datos.get("inmueble_id"),
        "titular_nombre": datos.get("titular_nombre"),
        "torre_apto": datos.get("torre_apto"),
        "filas": len(filas),
        "total_saldo": contexto.get("total_saldo"),
        "n_propietarios": len(contexto.get("propietarios") or []),
        "hay_varios_propietarios": bool(contexto.get("hay_varios_propietarios")),
        "placeholders": list(PLANTILLA_PLACEHOLDERS)
        + list(PLANTILLA_PLACEHOLDERS_MULTI),
        "plantilla": str(PLANTILLA_PATH.name),
        "fuente": datos.get("fuente") or "neon",
    }
    return buffer, nombre, meta
