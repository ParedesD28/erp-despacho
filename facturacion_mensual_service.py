"""Cargador de facturación mensual COLON: barreras B1–B5 y B7.

Pre-vista en memoria (0 escrituras). Aplicar solo al confirmar.
De la factura se causa `VALOR MES` de cuota ordinaria, extraordinaria o multa.
`Abono último Mes` se lee y se muestra desmarcado; solo se inserta (periodo M-1)
si el abogado marca esa fila. Nunca intereses de mora ni cierre automático
de desfase (p. ej. 25-103). El liquidador calcula la mora.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from threading import Lock
from typing import Any, Optional

from psycopg2.extras import RealDictCursor

import db
import liquidador
from certificados_deuda_repository import (
    _compatible_con_codigo_cuenta,
    _norm_texto,
    _tupla_numerica_unidad,
    _unidad_coincide,
    clave_canonica_unidad,
    _claves_unidad,
)
from facturacion_mensual_parser import parse_facturacion_pdf, periodo_desde_facturas

MAX_PDF_BYTES = 50 * 1024 * 1024

_CACHE_TTL_S = 1800
_CACHE_MAX = 6
_lote_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = Lock()

_MESES_NOM = (
    "",
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)


class FacturacionMensualError(ValueError):
    """Error de negocio del cargador (PDF, cruce, confirmación)."""


def _money(v) -> float:
    if v is None or v == "":
        return 0.0
    return round(float(v), 2)


def _digitos(texto: str) -> str:
    return re.sub(r"\D+", "", str(texto or ""))


def mes_anterior(anio: int, mes: int) -> tuple[int, int]:
    if mes <= 1:
        return anio - 1, 12
    return anio, mes - 1


def etiqueta_periodo(anio: int, mes: int) -> str:
    nombre = _MESES_NOM[mes] if 1 <= mes <= 12 else str(mes)
    return f"{nombre} {anio}"


def normalizar_nit_base(nit_raw: str) -> str:
    """9 dígitos base del NIT colombiano (sin dígito de verificación)."""
    s = str(nit_raw or "").strip()
    if "-" in s:
        s = s.split("-", 1)[0]
    digits = _digitos(s)
    return digits[:9] if len(digits) >= 9 else digits


def extraer_etapa_o_manzana(nombre: str) -> tuple[Optional[str], Optional[str]]:
    """MANZANA / ETAPA explícitos. No se borra ETAPA N (B1)."""
    norm = _norm_texto(nombre)
    m_mz = re.search(r"\b(?:MANZANA|MZ)\s*(\d+[A-Z]?)\b", norm)
    m_et = re.search(r"\b(?:ETAPA|ET)\s*(\d+[A-Z]?)\b", norm)
    return (m_mz.group(1) if m_mz else None, m_et.group(1) if m_et else None)


def cruzar_conjunto_estricto(
    copropiedad_pdf: str, nit_pdf: str, conjuntos_neon: list[dict]
) -> dict:
    """B1: NIT base + mismo número de Etapa/Manzana. Prohibido borrar ETAPA N."""
    nit_base_pdf = normalizar_nit_base(nit_pdf)
    mz_pdf, et_pdf = extraer_etapa_o_manzana(copropiedad_pdf)

    candidatos_nit = []
    for c in conjuntos_neon:
        nit_base_neon = normalizar_nit_base(c.get("pj_nit") or "")
        if nit_base_pdf and nit_base_neon and nit_base_pdf == nit_base_neon:
            candidatos_nit.append(c)

    if len(candidatos_nit) == 1:
        elegido = candidatos_nit[0]
        mz_neon, et_neon = extraer_etapa_o_manzana(elegido.get("conjunto_nombre") or "")
        if (mz_pdf and mz_neon and mz_pdf != mz_neon) or (
            et_pdf and et_neon and et_pdf != et_neon
        ):
            return {
                "ok": False,
                "motivo": "CONFLICTO_ETAPA_MANZANA_CON_MISMO_NIT",
                "candidato": elegido,
            }
        return {"ok": True, "metodo": "NIT_EXACTO", "conjunto": elegido}

    pool = candidatos_nit if candidatos_nit else conjuntos_neon
    filtrados = []
    for c in pool:
        mz_neon, et_neon = extraer_etapa_o_manzana(c.get("conjunto_nombre") or "")
        if mz_pdf and mz_neon and mz_pdf != mz_neon:
            continue
        if et_pdf and et_neon and et_pdf != et_neon:
            continue
        if (mz_pdf and not mz_neon) or (et_pdf and not et_neon):
            continue
        if _norm_texto(c.get("conjunto_nombre") or "") == _norm_texto(copropiedad_pdf):
            filtrados.append(c)
    if len(filtrados) == 1:
        return {"ok": True, "metodo": "NOMBRE_Y_ETAPA_EXACTOS", "conjunto": filtrados[0]}
    return {
        "ok": False,
        "motivo": "SIN_MATCH_UNIVOCO_CONJUNTO",
        "candidatos": len(filtrados),
    }


def cruzar_unidad_en_conjunto(
    unidad_dir: str,
    ref_pago: str,
    inmuebles_conjunto: list[dict],
) -> Optional[dict]:
    """B2: llave canónica bloque-apto dentro del conjunto. Sin fallback por nombre.

    Si el PDF trae estructura compuesta (Bloque B + Apto A → `B-A`), Neon debe
    coincidir por tupla numérica. REF.PAGO solo valida; nunca cruza `2-01` con `201`.
    """
    canon = clave_canonica_unidad(unidad_dir or "")
    claves_dir = _claves_unidad(unidad_dir or "")
    tupla = _tupla_numerica_unidad(canon) if canon else None
    es_compuesta = tupla is not None
    matches: list[dict] = []
    for inm in inmuebles_conjunto:
        torre = str(inm.get("torre_apto") or "")
        if not torre:
            continue
        if es_compuesta:
            if not _unidad_coincide(torre, claves_dir):
                continue
            if ref_pago and not _compatible_con_codigo_cuenta(torre, ref_pago):
                continue
            matches.append(inm)
        else:
            if _unidad_coincide(torre, claves_dir):
                matches.append(inm)
    if len(matches) == 1:
        return matches[0]
    return None


def clasificar_cargo_mes_recibo(concepto_pdf: str) -> dict:
    """B5: clasifica únicamente el VALOR MES de una línea del recibo."""
    norm = _norm_texto(concepto_pdf)
    tokens = set(norm.split())
    if "INTERES" in norm or "INTERESES" in tokens or "MORA" in tokens:
        return {
            "accion": "IGNORAR_SIEMPRE",
            "concepto_erp": None,
            "requiere_confirmacion": False,
            "motivo": "El interés lo calcula el liquidador con la tasa oficial Superfinanciera",
        }
    if any(k in tokens for k in ("ABONO", "ABONOS", "ANTICIPO", "ANTICIPOS", "ADELANTO", "PAGO", "PAGOS")):
        return {
            "accion": "IGNORAR_SIEMPRE",
            "concepto_erp": None,
            "requiere_confirmacion": False,
            "motivo": "El VALOR MES de una línea de abono/anticipo no se carga como cuota; el pago del pie se propone aparte",
        }
    if (
        ("CUOTA" in tokens or "APORTE" in tokens or "EXPENSA" in tokens)
        and ("ADMINISTRACION" in tokens or "ADMON" in tokens)
        and "EXTRA" not in norm
        and "RETROACTIVO" not in norm
    ):
        return {
            "accion": "CARGAR_CAPITAL",
            "concepto_erp": "Expensa Ordinaria",
            "requiere_confirmacion": False,
            "motivo": "Cuota ordinaria del mes facturado (solo VALOR MES)",
        }
    if any(k in norm for k in ("SANCION", "MULTA", "PENALIDAD")):
        return {
            "accion": "CARGAR_CON_CONFIRMACION",
            "concepto_erp": "Cuota Extraordinaria",
            "requiere_confirmacion": True,
            "motivo": "Sanción/multa nueva del mes: el abogado debe marcarla (Ley 675)",
        }
    if any(k in norm for k in ("HONORARIO", "PREJURIDICO", "ABOGADO", "COBRO JURIDICO")):
        return {
            "accion": "IGNORAR_SIEMPRE",
            "concepto_erp": None,
            "requiere_confirmacion": False,
            "motivo": "Honorarios/cobro jurídico de la administración: el ERP ya liquida el 23.8%",
        }
    return {
        "accion": "CARGAR_CAPITAL",
        "concepto_erp": "Cuota Extraordinaria",
        "requiere_confirmacion": False,
        "motivo": "Cargo extraordinario causado en el mes (VALOR MES > 0)",
    }


def ultima_ordinaria(expensas: list[dict]) -> Optional[tuple[int, int, float]]:
    mejor = None
    for e in expensas or []:
        if str(e.get("concepto") or "") != "Expensa Ordinaria":
            continue
        y, m = int(e.get("periodo_anio") or 0), int(e.get("periodo_mes") or 0)
        if mejor is None or (y, m) > (mejor[0], mejor[1]):
            mejor = (y, m, _money(e.get("valor_capital")))
    return mejor


def evaluar_elegibilidad(
    *,
    proceso_estado: str,
    obligacion_estado: str,
    expensas: list[dict],
    periodo_anio: int,
    periodo_mes: int,
) -> list[str]:
    """B4: bloquea proceso inactivo y falta de historial / hueco cronológico."""
    bloqueos: list[str] = []
    ob = (obligacion_estado or "ACTIVA").strip().upper()
    if ob in ("CANCELADA", "ANULADA", "INACTIVA", "INACTIVO", "TERMINADA"):
        bloqueos.append(f"Obligación PH en estado {ob}: no se carga la factura suelta.")
    proc = (proceso_estado or "").strip().upper()
    if proc != "ACTIVO":
        etiqueta = proceso_estado or "sin proceso"
        bloqueos.append(
            f"Proceso {etiqueta}: no se toca. Solo se cargan unidades con proceso Activo."
        )
    if not expensas:
        bloqueos.append(
            f"Falta historial base hasta {etiqueta_periodo(*mes_anterior(periodo_anio, periodo_mes))}; "
            "cargue primero el Certificado de Deuda / Estado de Cuenta."
        )
        return bloqueos
    ultima = ultima_ordinaria(expensas)
    if ultima is None:
        bloqueos.append(
            "No hay Expensa Ordinaria previa: cargue primero el Certificado de Deuda / Estado de Cuenta."
        )
        return bloqueos
    u_anio, u_mes, _ = ultima
    prev_a, prev_m = mes_anterior(periodo_anio, periodo_mes)
    if (u_anio, u_mes) == (periodo_anio, periodo_mes):
        return bloqueos
    if (u_anio, u_mes) == (prev_a, prev_m):
        return bloqueos
    bloqueos.append(
        f"La última cuota ordinaria en Neon es {u_anio}-{u_mes:02d}; para cargar "
        f"{periodo_anio}-{periodo_mes:02d} hace falta historial hasta {prev_a}-{prev_m:02d}. "
        "Cargue primero el Certificado / Estado de Cuenta (anti-hueco)."
    )
    return bloqueos


_CONCEPTOS_CAUSABLES = frozenset({"Expensa Ordinaria", "Cuota Extraordinaria"})


def hay_abono_igual(expensas: list[dict], anio: int, mes: int, monto: float) -> bool:
    for e in expensas or []:
        if str(e.get("concepto") or "") != "Abono":
            continue
        if int(e.get("periodo_anio") or 0) != anio or int(e.get("periodo_mes") or 0) != mes:
            continue
        if abs(_money(e.get("valor_capital")) - _money(monto)) < 0.01:
            return True
    return False


def abono_del_periodo(expensas: list[dict], anio: int, mes: int) -> Optional[float]:
    for e in expensas or []:
        if str(e.get("concepto") or "") != "Abono":
            continue
        if int(e.get("periodo_anio") or 0) == anio and int(e.get("periodo_mes") or 0) == mes:
            return _money(e.get("valor_capital"))
    return None


def detectar_abono_ultimo_mes(
    inv: dict,
    expensas: list[dict],
    periodo_anio: int,
    periodo_mes: int,
) -> Optional[dict]:
    """Lee `Abono último Mes` y lo propone como Abono de M-1, sin insertarlo.

    Checkbox desmarcado. No hay abono de cierre ni imputación automática.
    """
    monto = _money(inv.get("abono_ultimo_mes"))
    if monto <= 0:
        return None
    prev_a, prev_m = mes_anterior(periodo_anio, periodo_mes)
    periodo = f"{prev_a}-{prev_m:02d}"
    base = {
        "monto": monto,
        "periodo_anio": prev_a,
        "periodo_mes": prev_m,
        "periodo": periodo,
        "marcado_por_defecto": False,
        "inmueble_id": None,
    }
    if hay_abono_igual(expensas, prev_a, prev_m, monto):
        return {
            **base,
            "accion": "NO_OP_YA_EXISTE",
            "requiere_confirmacion": False,
            "motivo": f"Ya existe un Abono de ${monto:,.0f} en {periodo}; no se duplica.",
        }
    existente = abono_del_periodo(expensas, prev_a, prev_m)
    if existente is not None and abs(existente - monto) >= 0.01:
        return {
            **base,
            "accion": "NO_PISAR",
            "requiere_confirmacion": False,
            "motivo": (
                f"Ya hay un abono de ${existente:,.0f} en {periodo}; la factura trae "
                f"${monto:,.0f}. No se pisa."
            ),
        }
    return {
        **base,
        "accion": "PROPONER_ABONO_MES_ANTERIOR",
        "requiere_confirmacion": True,
        "motivo": (
            f"Abono último Mes ${monto:,.0f} → Abono {periodo} "
            "(mes anterior a la factura). Desmarcado: márquelo para registrarlo."
        ),
    }


def cargos_desde_factura(inv: dict, expensas: list[dict], anio: int, mes: int) -> list[dict]:
    """Solo VALOR MES > 0 de cuota ordinaria, extraordinaria o multa.

    Nunca intereses de mora. El abono del pie no entra por esta vía.
    """
    cargos: list[dict] = []
    for c in inv.get("conceptos") or []:
        valor_mes = _money(c.get("valor_mes"))
        if valor_mes <= 0:
            continue
        clasif = clasificar_cargo_mes_recibo(str(c.get("concepto") or ""))
        if clasif.get("concepto_erp") not in _CONCEPTOS_CAUSABLES:
            continue
        actual = None
        for e in expensas or []:
            if (
                str(e.get("concepto") or "") == clasif["concepto_erp"]
                and int(e.get("periodo_anio") or 0) == anio
                and int(e.get("periodo_mes") or 0) == mes
            ):
                actual = _money(e.get("valor_capital"))
                break
        if actual is None:
            op = "INSERT"
        elif abs(actual - valor_mes) < 0.01:
            op = "NO_OP"
        else:
            op = "UPDATE"
        cargos.append(
            {
                "concepto_pdf": c.get("concepto"),
                "concepto_erp": clasif["concepto_erp"],
                "valor_mes": valor_mes,
                "valor_neon": actual,
                "operacion": op,
                "requiere_confirmacion": bool(clasif.get("requiere_confirmacion")),
                "motivo": clasif.get("motivo") or "",
            }
        )
    return cargos


def cedulas_neon(unidad: dict) -> set[str]:
    out: set[str] = set()
    if unidad.get("contacto_cedula"):
        d = _digitos(unidad["contacto_cedula"])
        if d:
            out.add(d)
    for p in unidad.get("propietarios") or []:
        d = _digitos(p.get("identificacion") or "")
        if d:
            out.add(d)
    return out


def evaluar_unidad(
    inv: dict,
    unidad: dict,
    *,
    periodo_anio: int,
    periodo_mes: int,
) -> dict:
    expensas = unidad.get("expensas") or []
    ob = (unidad.get("obligaciones") or [{}])[0] if unidad.get("obligaciones") else {}
    bloqueos = evaluar_elegibilidad(
        proceso_estado=str(ob.get("proceso_estado") or ""),
        obligacion_estado=str(ob.get("obligacion_estado") or "ACTIVA"),
        expensas=expensas,
        periodo_anio=periodo_anio,
        periodo_mes=periodo_mes,
    )
    alertas: list[str] = []
    ced_pdf = _digitos(inv.get("propietario_identificacion") or "")
    ceds = cedulas_neon(unidad)
    cedula_coincide = bool(ced_pdf and ced_pdf in ceds)
    if not cedula_coincide:
        alertas.append(
            "DISCREPANCIA_TITULAR_PDF: en COLON figura "
            f"{inv.get('propietario_nombre')} (CC {ced_pdf or '—'}) y en Neon "
            f"{unidad.get('contacto_nombre')} (CC {', '.join(sorted(ceds)) or '—'}). "
            "No se cambia el titular de Neon."
        )

    cargos = cargos_desde_factura(inv, expensas, periodo_anio, periodo_mes)
    abono = detectar_abono_ultimo_mes(inv, expensas, periodo_anio, periodo_mes)
    if abono:
        abono["inmueble_id"] = unidad.get("inmueble_id")
        abono["torre_apto"] = unidad.get("torre_apto")
    ultima = ultima_ordinaria(expensas)
    if ultima and cargos:
        u_anio, u_mes, u_val = ultima
        for cg in cargos:
            if cg["concepto_erp"] == "Expensa Ordinaria" and abs(u_val - cg["valor_mes"]) > 0.01:
                if (u_anio, u_mes) != (periodo_anio, periodo_mes):
                    alertas.append(
                        f"En Neon {u_anio}-{u_mes:02d} figura ${u_val:,.0f} y la factura "
                        f"oficial del mes es ${cg['valor_mes']:,.0f}."
                    )

    if bloqueos:
        estado = "BLOQUEADO"
    elif alertas:
        estado = "APLICABLE_CON_ALERTA"
    else:
        estado = "APLICABLE"

    return {
        "inmueble_id": unidad.get("inmueble_id"),
        "torre_apto": unidad.get("torre_apto"),
        "obligation_id": ob.get("obligacion_id"),
        "radicado": ob.get("radicado_interno"),
        "proceso_estado": ob.get("proceso_estado"),
        "factura_no": inv.get("factura_no"),
        "page": inv.get("page"),
        "half": inv.get("half"),
        "titular_neon": unidad.get("contacto_nombre"),
        "titular_pdf": inv.get("propietario_nombre"),
        "cedula_pdf": inv.get("propietario_identificacion"),
        "cedula_coincide": cedula_coincide,
        "estado": estado,
        "bloqueos": bloqueos,
        "alertas": alertas,
        "cargos": cargos,
        "abono": abono,
        "ref_pago": inv.get("ref_pago"),
    }


def cruzar_lote(
    invoices: list[dict],
    conjunto: dict,
    unidades_conjunto: list[dict],
    *,
    periodo_anio: int,
    periodo_mes: int,
) -> dict:
    inm_list = list(unidades_conjunto)
    nuestras: list[dict] = []
    descartadas = 0
    for inv in invoices:
        match = cruzar_unidad_en_conjunto(
            inv.get("unidad_dir") or "",
            inv.get("ref_pago") or "",
            inm_list,
        )
        if not match:
            descartadas += 1
            continue
        nuestras.append(evaluar_unidad(inv, match, periodo_anio=periodo_anio, periodo_mes=periodo_mes))

    listas = [u for u in nuestras if u["estado"] != "BLOQUEADO"]
    bloqueadas = [u for u in nuestras if u["estado"] == "BLOQUEADO"]
    return {
        "conjunto": {
            "id": conjunto.get("conjunto_id") or conjunto.get("id"),
            "nombre": conjunto.get("conjunto_nombre") or conjunto.get("nombre"),
            "nit": conjunto.get("pj_nit"),
        },
        "periodo_anio": periodo_anio,
        "periodo_mes": periodo_mes,
        "periodo_etiqueta": etiqueta_periodo(periodo_anio, periodo_mes),
        "resumen": {
            "facturas_pdf": len(invoices),
            "descartadas": descartadas,
            "nuestras": len(nuestras),
            "listas": len(listas),
            "bloqueadas": len(bloqueadas),
            "abonos_detectados": sum(
                1 for u in nuestras if (u.get("abono") or {}).get("monto")
            ),
        },
        "unidades": sorted(nuestras, key=lambda x: int(x.get("inmueble_id") or 0)),
    }


def _guardar_cache(payload: dict) -> str:
    lote_id = secrets.token_urlsafe(16)
    ahora = time.time()
    with _cache_lock:
        vencidos = [k for k, (exp, _) in _lote_cache.items() if exp <= ahora]
        for k in vencidos:
            _lote_cache.pop(k, None)
        while len(_lote_cache) >= _CACHE_MAX:
            oldest = min(_lote_cache.items(), key=lambda kv: kv[1][0])[0]
            _lote_cache.pop(oldest, None)
        _lote_cache[lote_id] = (ahora + _CACHE_TTL_S, payload)
    return lote_id


def obtener_lote(lote_id: str) -> Optional[dict]:
    if not lote_id:
        return None
    ahora = time.time()
    with _cache_lock:
        item = _lote_cache.get(lote_id)
        if not item:
            return None
        exp, payload = item
        if exp <= ahora:
            _lote_cache.pop(lote_id, None)
            return None
        return payload


def cargar_conjuntos(cur) -> list[dict]:
    cur.execute(
        """
        SELECT cr.id AS conjunto_id, cr.nombre AS conjunto_nombre,
               c.identificacion AS pj_nit, c.nombre AS pj_nombre
          FROM conjuntos_residenciales cr
          LEFT JOIN contactos c ON c.id = cr.contacto_id
         ORDER BY cr.id
        """
    )
    return [dict(r) for r in cur.fetchall()]


def cargar_unidades_conjunto(cur, conjunto_id: int) -> list[dict]:
    cur.execute(
        """
        SELECT i.id AS inmueble_id, i.torre_apto, i.conjunto_id, i.conjunto_residencial,
               i.contacto_id, c.nombre AS contacto_nombre, c.identificacion AS contacto_cedula
          FROM inmuebles_ph i
          LEFT JOIN contactos c ON c.id = i.contacto_id
         WHERE i.conjunto_id = %s
         ORDER BY i.id
        """,
        (int(conjunto_id),),
    )
    unidades = [dict(r) for r in cur.fetchall()]
    for u in unidades:
        inm_id = int(u["inmueble_id"])
        cur.execute(
            """
            SELECT ip.contacto_id, ip.es_principal, c.nombre, c.identificacion
              FROM inmueble_propietarios ip
              JOIN contactos c ON c.id = ip.contacto_id
             WHERE ip.inmueble_id = %s
             ORDER BY ip.es_principal DESC, c.id
            """,
            (inm_id,),
        )
        u["propietarios"] = [dict(p) for p in cur.fetchall()]
        cur.execute(
            """
            SELECT o.id AS obligacion_id, o.estado AS obligacion_estado,
                   po.radicado_interno, p.estado AS proceso_estado,
                   p.etapa_actual AS proceso_etapa
              FROM obligaciones o
              LEFT JOIN proceso_obligaciones po ON po.obligacion_id = o.id
              LEFT JOIN procesos p ON p.radicado_interno = po.radicado_interno
             WHERE o.inmueble_id = %s
             ORDER BY o.id DESC
            """,
            (inm_id,),
        )
        u["obligaciones"] = [dict(o) for o in cur.fetchall()]
        cur.execute(
            """
            SELECT id, obligation_id, concepto, periodo_anio, periodo_mes,
                   valor_capital, fecha_vencimiento, estado
              FROM expensas_ph
             WHERE inmueble_id = %s
             ORDER BY periodo_anio, periodo_mes, id
            """,
            (inm_id,),
        )
        u["expensas"] = [dict(e) for e in cur.fetchall()]
    return unidades


def preview_pdf(pdf_bytes: bytes, filename: str = "") -> dict:
    if not pdf_bytes:
        raise FacturacionMensualError("El PDF está vacío.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise FacturacionMensualError("El PDF supera el tamaño máximo (50 MB).")
    invoices = parse_facturacion_pdf(pdf_bytes)
    if not invoices:
        raise FacturacionMensualError("No se leyeron facturas del PDF.")
    periodo_anio, periodo_mes = periodo_desde_facturas(invoices)
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            conjuntos = cargar_conjuntos(cur)
            cruce = cruzar_conjunto_estricto(
                invoices[0].get("copropiedad") or "",
                invoices[0].get("copropiedad_nit") or "",
                conjuntos,
            )
            if not cruce.get("ok"):
                raise FacturacionMensualError(
                    "No se identificó el conjunto con NIT + manzana/etapa. "
                    f"Motivo: {cruce.get('motivo')}. Revise que el PDF sea de este conjunto."
                )
            conjunto = cruce["conjunto"]
            unidades = cargar_unidades_conjunto(cur, int(conjunto["conjunto_id"]))
    finally:
        conn.release()

    preview = cruzar_lote(
        invoices, conjunto, unidades, periodo_anio=periodo_anio, periodo_mes=periodo_mes
    )
    fecha_comp = next((i.get("fecha_comprobante") for i in invoices if i.get("fecha_comprobante")), None)
    fecha_venc = next((i.get("fecha_vencimiento") for i in invoices if i.get("fecha_vencimiento")), None)
    preview["fecha_factura"] = fecha_comp
    preview["fecha_vencimiento"] = fecha_venc
    preview["sha256"] = sha256
    preview["nombre_archivo"] = filename
    preview["metodo_conjunto"] = cruce.get("metodo")

    lote_id = _guardar_cache(
        {
            "pdf_bytes": pdf_bytes,
            "sha256": sha256,
            "filename": filename,
            "invoices": invoices,
            "periodo_anio": periodo_anio,
            "periodo_mes": periodo_mes,
            "conjunto": conjunto,
            "preview": preview,
        }
    )
    preview["lote_id"] = lote_id
    return preview


def _upsert_expensa(cur, *, inmueble_id, obligation_id, concepto, anio, mes, valor) -> str:
    if str(concepto or "") not in _CONCEPTOS_CAUSABLES:
        return "OMITIDO_NO_CAUSABLE"
    cur.execute(
        """
        SELECT id, valor_capital FROM expensas_ph
         WHERE inmueble_id = %s AND concepto = %s
           AND periodo_anio = %s AND periodo_mes = %s
         LIMIT 1
        """,
        (inmueble_id, concepto, anio, mes),
    )
    row = cur.fetchone()
    if row:
        actual = _money(row["valor_capital"] if isinstance(row, dict) else row[1])
        if abs(actual - _money(valor)) < 0.01:
            return "NO_OP"
        cur.execute(
            """
            UPDATE expensas_ph
               SET valor_capital = %s, obligation_id = COALESCE(%s, obligation_id)
             WHERE id = %s
            """,
            (_money(valor), obligation_id, row["id"] if isinstance(row, dict) else row[0]),
        )
        return "UPDATE"
    f_venc = liquidador.fecha_vencimiento_fin_mes(anio, mes).isoformat()
    cur.execute(
        """
        INSERT INTO expensas_ph
            (inmueble_id, obligation_id, concepto, periodo_mes, periodo_anio,
             valor_capital, fecha_vencimiento, estado)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (inmueble_id, obligation_id, concepto, mes, anio, _money(valor), f_venc, "En Mora"),
    )
    return "INSERT"


def _insertar_abono(cur, *, inmueble_id, obligation_id, anio, mes, valor) -> str:
    """Inserta Abono de M-1. Idempotente por inmueble+periodo+monto. Nunca pisa otro monto."""
    monto = _money(valor)
    if monto <= 0:
        return "OMITIDO_SIN_MONTO"
    cur.execute(
        """
        SELECT id, valor_capital FROM expensas_ph
         WHERE inmueble_id = %s AND concepto = %s
           AND periodo_anio = %s AND periodo_mes = %s
         LIMIT 1
        """,
        (inmueble_id, "Abono", anio, mes),
    )
    row = cur.fetchone()
    if row:
        actual = _money(row["valor_capital"] if isinstance(row, dict) else row[1])
        if abs(actual - monto) < 0.01:
            return "NO_OP"
        return "NO_PISAR"
    f_venc = liquidador.fecha_vencimiento_fin_mes(anio, mes).isoformat()
    cur.execute(
        """
        INSERT INTO expensas_ph
            (inmueble_id, obligation_id, concepto, periodo_mes, periodo_anio,
             valor_capital, fecha_vencimiento, estado)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (inmueble_id, obligation_id, "Abono", mes, anio, monto, f_venc, "Pagado"),
    )
    return "INSERT"


def aplicar_lote(
    lote_id: str,
    *,
    usuario_id: str = "",
    confirmar_sanciones: Optional[set] = None,
    confirmar_abonos: Optional[set] = None,
) -> dict:
    lote = obtener_lote(lote_id)
    if not lote:
        raise FacturacionMensualError("La pre-vista venció o no existe. Vuelva a subir el PDF.")
    confirmar_sanciones = confirmar_sanciones or set()
    confirmar_abonos = {int(x) for x in (confirmar_abonos or set())}
    invoices = lote["invoices"]
    periodo_anio = int(lote["periodo_anio"])
    periodo_mes = int(lote["periodo_mes"])
    conjunto = lote["conjunto"]
    conjunto_id = int(conjunto["conjunto_id"])

    conn = db.get_connection()
    aplicadas: list[dict] = []
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                unidades = cargar_unidades_conjunto(cur, conjunto_id)
                plan = cruzar_lote(
                    invoices,
                    conjunto,
                    unidades,
                    periodo_anio=periodo_anio,
                    periodo_mes=periodo_mes,
                )
                for u in plan["unidades"]:
                    if u["estado"] == "BLOQUEADO":
                        continue
                    inm_id = int(u["inmueble_id"])
                    ob_id = u.get("obligation_id")
                    ops: list[dict] = []
                    for cargo in u.get("cargos") or []:
                        if cargo.get("requiere_confirmacion") and inm_id not in confirmar_sanciones:
                            ops.append({**cargo, "aplicado": "OMITIDO_SIN_CONFIRMACION"})
                            continue
                        if cargo.get("operacion") == "NO_OP":
                            ops.append({**cargo, "aplicado": "NO_OP"})
                            continue
                        hecho = _upsert_expensa(
                            cur,
                            inmueble_id=inm_id,
                            obligation_id=ob_id,
                            concepto=cargo["concepto_erp"],
                            anio=periodo_anio,
                            mes=periodo_mes,
                            valor=cargo["valor_mes"],
                        )
                        ops.append({**cargo, "aplicado": hecho})
                    abono = u.get("abono") or {}
                    if abono.get("accion") == "PROPONER_ABONO_MES_ANTERIOR":
                        if inm_id not in confirmar_abonos:
                            ops.append({**abono, "concepto_erp": "Abono", "aplicado": "OMITIDO_SIN_CONFIRMACION"})
                        else:
                            hecho = _insertar_abono(
                                cur,
                                inmueble_id=inm_id,
                                obligation_id=ob_id,
                                anio=int(abono["periodo_anio"]),
                                mes=int(abono["periodo_mes"]),
                                valor=abono["monto"],
                            )
                            ops.append({**abono, "concepto_erp": "Abono", "aplicado": hecho})
                    elif abono.get("accion") in ("NO_OP_YA_EXISTE", "NO_PISAR"):
                        ops.append({**abono, "concepto_erp": "Abono", "aplicado": abono["accion"]})
                    aplicadas.append({"inmueble_id": inm_id, "torre_apto": u["torre_apto"], "ops": ops})

                detalle = {
                    "unidades": plan["unidades"],
                    "aplicadas": aplicadas,
                    "sha256": lote["sha256"],
                    "archivo": lote.get("filename"),
                }
                cur.execute(
                    """
                    INSERT INTO lotes_facturacion_ph
                        (sha256, conjunto_id, periodo_anio, periodo_mes, nombre_archivo,
                         usuario_id, facturas_pdf, unidades_nuestras, unidades_aplicadas,
                         unidades_bloqueadas, detalle)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sha256, conjunto_id, periodo_anio, periodo_mes)
                    DO UPDATE SET
                        usuario_id = EXCLUDED.usuario_id,
                        unidades_aplicadas = EXCLUDED.unidades_aplicadas,
                        unidades_bloqueadas = EXCLUDED.unidades_bloqueadas,
                        detalle = EXCLUDED.detalle,
                        created_at = CURRENT_TIMESTAMP
                    RETURNING id
                    """,
                    (
                        lote["sha256"],
                        conjunto_id,
                        periodo_anio,
                        periodo_mes,
                        lote.get("filename"),
                        str(usuario_id or ""),
                        plan["resumen"]["facturas_pdf"],
                        plan["resumen"]["nuestras"],
                        len(aplicadas),
                        plan["resumen"]["bloqueadas"],
                        json.dumps(detalle, ensure_ascii=False, default=str),
                    ),
                )
                row = cur.fetchone()
                lote_db_id = row["id"] if isinstance(row, dict) else row[0]
    finally:
        conn.release()

    return {
        "ok": True,
        "lote_db_id": lote_db_id,
        "aplicadas": len(aplicadas),
        "bloqueadas": plan["resumen"]["bloqueadas"],
        "periodo": plan["periodo_etiqueta"],
        "conjunto": plan["conjunto"]["nombre"],
        "detalle": aplicadas,
    }
