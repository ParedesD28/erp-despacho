"""Flujo unificado: Bolsa Global → lookup Neon → Certificado(s) Word.

Orquesta parseo ya cacheado / Bolsa + matching canónico + docxtpl.
Regla multi-deudor: **un certificado por titular principal**
(`inmueble_propietarios.es_principal`); co-propietarios se listan en preview
pero no generan Word adicionales (más seguro litigiosamente: un demandado
principal por unidad).
"""
from __future__ import annotations

import zipfile
from io import BytesIO
from typing import Any, Optional

from bolsa_global_estado_cuenta_service import calcular_bolsa_lote
from certificados_deuda_repository import (
    clave_canonica_unidad,
    describir_busqueda,
    resolver_datos_certificado,
)
from certificados_deuda_service import (
    CertificadoDatosFaltantesError,
    CertificadoNoEncontradoError,
    generar_certificado_deuda,
)


REGLA_MULTI_DEUDOR = "titular_principal"
REGLA_MULTI_DEUDOR_DOC = (
    "Un certificado por titular principal de inmueble_propietarios "
    "(es_principal=true; si ninguno, el primero). Co-propietarios se informan "
    "en preview pero no reciben Word aparte."
)


def _meta_cuenta_bolsa(resultado: dict[str, Any]) -> dict[str, Any]:
    bloque = str(resultado.get("bloque") or "").strip()
    apto = str(resultado.get("apartamento") or "").strip()
    canon = clave_canonica_unidad(
        str(resultado.get("torre_apto") or ""),
        bloque=bloque,
        apartamento=apto,
    )
    return {
        "archivo": resultado.get("archivo") or "",
        "titular_pdf": resultado.get("titular") or "",
        "bloque": bloque,
        "apartamento": apto,
        "clave_canonica": canon,
        "codigo_cuenta": resultado.get("codigo_cuenta") or "",
        "conjunto": resultado.get("conjunto") or "",
        "omitido": bool(resultado.get("omitido")),
        "fecha_inicio_mora": resultado.get("fecha_inicio_mora"),
        "total_capital_demandado": resultado.get("total_capital_demandado"),
        "capital_limpio_a_demandar": list(
            resultado.get("capital_limpio_a_demandar") or []
        ),
        "errores_procesamiento": list(resultado.get("errores_procesamiento") or []),
    }


def _lookup_cuenta(
    resultado: dict[str, Any],
    *,
    permitir_datos_pdf: bool = False,
    titular_cedula: str = "",
    copropiedad_nit: str = "",
    copropiedad_nombre: str = "",
) -> dict[str, Any]:
    """Resuelve deudor(es) Neon para un resultado de Bolsa."""
    meta = _meta_cuenta_bolsa(resultado)
    items = meta["capital_limpio_a_demandar"]

    if meta["omitido"]:
        return {
            **meta,
            "estado": "omitido",
            "inmueble_id": None,
            "deudores": [],
            "titular_seleccionado": None,
            "error": "PDF omitido / no parseado",
            "criterios": "",
        }
    if not items:
        return {
            **meta,
            "estado": "sin_capital",
            "inmueble_id": None,
            "deudores": [],
            "titular_seleccionado": None,
            "error": "Sin capital limpio a demandar",
            "criterios": "",
        }

    criterios = describir_busqueda(
        conjunto_nombre=meta["conjunto"],
        torre_apto=f"TORRE {meta['bloque']} APTO {meta['apartamento']}"
        if meta["bloque"] and meta["apartamento"]
        else meta["clave_canonica"],
        bloque=meta["bloque"],
        apartamento=meta["apartamento"],
        titular=meta["titular_pdf"],
        codigo_cuenta=meta["codigo_cuenta"],
    )

    hallado = resolver_datos_certificado(
        conjunto_nombre=meta["conjunto"],
        torre_apto=meta["clave_canonica"]
        or (
            f"TORRE {meta['bloque']} APTO {meta['apartamento']}"
            if meta["bloque"] and meta["apartamento"]
            else ""
        ),
        bloque=meta["bloque"],
        apartamento=meta["apartamento"],
        titular=meta["titular_pdf"],
        incluir_propietarios=True,
    )

    if hallado:
        deudores = list(hallado.get("deudores") or hallado.get("propietarios") or [])
        principal = hallado.get("titular_principal") or (
            deudores[0] if deudores else None
        )
        estado = "ok"
        if len(deudores) > 1:
            estado = "varios_propietarios"
        return {
            **meta,
            "estado": estado,
            "inmueble_id": hallado.get("inmueble_id"),
            "torre_apto_neon": hallado.get("torre_apto"),
            "conjunto_nombre": hallado.get("conjunto_nombre"),
            "copropiedad_nombre": hallado.get("copropiedad_nombre"),
            "copropiedad_nit": hallado.get("copropiedad_nit"),
            "deudores": deudores,
            "titular_seleccionado": principal,
            "regla_multi_deudor": REGLA_MULTI_DEUDOR,
            "error": None,
            "criterios": criterios,
            "datos_neon": hallado,
        }

    if permitir_datos_pdf:
        return {
            **meta,
            "estado": "fallback_pdf",
            "inmueble_id": None,
            "deudores": [
                {
                    "contacto_id": None,
                    "nombre": meta["titular_pdf"] or None,
                    "cedula": (titular_cedula or "").strip() or None,
                    "es_principal": True,
                }
            ],
            "titular_seleccionado": {
                "contacto_id": None,
                "nombre": meta["titular_pdf"] or None,
                "cedula": (titular_cedula or "").strip() or None,
                "es_principal": True,
            },
            "copropiedad_nit": (copropiedad_nit or "").strip() or None,
            "copropiedad_nombre": (copropiedad_nombre or "").strip()
            or meta["conjunto"]
            or None,
            "regla_multi_deudor": REGLA_MULTI_DEUDOR,
            "error": None,
            "criterios": criterios,
            "datos_neon": None,
        }

    return {
        **meta,
        "estado": "sin_match",
        "inmueble_id": None,
        "deudores": [],
        "titular_seleccionado": None,
        "error": (
            "No se encontró inmueble/titular en Neon. "
            "Active 'Permitir datos del PDF' e indique NIT y cédula, "
            "o cargue el maestro."
        ),
        "criterios": criterios,
    }


def procesar_lote_certificados(
    cuentas: list[dict[str, Any]],
    *,
    permitir_datos_pdf: bool = False,
    titular_cedula: str = "",
    copropiedad_nit: str = "",
    copropiedad_nombre: str = "",
) -> dict[str, Any]:
    """
    Bolsa Global por cuenta + lookup Neon (clave canónica).

    No genera Word; solo preview / diagnóstico.
    """
    bolsa = calcular_bolsa_lote(cuentas)
    resultados: list[dict[str, Any]] = []
    for r in bolsa.get("resultados") or []:
        resultados.append(
            _lookup_cuenta(
                r,
                permitir_datos_pdf=permitir_datos_pdf,
                titular_cedula=titular_cedula,
                copropiedad_nit=copropiedad_nit,
                copropiedad_nombre=copropiedad_nombre,
            )
        )

    emitibles = [
        x
        for x in resultados
        if x.get("estado") in {"ok", "varios_propietarios", "fallback_pdf"}
        and x.get("capital_limpio_a_demandar")
    ]
    sin_match = [x for x in resultados if x.get("estado") == "sin_match"]
    return {
        "cuentas_evaluadas": bolsa.get("cuentas_evaluadas") or 0,
        "cuentas_con_capital": bolsa.get("cuentas_con_capital") or 0,
        "total_capital_demandado_lote": bolsa.get("total_capital_demandado_lote") or 0,
        "emitibles": len(emitibles),
        "sin_match": len(sin_match),
        "regla_multi_deudor": REGLA_MULTI_DEUDOR,
        "regla_multi_deudor_doc": REGLA_MULTI_DEUDOR_DOC,
        "resultados": resultados,
        "bolsa": bolsa,
    }


def _generar_uno(
    item: dict[str, Any],
    *,
    representante_nombre: str = "",
    representante_cedula: str = "",
    dia_vencimiento: int = 5,
    permitir_datos_pdf: bool = False,
    titular_cedula: str = "",
    copropiedad_nit: str = "",
    copropiedad_nombre: str = "",
    ciudad: str = "",
) -> tuple[BytesIO, str, dict[str, Any]]:
    """Genera un certificado para el titular principal de un resultado preview."""
    neon = item.get("datos_neon")
    inmueble_id = item.get("inmueble_id")
    sel = item.get("titular_seleccionado") or {}
    titular_nombre = (sel.get("nombre") or item.get("titular_pdf") or "").strip()
    cedula = (sel.get("cedula") or titular_cedula or "").strip()

    return generar_certificado_deuda(
        capital_limpio_a_demandar=item.get("capital_limpio_a_demandar") or [],
        inmueble_id=int(inmueble_id) if inmueble_id is not None else None,
        conjunto=str(item.get("conjunto") or ""),
        torre_apto=str(
            item.get("torre_apto_neon")
            or item.get("clave_canonica")
            or ""
        ),
        bloque=str(item.get("bloque") or ""),
        apartamento=str(item.get("apartamento") or ""),
        titular=titular_nombre,
        titular_cedula=cedula,
        copropiedad_nombre=str(
            (neon or {}).get("copropiedad_nombre")
            or item.get("copropiedad_nombre")
            or copropiedad_nombre
            or ""
        ),
        copropiedad_nit=str(
            (neon or {}).get("copropiedad_nit")
            or item.get("copropiedad_nit")
            or copropiedad_nit
            or ""
        ),
        codigo_cuenta=str(item.get("codigo_cuenta") or ""),
        ciudad=ciudad
        or str((neon or {}).get("ciudad") or ""),
        representante_nombre=representante_nombre,
        representante_cedula=representante_cedula,
        dia_vencimiento=dia_vencimiento,
        permitir_datos_pdf=permitir_datos_pdf
        or item.get("estado") == "fallback_pdf",
    )


def generar_certificados_desde_preview(
    preview: dict[str, Any],
    *,
    representante_nombre: str = "",
    representante_cedula: str = "",
    dia_vencimiento: int = 5,
    permitir_datos_pdf: bool = False,
    titular_cedula: str = "",
    copropiedad_nit: str = "",
    copropiedad_nombre: str = "",
    ciudad: str = "",
    indices: Optional[list[int]] = None,
) -> tuple[BytesIO, str, dict[str, Any]]:
    """
    Emite Word(s) para resultados emitibles del preview.

    - 1 certificado → docx suelto
    - varios → zip `Certificados_deuda.zip`
    Regla: un Word por cuenta/unidad al **titular principal**.
    """
    resultados = list(preview.get("resultados") or [])
    if indices is not None:
        elegidos = [resultados[i] for i in indices if 0 <= i < len(resultados)]
    else:
        elegidos = [
            r
            for r in resultados
            if r.get("estado") in {"ok", "varios_propietarios", "fallback_pdf"}
            and r.get("capital_limpio_a_demandar")
        ]

    if not elegidos:
        raise CertificadoNoEncontradoError(
            "No hay cuentas emitibles para certificado. "
            "Revise matches Neon o active permitir_datos_pdf con NIT/cédula.",
            criterios=f"sin_match={preview.get('sin_match')}",
        )

    generados: list[tuple[str, bytes]] = []
    meta_lista: list[dict[str, Any]] = []
    errores: list[str] = []

    for item in elegidos:
        try:
            buf, nombre, meta = _generar_uno(
                item,
                representante_nombre=representante_nombre,
                representante_cedula=representante_cedula,
                dia_vencimiento=dia_vencimiento,
                permitir_datos_pdf=permitir_datos_pdf,
                titular_cedula=titular_cedula,
                copropiedad_nit=copropiedad_nit,
                copropiedad_nombre=copropiedad_nombre,
                ciudad=ciudad,
            )
            generados.append((nombre, buf.getvalue()))
            meta_lista.append(meta)
        except (CertificadoNoEncontradoError, CertificadoDatosFaltantesError) as exc:
            errores.append(f"{item.get('archivo') or '?'}: {exc}")

    if not generados:
        raise CertificadoDatosFaltantesError(
            errores or ["No se pudo generar ningún certificado"],
            fuente="neon",
        )

    resumen = {
        "generados": len(generados),
        "fallidos": len(errores),
        "errores": errores,
        "regla_multi_deudor": REGLA_MULTI_DEUDOR,
        "metas": meta_lista,
    }

    if len(generados) == 1:
        nombre, raw = generados[0]
        return BytesIO(raw), nombre, resumen

    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        usados: dict[str, int] = {}
        for nombre, raw in generados:
            base = nombre
            if base in usados:
                usados[base] += 1
                stem, _, ext = base.rpartition(".")
                base = f"{stem}_{usados[nombre]}.{ext}" if stem else f"{base}_{usados[nombre]}"
            else:
                usados[base] = 0
            zf.writestr(base, raw)
    zip_buf.seek(0)
    return zip_buf, "Certificados_deuda.zip", resumen


def procesar_y_generar_certificados(
    cuentas: list[dict[str, Any]],
    *,
    modo: str = "preview",
    permitir_datos_pdf: bool = False,
    titular_cedula: str = "",
    copropiedad_nit: str = "",
    copropiedad_nombre: str = "",
    representante_nombre: str = "",
    representante_cedula: str = "",
    dia_vencimiento: int = 5,
    ciudad: str = "",
    indices: Optional[list[int]] = None,
) -> dict[str, Any] | tuple[BytesIO, str, dict[str, Any]]:
    """
    Punto único: preview (dict) o generar (buffer, filename, meta).

    `modo`:
      - `preview` → JSON con deudores / estados
      - `generar` → docx o zip
    """
    preview = procesar_lote_certificados(
        cuentas,
        permitir_datos_pdf=permitir_datos_pdf,
        titular_cedula=titular_cedula,
        copropiedad_nit=copropiedad_nit,
        copropiedad_nombre=copropiedad_nombre,
    )
    if (modo or "preview").strip().lower() != "generar":
        return preview
    return generar_certificados_desde_preview(
        preview,
        representante_nombre=representante_nombre,
        representante_cedula=representante_cedula,
        dia_vencimiento=dia_vencimiento,
        permitir_datos_pdf=permitir_datos_pdf,
        titular_cedula=titular_cedula,
        copropiedad_nit=copropiedad_nit,
        copropiedad_nombre=copropiedad_nombre,
        ciudad=ciudad,
        indices=indices,
    )
