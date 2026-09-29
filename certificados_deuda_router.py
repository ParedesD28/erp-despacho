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
):
    """
    Flujo unificado: Bolsa Global → lookup Neon (clave canónica) → certificado(s).

    Auth: sesión + accion.editar (POST genérico).

    - `modo=preview`: JSON con deudores, estado match y regla multi-deudor
      (titular principal).
    - `modo=generar`: un `.docx` o `.zip` si hay varios; un Word por unidad
      al titular principal de `inmueble_propietarios`.

    Acepta `cache_id` (tras Analizar) y/o `archivos` PDF.
    """
    # Import diferido evita ciclo con main (helpers de caché/upload).
    import main as erp_main

    try:
        idxs = _parse_indices(indices)
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
