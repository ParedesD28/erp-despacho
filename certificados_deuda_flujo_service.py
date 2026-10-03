"""Flujo unificado: Bolsa Global → lookup Neon → Certificado(s) Word.

Orquesta parseo ya cacheado / Bolsa + matching canónico + docxtpl.

Regla multi-propietario: **un Word por unidad (cuenta)** que relaciona a
TODOS los propietarios de `inmueble_propietarios`. El principal llena
`titular_*` de la plantilla; co-propietarios van en preview + contexto
docxtpl. Misma Bolsa/capital de la cuenta — **nunca** prorratear.
`sin_match` = fallo de cruce Neon (no confundir con co-propietarios).
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
    validar_antefirma_representante,
)


REGLA_MULTI_DEUDOR = "unidad_todos_propietarios"
REGLA_MULTI_DEUDOR_DOC = (
    "Un Word por unidad (cuenta) que lista todos los propietarios "
    "relacionados; titular_nombre/cedula = principal (o primero). "
    "Misma Bolsa/capital de la cuenta — no prorratear. "
    "Co-propietarios no reciben Word aparte. "
    "sin_match = unidad/conjunto no hallado en Neon (no son codeudores)."
)

# Estados con match Neon usable (sin fallback PDF).
ESTADOS_MATCH_NEON = frozenset({"ok", "varios_propietarios"})
ESTADOS_EMITIBLES = frozenset({"ok", "varios_propietarios", "fallback_pdf"})

MOTIVO_SIN_MATCH = "sin_inmueble_neon"
MOTIVO_SIN_CAPITAL = "sin_capital_limpio"
MOTIVO_OMITIDO = "pdf_omitido"


def indices_filtrados_preview(
    preview: dict[str, Any],
    *,
    conjunto: str = "",
    busqueda: str = "",
    solo_match_neon: bool = False,
    solo_emitibles: bool = False,
) -> list[int]:
    """
    Índices de `preview['resultados']` que pasan filtros de lote multi-conjunto.

    Pensado para `modo=generar` + `indices=[…]` sin re-mezclar deudores.
    """
    resultados = list(preview.get("resultados") or [])
    conj = (conjunto or "").strip().casefold()
    q = (busqueda or "").strip().casefold()
    out: list[int] = []
    for i, r in enumerate(resultados):
        estado = str(r.get("estado") or "")
        if solo_match_neon and estado not in ESTADOS_MATCH_NEON:
            continue
        if solo_emitibles and estado not in ESTADOS_EMITIBLES:
            continue
        if conj:
            nombre = str(r.get("conjunto") or r.get("conjunto_nombre") or "").casefold()
            if conj not in nombre and nombre != conj:
                continue
        if q:
            # Unidad/código: igualdad exacta (evita "431" ⊂ "1-431" / "1431").
            # Texto libre: substring sobre titular/archivo/conjunto.
            unidad_vals = [
                str(r.get(k) or "").casefold()
                for k in (
                    "bloque",
                    "apartamento",
                    "clave_canonica",
                    "codigo_cuenta",
                    "torre_apto_neon",
                )
            ]
            texto = " ".join(
                str(r.get(k) or "")
                for k in ("titular_pdf", "titular", "archivo", "conjunto")
            ).casefold()
            if q not in unidad_vals and q not in texto:
                continue
        out.append(i)
    return out


def resumen_sin_match(preview: dict[str, Any]) -> list[dict[str, Any]]:
    """Filas compactas para UI: archivo, unidad PDF, motivo (no son co-propietarios)."""
    out: list[dict[str, Any]] = []
    for r in preview.get("resultados") or []:
        if r.get("estado") != "sin_match":
            continue
        out.append(
            {
                "archivo": r.get("archivo") or "",
                "titular_pdf": r.get("titular_pdf") or "",
                "conjunto": r.get("conjunto") or "",
                "bloque": r.get("bloque") or "",
                "apartamento": r.get("apartamento") or "",
                "clave_canonica": r.get("clave_canonica") or "",
                "codigo_cuenta": r.get("codigo_cuenta") or "",
                "motivo": r.get("motivo") or MOTIVO_SIN_MATCH,
                "motivo_detalle": r.get("motivo_detalle") or r.get("error") or "",
                "criterios": r.get("criterios") or "",
            }
        )
    return out


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
            "advertencias": [],
            "motivo": MOTIVO_OMITIDO,
            "motivo_detalle": "PDF omitido / no parseado",
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
            "advertencias": [],
            "motivo": MOTIVO_SIN_CAPITAL,
            "motivo_detalle": "Sin capital limpio a demandar (Bolsa)",
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
        codigo_cuenta=meta["codigo_cuenta"],
        incluir_propietarios=True,
    )

    if hallado:
        deudores = list(hallado.get("deudores") or hallado.get("propietarios") or [])
        principal = hallado.get("titular_principal") or (
            deudores[0] if deudores else None
        )
        advertencias = list(hallado.get("advertencias") or [])
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
            "advertencias": advertencias,
            "diagnostico_propietarios": hallado.get("diagnostico_propietarios"),
            "regla_multi_deudor": REGLA_MULTI_DEUDOR,
            "motivo": None,
            "motivo_detalle": None,
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
                    "rol": "principal",
                }
            ],
            "titular_seleccionado": {
                "contacto_id": None,
                "nombre": meta["titular_pdf"] or None,
                "cedula": (titular_cedula or "").strip() or None,
                "es_principal": True,
                "rol": "principal",
            },
            "advertencias": [
                "Fallback PDF: sin lista de co-propietarios Neon "
                "(cargue el maestro para relacionar a todos)."
            ],
            "copropiedad_nit": (copropiedad_nit or "").strip() or None,
            "copropiedad_nombre": (copropiedad_nombre or "").strip()
            or meta["conjunto"]
            or None,
            "regla_multi_deudor": REGLA_MULTI_DEUDOR,
            "motivo": None,
            "motivo_detalle": None,
            "error": None,
            "criterios": criterios,
            "datos_neon": None,
        }

    detalle = (
        "No se encontró inmueble/titular en Neon para este conjunto+unidad. "
        "No son co-propietarios: el PDF parseó bien pero el maestro no cruzó. "
        "Active 'Permitir datos del PDF' e indique NIT y cédula, "
        "o cargue el inmueble en Neon."
    )
    return {
        **meta,
        "estado": "sin_match",
        "inmueble_id": None,
        "deudores": [],
        "titular_seleccionado": None,
        "advertencias": [],
        "motivo": MOTIVO_SIN_MATCH,
        "motivo_detalle": detalle,
        "error": detalle,
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
    preview = {
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
    preview["sin_match_detalle"] = resumen_sin_match(preview)
    return preview


def _generar_uno(
    item: dict[str, Any],
    *,
    representante_nombre: str = "",
    representante_cedula: str = "",
    dia_vencimiento: int = 31,
    permitir_datos_pdf: bool = False,
    titular_cedula: str = "",
    copropiedad_nit: str = "",
    copropiedad_nombre: str = "",
    ciudad: str = "",
    incluir_poder: bool = True,
    fmi: str = "",
    edicion: Optional[dict[str, Any]] = None,
) -> tuple[BytesIO, str, dict[str, Any]]:
    """Genera un certificado por unidad (principal + lista completa en contexto)."""
    neon = item.get("datos_neon")
    inmueble_id = item.get("inmueble_id")
    sel = item.get("titular_seleccionado") or {}
    ed = edicion if isinstance(edicion, dict) else {}
    titular_nombre = (
        str(ed.get("titular_nombre") or "").strip()
        or (sel.get("nombre") or item.get("titular_pdf") or "")
    ).strip()
    cedula = (
        str(ed.get("titular_cedula") or "").strip()
        or (sel.get("cedula") or titular_cedula or "")
    ).strip()
    fmi_final = (
        str(ed.get("fmi") or "").strip()
        or (fmi or item.get("fmi") or "")
    ).strip()
    props_override = ed.get("propietarios")
    if not isinstance(props_override, list):
        props_override = None
    conceptos_ord = ed.get("conceptos_ordinaria") or ed.get("conceptos_forzar_ordinaria")
    if isinstance(conceptos_ord, str):
        conceptos_ord = [conceptos_ord]
    if not isinstance(conceptos_ord, list):
        conceptos_ord = None
    forzar = bool(ed) and (
        bool(ed.get("titular_nombre"))
        or bool(ed.get("titular_cedula"))
        or bool(props_override)
        or bool(ed.get("forzar"))
    )

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
        incluir_poder=incluir_poder,
        fmi=fmi_final,
        forzar_datos_titular=forzar,
        conceptos_forzar_ordinaria=conceptos_ord,
        propietarios_override=props_override,
    )


def generar_certificados_desde_preview(
    preview: dict[str, Any],
    *,
    representante_nombre: str = "",
    representante_cedula: str = "",
    dia_vencimiento: int = 31,
    permitir_datos_pdf: bool = False,
    titular_cedula: str = "",
    copropiedad_nit: str = "",
    copropiedad_nombre: str = "",
    ciudad: str = "",
    indices: Optional[list[int]] = None,
    incluir_poder: bool = True,
    fmi_por_indice: Optional[dict[int, str]] = None,
    edicion_por_indice: Optional[dict[int, dict[str, Any]]] = None,
) -> tuple[BytesIO, str, dict[str, Any]]:
    """
    Emite Word(s) para resultados emitibles del preview.

    - 1 certificado → docx suelto
    - varios → zip `Certificados_deuda.zip`
    Regla: un Word por cuenta/unidad (todos los propietarios relacionados
    en contexto; destinatario plantilla = principal).

    Antefirma (`representante_*`) es obligatoria y se aplica a **todos**
    los Word del lote; si falta, no se genera ningún archivo.

    `incluir_poder`: append del poder en el mismo .docx (default True).
    `fmi_por_indice`: mapa índice del preview → FMI (texto). Vacío permitido;
    la plantilla omite el fragmento FMI si no hay valor.
    `edicion_por_indice`: overrides phasecob (nombre/cédula/propietarios/
    conceptos→ordinaria / fmi) por índice de preview.
    """
    # Early-return: no zip/docx parciales sin antefirma del RL.
    rl_nombre, rl_cedula = validar_antefirma_representante(
        representante_nombre, representante_cedula
    )

    resultados = list(preview.get("resultados") or [])
    fmi_map = {
        int(k): str(v or "").strip()
        for k, v in (fmi_por_indice or {}).items()
    }
    edicion_map = {
        int(k): (v if isinstance(v, dict) else {})
        for k, v in (edicion_por_indice or {}).items()
    }
    if indices is not None:
        elegidos = [
            (i, resultados[i])
            for i in indices
            if 0 <= i < len(resultados)
        ]
    else:
        elegidos = [
            (i, r)
            for i, r in enumerate(resultados)
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

    for idx, item in elegidos:
        try:
            buf, nombre, meta = _generar_uno(
                item,
                representante_nombre=rl_nombre,
                representante_cedula=rl_cedula,
                dia_vencimiento=dia_vencimiento,
                permitir_datos_pdf=permitir_datos_pdf,
                titular_cedula=titular_cedula,
                copropiedad_nit=copropiedad_nit,
                copropiedad_nombre=copropiedad_nombre,
                ciudad=ciudad,
                incluir_poder=incluir_poder,
                fmi=fmi_map.get(idx, ""),
                edicion=edicion_map.get(idx),
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
        "representante_nombre": rl_nombre,
        "representante_cedula": rl_cedula,
        "incluir_poder": bool(incluir_poder),
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
    dia_vencimiento: int = 31,
    ciudad: str = "",
    indices: Optional[list[int]] = None,
    filtro_conjunto: str = "",
    filtro_busqueda: str = "",
    solo_match_neon: bool = False,
    incluir_poder: bool = True,
    fmi_por_indice: Optional[dict[int, str]] = None,
    edicion_por_indice: Optional[dict[int, dict[str, Any]]] = None,
) -> dict[str, Any] | tuple[BytesIO, str, dict[str, Any]]:
    """
    Punto único: preview (dict) o generar (buffer, filename, meta).

    `modo`:
      - `preview` → JSON con deudores / estados
      - `generar` → docx o zip

    Filtros (`filtro_*` / `solo_match_neon`) aplican al emitir; si además
    hay `indices`, se intersectan (no se mezclan deudores entre cuentas).
    `fmi_por_indice` aplica solo en `generar` (índice del preview → FMI).
    `edicion_por_indice` (phasecob): nombre/cédula/FMI/conceptos por índice.
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

    idxs = indices
    if filtro_conjunto or filtro_busqueda or solo_match_neon:
        filtrados = indices_filtrados_preview(
            preview,
            conjunto=filtro_conjunto,
            busqueda=filtro_busqueda,
            solo_match_neon=solo_match_neon,
            solo_emitibles=True,
        )
        if idxs is None:
            idxs = filtrados
        else:
            allow = set(filtrados)
            idxs = [i for i in idxs if i in allow]

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
        indices=idxs,
        incluir_poder=incluir_poder,
        fmi_por_indice=fmi_por_indice,
        edicion_por_indice=edicion_por_indice,
    )
