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

from certificados_deuda_repository import resolver_datos_certificado

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


class CertificadoDatosFaltantesError(ValueError):
    """Faltan campos críticos para emitir el certificado."""

    def __init__(self, faltantes: list[str]):
        self.faltantes = list(faltantes)
        detalle = "; ".join(self.faltantes)
        super().__init__(f"Faltan datos críticos en Neon: {detalle}")


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


def validar_datos_criticos(datos: dict[str, Any]) -> None:
    """
    Early-return validation: exige nombre+NIT copropiedad, nombre+cédula titular
    e identificación del inmueble (torre/apto).
    """
    faltantes: list[str] = []
    if not (datos.get("copropiedad_nombre") or "").strip():
        faltantes.append("nombre de la copropiedad (conjuntos_residenciales→contactos.nombre)")
    if not (datos.get("copropiedad_nit") or "").strip():
        faltantes.append("NIT de la copropiedad (conjuntos_residenciales→contactos.identificacion)")
    if not (datos.get("titular_nombre") or "").strip():
        faltantes.append("nombre del titular (inmueble_propietarios/contactos)")
    if not (datos.get("titular_cedula") or "").strip():
        faltantes.append("cédula del titular (contactos.identificacion)")
    if not (datos.get("torre_apto") or "").strip():
        faltantes.append("identificación del inmueble torre/apto (inmuebles_ph.torre_apto)")
    if faltantes:
        raise CertificadoDatosFaltantesError(faltantes)


def _safe_filename(texto: str) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", (texto or "").strip(), flags=re.UNICODE)
    return cleaned.strip("_")[:80] or "SIN_TITULAR"


def nombre_archivo_certificado(titular_nombre: str) -> str:
    return f"Certificado_{_safe_filename(titular_nombre)}.docx"


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
    return {
        "copropiedad_nombre": copropiedad,
        "copropiedad_nit": (datos_neon.get("copropiedad_nit") or "").strip(),
        "ciudad": ciudad_final,
        "ciudad_mayus": ciudad_final.upper(),
        "torre_apto": (datos_neon.get("torre_apto") or "").strip(),
        "conjunto_nombre": conjunto,
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
    ciudad: str = "",
    representante_nombre: str = "",
    representante_cedula: str = "",
    dia_vencimiento: int = 5,
    conn=None,
) -> tuple[BytesIO, str, dict[str, Any]]:
    """
    Orquesta validación Neon → agrupación → Word (plantilla oficial).

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
        conn=conn,
    )
    if not datos:
        raise CertificadoNoEncontradoError(
            "No se encontró inmueble/titular en Neon con los identificadores "
            "proporcionados (inmueble_id o conjunto+torre/apto)."
        )
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
        "inmueble_id": datos["inmueble_id"],
        "titular_nombre": datos["titular_nombre"],
        "torre_apto": datos["torre_apto"],
        "filas": len(filas),
        "placeholders": list(PLANTILLA_PLACEHOLDERS),
        "plantilla": str(PLANTILLA_PATH.name),
    }
    return buffer, nombre, meta
