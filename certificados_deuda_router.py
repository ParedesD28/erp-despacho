"""Router FastAPI: Certificado de Deuda Word desde Bolsa Global + Neon."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from certificados_deuda_flujo_service import (
    REGLA_MULTI_DEUDOR_DOC,
    procesar_y_generar_certificados,
)
from certificados_deuda_service import (
    CertificadoDatosFaltantesError,
    CertificadoNoEncontradoError,
    generar_certificado_deuda,
)

router = APIRouter(tags=["Certificados de deuda"])


class CertificadoDeudaRequest(BaseModel):
    """Contrato de entrada: resultado Bolsa Global + identificadores Neon/PDF."""

    capital_limpio_a_demandar: list[dict[str, Any]] = Field(default_factory=list)
    inmueble_id: Optional[int] = None
    conjunto_id: Optional[int] = None
    conjunto: str = ""
    torre_apto: str = ""
    bloque: str = ""
    apartamento: str = ""
    codigo_cuenta: str = Field(
        default="",
        description="Referencia COLON del PDF; Neon no indexa codigo_cuenta en maestros.",
    )
    ciudad: str = Field(
        default="",
        description="Ciudad del domicilio / juzgado; si vacío usa contactos.ciudad o Pereira.",
    )
    representante_nombre: str = Field(
        default="",
        description="Nombre del representante legal que firma el certificado.",
    )
    representante_cedula: str = Field(
        default="",
        description="Cédula del representante legal.",
    )
    dia_vencimiento: int = Field(
        default=5,
        ge=1,
        le=28,
        description="Día de vencimiento por fila (formato 5-jul-23 del Word oficial).",
    )
    titular: str = Field(
        default="",
        description=(
            "Nombre del titular del PDF; desambigua unidad y permite "
            "fallback si el nombre de conjunto del PDF no cuadra con Neon."
        ),
    )
    titular_cedula: str = Field(
        default="",
        description=(
            "Cédula del titular. Obligatoria si permitir_datos_pdf y Neon no "
            "tiene maestro (no se inventa)."
        ),
    )
    copropiedad_nombre: str = Field(
        default="",
        description="Nombre PH opcional para fallback PDF (default: conjunto).",
    )
    copropiedad_nit: str = Field(
        default="",
        description=(
            "NIT de la copropiedad. Obligatorio si permitir_datos_pdf y Neon "
            "no tiene maestro (no se inventa)."
        ),
    )
    permitir_datos_pdf: bool = Field(
        default=False,
        description=(
            "Si Neon no tiene la unidad, emitir con titular/bloque-apto/conjunto "
            "del PDF. Exige NIT y cédula explícitos (body); no inventa datos."
        ),
    )
    incluir_poder: bool = Field(
        default=True,
        description=(
            "Si true, append del poder en el mismo .docx tras un salto de página. "
            "FMI opcional por cuenta; vacío = se omite en el poder."
        ),
    )
    fmi: str = Field(
        default="",
        description=(
            "Folio de matrícula inmobiliaria (opcional). Vacío = se omite en el "
            "poder; no se inventa ni bloquea la emisión."
        ),
    )


@router.post("/herramientas/estado-cuenta/certificado-deuda")
async def emitir_certificado_deuda(payload: CertificadoDeudaRequest):
    """
    Genera Certificado_{TITULAR}.docx con la plantilla oficial (docxtpl).

    Auth/RBAC: sesión + accion.editar vía middleware de POST genérico
    (igual que bolsa-global). DB y Word en `asyncio.to_thread` (psycopg2 sync).
    """
    if (
        payload.inmueble_id is None
        and not payload.conjunto_id
        and not (payload.conjunto or "").strip()
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Debe indicar inmueble_id, o conjunto_id, o nombre de conjunto "
                "para cruzar con Neon."
            ),
        )
    if (
        payload.inmueble_id is None
        and not (payload.torre_apto or "").strip()
        and not ((payload.bloque or "").strip() and (payload.apartamento or "").strip())
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Sin inmueble_id debe indicar torre_apto o bloque+apartamento "
                "para identificar la unidad."
            ),
        )

    try:
        buffer, nombre, _meta = await asyncio.to_thread(
            generar_certificado_deuda,
            capital_limpio_a_demandar=payload.capital_limpio_a_demandar,
            inmueble_id=payload.inmueble_id,
            conjunto_id=payload.conjunto_id,
            conjunto=payload.conjunto,
            torre_apto=payload.torre_apto,
            bloque=payload.bloque,
            apartamento=payload.apartamento,
            titular=payload.titular,
            titular_cedula=payload.titular_cedula,
            copropiedad_nombre=payload.copropiedad_nombre,
            copropiedad_nit=payload.copropiedad_nit,
            codigo_cuenta=payload.codigo_cuenta,
            ciudad=payload.ciudad,
            representante_nombre=payload.representante_nombre,
            representante_cedula=payload.representante_cedula,
            dia_vencimiento=payload.dia_vencimiento,
            permitir_datos_pdf=payload.permitir_datos_pdf,
            incluir_poder=payload.incluir_poder,
            fmi=payload.fmi,
        )
    except CertificadoNoEncontradoError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CertificadoDatosFaltantesError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(
        buffer,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{nombre}"',
        },
    )


def _parse_indices(raw: str) -> Optional[list[int]]:
    texto = (raw or "").strip()
    if not texto:
        return None
    try:
        data = json.loads(texto)
    except json.JSONDecodeError:
        # "0,1,2"
        parts = [p.strip() for p in texto.split(",") if p.strip()]
        return [int(p) for p in parts]
    if isinstance(data, list):
        return [int(x) for x in data]
    raise ValueError("indices debe ser JSON array o lista separada por comas")


def _parse_fmi_por_indice(raw: str) -> Optional[dict[int, str]]:
    """Parsea mapa índice→FMI (`{"0":"290-1"}` o `[[0,"290-1"]]`)."""
    texto = (raw or "").strip()
    if not texto:
        return None
    try:
        data = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "fmi_por_indice debe ser JSON objeto o lista de pares"
        ) from exc
    if isinstance(data, dict):
        return {int(k): str(v or "").strip() for k, v in data.items()}
    if isinstance(data, list):
        out: dict[int, str] = {}
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                out[int(item[0])] = str(item[1] or "").strip()
            elif isinstance(item, dict) and "indice" in item:
                out[int(item["indice"])] = str(item.get("fmi") or "").strip()
            else:
                raise ValueError(
                    "cada entrada de fmi_por_indice requiere indice y fmi"
                )
        return out
    raise ValueError("fmi_por_indice debe ser JSON objeto o lista")


@router.post("/herramientas/estado-cuenta/procesar-certificado")
async def procesar_certificado_unificado(
    cache_id: str = Form(""),
    archivos: list[UploadFile] | None = File(None),
    modo: str = Form(
        "preview",
        description="preview = JSON deudores; generar = docx/zip",
    ),
    permitir_datos_pdf: bool = Form(False),
    titular_cedula: str = Form(""),
    copropiedad_nit: str = Form(""),
    copropiedad_nombre: str = Form(""),
    representante_nombre: str = Form(""),
    representante_cedula: str = Form(""),
    dia_vencimiento: int = Form(5),
    ciudad: str = Form(""),
    indices: str = Form(
        "",
        description="Opcional: índices JSON del preview a emitir, p.ej. [0,2]",
    ),
    filtro_conjunto: str = Form(
        "",
        description="Opcional: filtrar por nombre de conjunto (substring).",
    ),
    filtro_busqueda: str = Form(
        "",
        description="Opcional: búsqueda en titular/archivo/unidad/cuenta.",
    ),
    solo_match_neon: bool = Form(
        False,
        description="Si true, solo emite cuentas con match Neon (ok/varios_propietarios).",
    ),
    incluir_poder: bool = Form(
        True,
        description=(
            "Si true, cada Word incluye el poder (misma sección tras page break)."
        ),
    ),
    fmi_por_indice: str = Form(
        "",
        description=(
            "Opcional (modo generar): JSON mapa índice→FMI, p.ej. "
            '{"0":"290-219335","2":"290-1"}. Vacío por cuenta = se omite.'
        ),
    ),
):
    """
    Flujo unificado: Bolsa Global → lookup Neon (clave canónica) → certificado(s).

    Auth: sesión + accion.editar (POST genérico).

    - `modo=preview`: JSON con deudores (todos los propietarios), estados
      (`ok` / `varios_propietarios` / `sin_match` / …), `sin_match_detalle`
      y regla `unidad_todos_propietarios`.
    - `modo=generar`: un `.docx` o `.zip`; **un Word por unidad** (principal
      en plantilla; lista completa en contexto). No prorratea Bolsa.
      Acepta `indices` y/o filtros (`filtro_conjunto`, `filtro_busqueda`,
      `solo_match_neon`). Con `incluir_poder` (default true) append del poder
      en el mismo archivo. `fmi_por_indice` alimenta el placeholder FMI del
      poder por cuenta (vacío permitido; no se inventa).

    Acepta `cache_id` (tras Analizar) y/o `archivos` PDF.
    `sin_match` = fallo de cruce Neon (no confundir con co-propietarios).
    """
    # Import diferido evita ciclo con main (helpers de caché/upload).
    import main as erp_main

    try:
        idxs = _parse_indices(indices)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        fmi_map = _parse_fmi_por_indice(fmi_por_indice)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cuentas = await erp_main._cuentas_lote_para_bolsa(cache_id, archivos)

    try:
        resultado = await asyncio.to_thread(
            procesar_y_generar_certificados,
            cuentas,
            modo=modo,
            permitir_datos_pdf=permitir_datos_pdf,
            titular_cedula=titular_cedula,
            copropiedad_nit=copropiedad_nit,
            copropiedad_nombre=copropiedad_nombre,
            representante_nombre=representante_nombre,
            representante_cedula=representante_cedula,
            dia_vencimiento=int(dia_vencimiento or 5),
            ciudad=ciudad,
            indices=idxs,
            filtro_conjunto=filtro_conjunto,
            filtro_busqueda=filtro_busqueda,
            solo_match_neon=solo_match_neon,
            incluir_poder=incluir_poder,
            fmi_por_indice=fmi_map,
        )
    except CertificadoNoEncontradoError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CertificadoDatosFaltantesError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if isinstance(resultado, dict):
        # Preview JSON (sin bytes de plantilla / bolsa rows crudos enormes)
        bolsa = resultado.pop("bolsa", None)
        if bolsa is not None:
            resultado["bolsa_resumen"] = {
                "cuentas_evaluadas": bolsa.get("cuentas_evaluadas"),
                "cuentas_con_capital": bolsa.get("cuentas_con_capital"),
                "total_capital_demandado_lote": bolsa.get(
                    "total_capital_demandado_lote"
                ),
            }
        # Quitar datos_neon pesados / internos de cada ítem
        for item in resultado.get("resultados") or []:
            item.pop("datos_neon", None)
            item.pop("capital_limpio_a_demandar", None)
        resultado["regla_multi_deudor_doc"] = REGLA_MULTI_DEUDOR_DOC
        return JSONResponse(resultado)

    buffer, nombre, meta = resultado
    is_zip = nombre.lower().endswith(".zip")
    media = (
        "application/zip"
        if is_zip
        else (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        )
    )
    return StreamingResponse(
        buffer,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "X-Certificados-Generados": str(meta.get("generados") or 0),
            "X-Certificados-Fallidos": str(meta.get("fallidos") or 0),
            "X-Regla-Multi-Deudor": str(meta.get("regla_multi_deudor") or ""),
        },
    )


@router.post("/herramientas/estado-cuenta/fmi-parse-tabla")
async def fmi_parse_tabla(
    archivo: UploadFile = File(..., description="CSV o Excel con cedula/fmi u otras claves"),
):
    """Parsea CSV/Excel a filas FMI para aplicar en el preview (cliente)."""
    from certificados_fmi_masivo import (
        _parece_excel_fmi,
        filas_desde_excel,
        parse_fmi_csv,
    )

    nombre = (archivo.filename or "").strip()
    raw = await archivo.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Archivo vacío")
    try:
        if _parece_excel_fmi(nombre, raw):
            # Nunca decode OOXML como UTF-8/CSV — eso pierde CC|FMI y falla
            # con "CSV debe incluir columna fmi".
            filas = filas_desde_excel(raw, nombre=nombre or "fmi.xlsx")
        else:
            texto = raw.decode("utf-8-sig")
            filas = parse_fmi_csv(texto)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="No se pudo leer el archivo como texto UTF-8; use CSV o Excel (.xlsx).",
        ) from exc
    return {"filas": filas, "archivo": nombre, "n": len(filas)}
