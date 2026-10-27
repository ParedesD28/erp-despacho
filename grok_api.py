"""Consultas de SOLO LECTURA para "Grok Bot" bajo ``/api/grok/*``.

Autenticación: únicamente ``X-API-Key`` = ``GROK_BOT_API_KEY`` (ninguna otra llave sirve
aquí, ni la heredada). Todas las rutas son GET y se ejecutan en transacciones
``READ ONLY``; la liquidación se calcula con ``autocausar=False`` (no escribe).

Qué NO se devuelve nunca: contraseñas, hashes, tokens, llaves, datos de usuarios del ERP
(abogados/usuarios, perfiles) ni sus nombres. Sí se devuelven datos de deudores y
propietarios (nombre, identificación, teléfono, correo, dirección) porque el bot los
necesita para cobrar; ver PRODUCCION.md.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg2.extras import RealDictCursor

import m2m_keys
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


@router.get("/liquidacion")
def calcular_liquidacion(
    inmueble_id: int,
    fecha_corte: str,
    obligacion_id: int | None = None,
    detalle: bool = False,
):
    """Calcula la liquidación PH sin escribir (sin autocausación y sin PDF)."""
    import bot_api
    import liquidador

    corte = _fecha(fecha_corte, "fecha_corte", futura_ok=False)
    try:
        resultados, resumen, inm_info = liquidador.motor_calculo_judicial(
            inmueble_id, "usura", 2.5, 23.8, 0.0, corte,
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
