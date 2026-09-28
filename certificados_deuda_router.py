"""Router FastAPI: Certificado de Deuda Word desde Bolsa Global + Neon."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
    permitir_datos_pdf: bool = Field(
        default=False,
        description=(
            "Reservado: emitir con datos del PDF si Neon no tiene maestro. "
            "No implementado; el certificado exige match Neon."
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
            codigo_cuenta=payload.codigo_cuenta,
            ciudad=payload.ciudad,
            representante_nombre=payload.representante_nombre,
            representante_cedula=payload.representante_cedula,
            dia_vencimiento=payload.dia_vencimiento,
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
