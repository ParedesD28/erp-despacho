"""Consultas de SOLO LECTURA para "Grok Bot" bajo ``/api/grok/*``.

Autenticación: únicamente ``X-API-Key`` = ``GROK_BOT_API_KEY`` (ninguna otra llave sirve
aquí, ni la heredada). Todas las rutas son GET. Las consultas propias de este módulo se
ejecutan en transacciones ``READ ONLY``; la liquidación (y su PDF) se calcula con
``autocausar=False``, que no escribe ``expensas_ph`` (el motor abre sus propias
conexiones; su único efecto lateral posible es completar la caché de tasas oficiales
``historico_tasas`` si falta un mes).

Rama Judicial: ``/rama/consulta`` y ``/rama/novedades`` consultan con ``LegalAPIClient``
(solo lectura; no escriben en la base ni en la Rama). Nunca se devuelve ni se registra la
URL base ni el detalle de error del cliente: solo mensajes cortos (404 no existe, 502 la
Rama no responde, 503 no configurada). Ver ``docs/GROK_API_README.md``.

Qué NO se devuelve nunca: contraseñas, hashes, tokens, llaves, datos de usuarios del ERP
(abogados/usuarios, perfiles) ni sus nombres. Sí se devuelven datos de deudores y
propietarios (nombre, identificación, teléfono, correo, dirección) porque el bot los
necesita para cobrar; ver PRODUCCION.md.
"""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from psycopg2.extras import RealDictCursor
from starlette.background import BackgroundTask

import external_legal_client
import m2m_keys
import sync_procesos_service as sps
from observability import _json_log
from vencimientos_estado import sql_abierto


def _require_grok_key(request: Request) -> None:
    m2m_keys.require_grok_request(request)


router = APIRouter(
    prefix="/api/grok",
    tags=["Grok Bot (solo lectura)"],
    dependencies=[Depends(_require_grok_key)],
)

_CLAVES_SENSIBLES = (
    "password", "passwd", "hash", "token", "secret", "llave", "api_key", "apikey",
)
_MAX_LIMITE = 500


def _limpiar(valor: Any) -> Any:
    """Quita recursivamente cualquier clave con aspecto de secreto (defensa extra)."""
    if isinstance(valor, dict):
        return {
            k: _limpiar(v)
            for k, v in valor.items()
            if not any(s in str(k).lower() for s in _CLAVES_SENSIBLES)
        }
    if isinstance(valor, (list, tuple)):
        return [_limpiar(v) for v in valor]
    return valor


def _ok(**datos: Any) -> JSONResponse:
    return JSONResponse(jsonable_encoder({"status": "success", **_limpiar(datos)}))


def _elegir(fila: dict | None, campos: tuple[str, ...]) -> dict:
    fila = fila or {}
    return {c: fila.get(c) for c in campos}


@contextmanager
def _lectura() -> Iterator[Any]:
    """Cursor en transacción READ ONLY: cualquier escritura falla en la base de datos."""
    import db

    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            yield cur
    finally:
        try:
            conn.rollback()
        finally:
            conn.release()


def _fecha(texto: str | None, nombre: str, *, futura_ok: bool = True) -> date | None:
    if not texto:
        return None
    try:
        valor = datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{nombre} debe tener formato YYYY-MM-DD")
    if not futura_ok and valor > date.today():
        raise HTTPException(status_code=422, detail=f"{nombre} no puede ser futura")
    return valor


@router.get("/ping")
def ping():
    return _ok(consumidor=m2m_keys.CONSUMER_GROK, modo="lectura")


@router.get("/procesos")
def listar_procesos(
    estado: str = Query("ACTIVOS", max_length=10),
    q: str = Query("", max_length=80),
    limit: int = Query(100, ge=1, le=_MAX_LIMITE),
    offset: int = Query(0, ge=0),
):
    import expedientes_service

    estado = estado.strip().upper()
    if estado not in ("ACTIVOS", "INACTIVOS", "TODOS"):
        raise HTTPException(status_code=422, detail="estado debe ser ACTIVOS, INACTIVOS o TODOS")
    filas = expedientes_service.cargar_procesos_general_sin_duplicados(estado)
    if q.strip():
        n = q.strip().lower()
        filas = [
            f for f in filas
            if n in " ".join(
                str(f.get(c) or "")
                for c in (
                    "radicado_interno", "radicado_rama", "juzgado",
                    "demandante_nombre", "demandante_identificacion",
                    "demandado_nombre", "demandado_identificacion",
                )
            ).lower()
        ]
    campos = (
        "radicado_interno", "radicado_rama", "tipo_cartera", "naturaleza", "juzgado",
        "etapa_actual", "estado", "pretensiones", "medidas_cautelares",
        "demandante_nombre", "demandante_identificacion",
        "demandado_nombre", "demandado_identificacion",
    )
    pagina = [_elegir(f, campos) for f in filas[offset:offset + limit]]
    return _ok(total=len(filas), limit=limit, offset=offset, procesos=pagina)


@router.get("/procesos/{radicado}")
def detalle_proceso(radicado: str):
    import expedientes_service as es
    import obligaciones_service

    radicado = radicado.strip()
    if not radicado or len(radicado) > 60:
        raise HTTPException(status_code=422, detail="radicado inválido")
    with _lectura() as cur:
        proc = es._get_process(cur, radicado)
        if not proc:
            raise HTTPException(status_code=404, detail="Proceso no encontrado")
        demandantes = es._get_demandantes(cur, proc)
        demandados = es._get_demandados(cur, radicado)
        actuaciones = es._get_actuaciones(cur, radicado)[:50]
        obligaciones = obligaciones_service.obtener_obligaciones_proceso(cur, radicado)

    proceso = _elegir(
        proc,
        (
            "radicado_interno", "radicado_rama", "estado_rama", "tipo_cartera",
            "naturaleza", "juzgado", "etapa_actual", "estado", "pretensiones",
            "medidas_cautelares", "fecha_radicacion", "torre_apto",
        ),
    )
    inm = proc.get("inmueble") or {}
    proceso["inmueble"] = _elegir(inm, ("id", "conjunto_residencial", "torre_apto", "conjunto_id"))
    campos_persona = (
        "identificacion", "nombre", "tipo", "telefono", "email", "direccion",
        "ciudad", "es_principal",
    )
    proceso["propietarios_inmueble"] = [
        _elegir(p, ("identificacion", "nombre", "telefono", "email", "es_principal"))
        for p in proc.get("propietarios_inmueble") or []
    ]
    inact = proc.get("inactivacion")
    proceso["inactivacion"] = _elegir(inact, ("motivo", "fecha")) if inact else None
    proceso["demandantes"] = [_elegir(p, campos_persona) for p in demandantes]
    proceso["demandados"] = [_elegir(p, campos_persona) for p in demandados]
    proceso["actuaciones"] = [
        _elegir(a, ("id", "fecha", "etapa", "descripcion", "tipificacion_sugerida"))
        for a in actuaciones
    ]
    proceso["obligaciones"] = [_campos_obligacion(o) for o in obligaciones]
    return _ok(proceso=proceso)


_CAMPOS_OBLIGACION = (
    "id", "tipo_obligacion_codigo", "tipo_obligacion_nombre", "identificacion_deudor",
    "inmueble_id", "numero_documento", "capital_inicial", "fecha_exigibilidad",
    "estado", "fuente_saldo", "es_principal",
)


def _campos_obligacion(fila: dict) -> dict:
    return _elegir(fila, _CAMPOS_OBLIGACION)


@router.get("/procesos/{radicado}/obligaciones")
def obligaciones_de_proceso(radicado: str):
    import obligaciones_service

    radicado = radicado.strip()
    if not radicado or len(radicado) > 60:
        raise HTTPException(status_code=422, detail="radicado inválido")
    with _lectura() as cur:
        filas = obligaciones_service.obtener_obligaciones_proceso(cur, radicado)
    return _ok(radicado_interno=radicado, obligaciones=[_campos_obligacion(f) for f in filas])


@router.get("/obligaciones/{obligacion_id}/saldo")
def saldo_de_obligacion(
    obligacion_id: int,
    fecha_corte: str | None = Query(None, description="YYYY-MM-DD, no futura"),
):
    import obligacion_saldo_service

    corte = _fecha(fecha_corte, "fecha_corte", futura_ok=False)
    saldo = obligacion_saldo_service.calcular_saldo_obligacion(obligacion_id, fecha_corte=corte)
    return _ok(saldo=saldo)


@router.get("/vencimientos")
def listar_vencimientos(
    desde: str | None = Query(None, description="YYYY-MM-DD"),
    hasta: str | None = Query(None, description="YYYY-MM-DD"),
    incluir_completados: bool = False,
    radicado: str | None = Query(None, max_length=60),
    limit: int = Query(100, ge=1, le=_MAX_LIMITE),
):
    f_desde = _fecha(desde, "desde")
    f_hasta = _fecha(hasta, "hasta")
    filtros = ["COALESCE(v.anulado, FALSE) = FALSE"]
    params: list[Any] = []
    if not incluir_completados:
        filtros.append(sql_abierto("v"))
    if f_desde:
        filtros.append("v.fecha_vencimiento >= %s")
        params.append(f_desde)
    if f_hasta:
        filtros.append("v.fecha_vencimiento <= %s")
        params.append(f_hasta)
    if radicado:
        filtros.append("v.radicado_interno = %s")
        params.append(radicado.strip())
    params.append(limit)
    with _lectura() as cur:
        cur.execute(
            f"""
            SELECT v.id, v.radicado_interno, v.obligacion_id, v.inmueble_id, v.titulo,
                   v.fecha_vencimiento, v.estado, v.tipo, v.categoria, v.valor,
                   v.observaciones
            FROM vencimientos v
            WHERE {' AND '.join(filtros)}
            ORDER BY v.fecha_vencimiento ASC, v.id ASC
            LIMIT %s
            """,
            params,
        )
        filas = [dict(r) for r in cur.fetchall()]
    return _ok(total=len(filas), vencimientos=filas)


@router.get("/acuerdos")
def listar_acuerdos(
    estado: str | None = Query(None, max_length=30),
    inmueble_id: int | None = None,
    obligacion_id: int | None = None,
    limit: int = Query(100, ge=1, le=_MAX_LIMITE),
):
    filtros = ["UPPER(COALESCE(a.estado,'PENDIENTE')) NOT IN ('ANULADO','ELIMINADO')"]
    params: list[Any] = []
    if estado:
        filtros.append("UPPER(COALESCE(a.estado,'PENDIENTE')) = %s")
        params.append(estado.strip().upper())
    if inmueble_id is not None:
        filtros.append("a.inmueble_id = %s")
        params.append(inmueble_id)
    if obligacion_id is not None:
        filtros.append("a.obligacion_id = %s")
        params.append(obligacion_id)
    params.append(limit)
    with _lectura() as cur:
        cur.execute(
            f"""
            SELECT a.id, a.inmueble_id, a.obligacion_id, a.identificacion_deudor,
                   a.nombre_deudor, a.telefono, a.valor_acordado, a.numero_cuotas,
                   a.cuota_actual, a.fecha_compromiso, a.estado, a.origen,
                   a.observaciones, i.conjunto_residencial, i.torre_apto
            FROM acuerdos_pago a
            LEFT JOIN inmuebles_ph i ON i.id = a.inmueble_id
            WHERE {' AND '.join(filtros)}
            ORDER BY a.fecha_compromiso DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            params,
        )
        filas = [dict(r) for r in cur.fetchall()]
    return _ok(total=len(filas), acuerdos=filas)


@router.get("/conjuntos")
def listar_conjuntos_residenciales(activos: bool = True):
    import catalogos_service

    with _lectura() as cur:
        filas = catalogos_service.listar_conjuntos(cur, activos=activos)
    campos = (
        "id", "nombre", "nit", "persona_juridica", "telefono", "email", "direccion",
        "ciudad", "unidades_count",
    )
    return _ok(conjuntos=[_elegir(f, campos) for f in filas])


@router.get("/conjuntos/{conjunto_id}/unidades")
def unidades_de_conjunto(conjunto_id: int):
    import catalogos_service

    with _lectura() as cur:
        if not catalogos_service.obtener_conjunto(cur, conjunto_id):
            raise HTTPException(status_code=404, detail="Conjunto no encontrado")
        unidades = catalogos_service.listar_unidades_conjunto(cur, conjunto_id)
    return _ok(conjunto_id=conjunto_id, unidades=unidades)


def _calcular_liquidacion(
    inmueble_id: int, fecha_corte: date, obligacion_id: int | None
) -> tuple[list[dict], dict, tuple]:
    """Liquidación PH sin autocausación (no escribe ``expensas_ph``)."""
    import liquidador

    try:
        resultados, resumen, inm_info = liquidador.motor_calculo_judicial(
            inmueble_id, "usura", 2.5, 23.8, 0.0, fecha_corte,
            autocausar=False, obligacion_id=obligacion_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[GROK LIQUIDACION] error: {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="No fue posible calcular la liquidación")
    if not inm_info:
        raise HTTPException(status_code=404, detail="No existe información del inmueble")
    if not resultados:
        raise HTTPException(status_code=404, detail="No hay movimientos para liquidar")
    return resultados, resumen, inm_info


@router.get("/liquidacion")
def calcular_liquidacion(
    inmueble_id: int,
    fecha_corte: str,
    obligacion_id: int | None = None,
    detalle: bool = False,
):
    """Calcula la liquidación PH sin escribir (sin autocausación y sin PDF)."""
    import bot_api

    corte = _fecha(fecha_corte, "fecha_corte", futura_ok=False)
    resultados, resumen, inm_info = _calcular_liquidacion(inmueble_id, corte, obligacion_id)
    tasas, fuente = bot_api._resumen_tasas(resultados)
    datos = {
        "inmueble_id": inmueble_id,
        "obligacion_id": obligacion_id,
        "fecha_corte": corte,
        "deudor": inm_info[2],
        "identificacion": inm_info[3],
        "capital": float(resumen["capital"]),
        "intereses": float(resumen["intereses"]),
        "honorarios_pct": float(resumen["honorarios_pct"]),
        "honorarios": float(resumen["honorarios"]),
        "gastos": float(resumen["gastos"]),
        "gran_total": float(resumen["gran_total"]),
        "tasas_aplicadas": tasas,
        "fuente_tasas": fuente,
    }
    if detalle:
        datos["detalle_mensual"] = resultados
    return _ok(datos=datos)


def _borrar_archivo(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@router.get("/liquidacion/pdf")
def liquidacion_pdf(inmueble_id: int, fecha_corte: str, obligacion_id: int | None = None):
    """PDF de la liquidación, calculado sin escribir en la base y devuelto directo.

    El archivo temporal se borra después de enviarlo.
    """
    import exportaciones

    if not fecha_corte.strip():
        raise HTTPException(status_code=422, detail="fecha_corte es obligatoria (YYYY-MM-DD)")
    corte = _fecha(fecha_corte.strip(), "fecha_corte", futura_ok=False)
    resultados, resumen, inm_info = _calcular_liquidacion(inmueble_id, corte, obligacion_id)
    try:
        pdf_path = exportaciones.generar_pdf_liquidacion(
            inmueble_id, corte, resultados, resumen, inm_info
        )
    except Exception as exc:
        print(f"[GROK LIQUIDACION PDF] error: {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="No fue posible generar el PDF")
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"Liquidacion_{inmueble_id}_{corte.isoformat()}.pdf",
        headers={"Cache-Control": "private, no-store"},
        background=BackgroundTask(_borrar_archivo, str(pdf_path)),
    )


# --- Rama Judicial (solo lectura, vía LegalAPIClient) -------------------------
#
# Reglas: nunca se devuelve ni se registra LEGAL_API_BASE_URL, cabeceras ni el detalle
# de error del cliente (puede traer base_path/fragmentos de la respuesta externa): solo
# mensajes cortos propios. Ni la Rama ni la base de datos se escriben.

_RADICADO_RAMA_RE = re.compile(r"^\d{23}$")
_RADICADO_INTERNO_RE = re.compile(r"^[\w.\-/]{1,60}$")
_FECHA_DIA_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

_MSG_NO_RESPONDE = "La Rama Judicial no responde"
_MSG_TIEMPO = "La Rama Judicial no respondió a tiempo"
_MSG_NO_ENCONTRADO = "Proceso no encontrado en la Rama Judicial"
_MSG_NO_CONFIGURADA = "Consulta a la Rama Judicial no configurada"


def _env_numero(nombre: str, defecto: float, minimo: float, maximo: float) -> float:
    try:
        valor = float(os.getenv(nombre, "") or defecto)
    except ValueError:
        valor = defecto
    return max(minimo, min(maximo, valor))


def _timeout_consulta_s() -> float:
    return _env_numero("GROK_RAMA_TIMEOUT_S", 30.0, 1.0, 120.0)


def _timeout_total_s() -> float:
    return _env_numero("GROK_RAMA_TOTAL_TIMEOUT_S", 100.0, 5.0, 600.0)


def _concurrencia() -> int:
    return int(_env_numero("GROK_RAMA_CONCURRENCY", 3, 1, 5))


def _dia(valor: Any) -> Any:
    """Recorta fechas ISO con hora a AAAA-MM-DD (igual que la herramienta de consulta)."""
    if isinstance(valor, str):
        m = _FECHA_DIA_RE.match(valor.strip())
        if m:
            return m.group(1)
    return valor


def _actuacion_json(a: Any) -> dict:
    d = a.model_dump()
    return {
        "id_actuacion": d.get("id_actuacion"),
        "fecha_actuacion": _dia(d.get("fecha_actuacion")),
        "actuacion": d.get("actuacion"),
        "anotacion": d.get("anotacion"),
        "inicio_termino": _dia(d.get("inicio_termino")),
        "fin_termino": _dia(d.get("fin_termino")),
        "fecha_registro": _dia(d.get("fecha_registro")),
        "con_documentos": d.get("con_documentos"),
    }


def _fecha_orden(a: dict) -> str:
    return str(a.get("fecha_actuacion") or "")


def _error_rama(exc: BaseException) -> HTTPException:
    """Traduce cualquier fallo del cliente a un mensaje corto y seguro."""
    if isinstance(exc, HTTPException):
        if exc.status_code == 404:
            return HTTPException(status_code=404, detail=_MSG_NO_ENCONTRADO)
        if exc.status_code == 503:
            return HTTPException(status_code=503, detail=_MSG_NO_CONFIGURADA)
    detalle = _MSG_TIEMPO if isinstance(exc, asyncio.TimeoutError) else _MSG_NO_RESPONDE
    _json_log(
        "WARNING",
        "grok_rama_error",
        tipo=type(exc).__name__,
        estado=getattr(exc, "status_code", None),
    )
    return HTTPException(status_code=502, detail=detalle)


async def _con_limite(coro: Any) -> Any:
    return await asyncio.wait_for(coro, timeout=_timeout_consulta_s())


def _resolver_radicado(radicado: str) -> tuple[str, str | None]:
    """Devuelve (radicado de 23 dígitos, radicado_interno o None).

    Acepta el radicado de 23 dígitos (con separadores opcionales) o el radicado
    interno del ERP (p. ej. EXP-0001), que se resuelve en ``procesos``.
    """
    compacto = re.sub(r"[\s.\-]", "", radicado)
    if _RADICADO_RAMA_RE.match(compacto):
        return compacto, None
    with _lectura() as cur:
        cur.execute(
            """
            SELECT radicado_interno, radicado_rama
            FROM procesos
            WHERE UPPER(radicado_interno) = UPPER(%s)
            LIMIT 1
            """,
            (radicado,),
        )
        fila = cur.fetchone()
    if not fila:
        raise HTTPException(status_code=404, detail="Proceso no encontrado")
    rama = sps.normalizar_radicado_rama(fila.get("radicado_rama"))
    if not _RADICADO_RAMA_RE.match(rama):
        raise HTTPException(
            status_code=404, detail="El proceso no tiene radicado de la Rama de 23 dígitos"
        )
    return rama, str(fila.get("radicado_interno") or "")


@router.get("/rama/consulta")
async def rama_consulta(
    radicado: str = Query(..., min_length=1, max_length=80, description="23 dígitos o EXP-0001"),
    limite_actuaciones: int = Query(200, ge=1, le=1000),
):
    """Detalle, actuaciones y sujetos de un proceso en la Rama Judicial (solo lectura)."""
    pedido = radicado.strip()
    if not _RADICADO_INTERNO_RE.match(re.sub(r"\s", "", pedido)):
        raise HTTPException(status_code=422, detail="radicado inválido")
    radicado_rama, interno = await run_in_threadpool(_resolver_radicado, pedido)
    try:
        async with external_legal_client.LegalAPIClient() as client:
            id_interno = await _con_limite(client.consultar_id_por_radicado(radicado_rama))
            detalle = await _con_limite(client.obtener_detalle(id_interno))
            actuaciones = await _con_limite(client.obtener_actuaciones(id_interno))
            sujetos = await _con_limite(client.obtener_sujetos(id_interno))
    except Exception as exc:
        raise _error_rama(exc) from None
    acts = sorted((_actuacion_json(a) for a in actuaciones), key=_fecha_orden, reverse=True)
    return _ok(
        radicado=radicado_rama,
        radicado_interno=interno,
        id_interno=id_interno,
        detalle=detalle.model_dump(),
        total_actuaciones=len(acts),
        actuaciones=acts[:limite_actuaciones],
        sujetos=[s.model_dump() for s in sujetos],
    )


def _tabla_existe(cur: Any, nombre: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{nombre}",))
    fila = cur.fetchone() or {}
    return bool(fila.get("ok"))


def _cargar_procesos_y_registros(
    estado: str, limit: int, offset: int
) -> tuple[int, list[dict], dict[str, dict]]:
    """Procesos con radicado de la Rama + lo que el ERP ya tiene registrado (solo SELECT)."""
    filtro_estado = {
        "ACTIVOS": "UPPER(COALESCE(p.estado,'ACTIVO')) <> 'INACTIVO'",
        "INACTIVOS": "UPPER(COALESCE(p.estado,'ACTIVO')) = 'INACTIVO'",
        "TODOS": "TRUE",
    }[estado]
    base = f"""
        FROM procesos p
        LEFT JOIN inmuebles_ph i ON i.id = p.inmueble_id
        WHERE {filtro_estado}
          AND p.radicado_rama IS NOT NULL
          AND length(regexp_replace(p.radicado_rama, '[^0-9]', '', 'g')) = 23
    """
    with _lectura() as cur:
        cur.execute(f"SELECT COUNT(*) AS total {base}")
        total = int((cur.fetchone() or {}).get("total") or 0)
        cur.execute(
            f"""
            SELECT p.radicado_interno, p.radicado_rama, p.juzgado,
                   i.conjunto_residencial, i.torre_apto,
                   (SELECT STRING_AGG(DISTINCT c.nombre, ' | ' ORDER BY c.nombre)
                      FROM proceso_partes pp
                      JOIN contactos c ON c.id = pp.contacto_id
                     WHERE pp.radicado_interno = p.radicado_interno
                       AND UPPER(pp.rol) = 'DEMANDADO') AS demandado
            {base}
            ORDER BY p.radicado_interno
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        procesos = [dict(r) for r in cur.fetchall()]
        ids = [p["radicado_interno"] for p in procesos]
        registros: dict[str, dict] = {
            i: {"ids": set(), "hashes": set(), "manuales": []} for i in ids
        }
        if ids and _tabla_existe(cur, "actuaciones_proceso"):
            cur.execute(
                """
                SELECT radicado_interno, id_actuacion, content_hash
                FROM actuaciones_proceso
                WHERE radicado_interno = ANY(%s)
                """,
                (ids,),
            )
            for r in cur.fetchall():
                reg = registros.get(r["radicado_interno"])
                if reg is None:
                    continue
                if r.get("id_actuacion"):
                    reg["ids"].add(str(r["id_actuacion"]).strip())
                if r.get("content_hash"):
                    reg["hashes"].add(r["content_hash"])
        if ids and _tabla_existe(cur, "actuaciones"):
            cur.execute(
                """
                SELECT radicado_interno, fecha, descripcion
                FROM actuaciones
                WHERE radicado_interno = ANY(%s)
                """,
                (ids,),
            )
            for r in cur.fetchall():
                reg = registros.get(r["radicado_interno"])
                texto = sps._norm_text(r.get("descripcion"))
                fecha = sps._parse_date(r.get("fecha"))
                if reg is not None and texto and fecha is not None:
                    reg["manuales"].append((fecha, texto))
    return total, procesos, registros


def _ya_registrada(fila: dict, registro: dict) -> bool:
    """True si la actuación de la Rama ya está en el ERP.

    Coincide por id externo o hash en ``actuaciones_proceso`` (lo que llena la
    sincronización diaria) o, en ``actuaciones`` (manual), por fecha y texto.
    """
    if fila.get("id_actuacion") and fila["id_actuacion"] in registro["ids"]:
        return True
    if fila.get("content_hash") in registro["hashes"]:
        return True
    fecha = fila.get("fecha_actuacion")
    if fecha is None:
        return False
    rama_texto = sps._norm_text(f"{fila.get('tipo_actuacion') or ''} {fila.get('descripcion') or ''}")
    anotacion = sps._norm_text(fila.get("descripcion"))
    for f_manual, texto in registro["manuales"]:
        if f_manual != fecha:
            continue
        if texto in rama_texto or (anotacion and anotacion in texto):
            return True
    return False


async def _actuaciones_de_la_rama(client: Any, radicado_rama: str) -> list:
    id_interno = await client.consultar_id_por_radicado(radicado_rama)
    return await client.obtener_actuaciones(id_interno)


@router.get("/rama/novedades")
async def rama_novedades(
    desde: str | None = Query(None, description="AAAA-MM-DD (inclusive)"),
    estado: str = Query("ACTIVOS", max_length=10),
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    solo_no_registradas: bool = False,
):
    """Actuaciones de la Rama posteriores a ``desde`` o aún no registradas en el ERP.

    Con ``solo_no_registradas=true`` se exige además que no estén registradas.
    Una consulta fallida va a ``errores`` sin afectar al resto.
    """
    estado = estado.strip().upper()
    if estado not in ("ACTIVOS", "INACTIVOS", "TODOS"):
        raise HTTPException(status_code=422, detail="estado debe ser ACTIVOS, INACTIVOS o TODOS")
    f_desde = _fecha(desde, "desde")
    total, procesos, registros = await run_in_threadpool(
        _cargar_procesos_y_registros, estado, limit, offset
    )

    loop = asyncio.get_running_loop()
    limite_total = loop.time() + _timeout_total_s()
    cupo = asyncio.Semaphore(_concurrencia())
    errores: list[dict] = []
    novedades: list[dict] = []

    async def _una(client: Any, p: dict) -> None:
        radicado_interno = p["radicado_interno"]
        rama = sps.normalizar_radicado_rama(p["radicado_rama"])
        async with cupo:
            if loop.time() > limite_total:
                errores.append({"radicado_interno": radicado_interno, "error": _MSG_TIEMPO})
                return
            try:
                crudas = await _con_limite(_actuaciones_de_la_rama(client, rama))
            except Exception as exc:
                errores.append(
                    {"radicado_interno": radicado_interno, "error": _error_rama(exc).detail}
                )
                return
        items = []
        for a in crudas:
            fila = sps.mapear_actuacion_api(a, radicado_interno=radicado_interno, radicado_rama=rama)
            registrada = _ya_registrada(fila, registros[radicado_interno])
            posterior = bool(f_desde and fila["fecha_actuacion"] and fila["fecha_actuacion"] >= f_desde)
            if solo_no_registradas:
                incluir = not registrada and (f_desde is None or posterior)
            else:
                incluir = posterior or not registrada
            if not incluir:
                continue
            item = _actuacion_json(a)
            item["registrada_en_erp"] = registrada
            item["motivo"] = [m for m, ok in (("posterior_a_desde", posterior), ("no_registrada", not registrada)) if ok]
            items.append(item)
        if items:
            items.sort(key=_fecha_orden, reverse=True)
            novedades.append(
                {
                    "radicado_interno": radicado_interno,
                    "radicado_rama": rama,
                    "conjunto": p.get("conjunto_residencial"),
                    "torre_apto": p.get("torre_apto"),
                    "demandado": p.get("demandado"),
                    "juzgado": p.get("juzgado"),
                    "actuaciones": items,
                }
            )

    if procesos:
        try:
            async with external_legal_client.LegalAPIClient() as client:
                await asyncio.gather(*(_una(client, p) for p in procesos))
        except Exception as exc:
            raise _error_rama(exc) from None
    novedades.sort(key=lambda n: n["radicado_interno"])
    errores.sort(key=lambda e: e["radicado_interno"])
    return _ok(
        desde=f_desde,
        estado=estado,
        total_procesos=total,
        limit=limit,
        offset=offset,
        consultados=len(procesos) - len(errores),
        con_novedades=len(novedades),
        novedades=novedades,
        errores=errores,
    )
