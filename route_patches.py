"""Parches de producción para tasas SFC y rutas del ERP."""
from datetime import date, datetime
import unicodedata

import requests
from fastapi import Request, HTTPException
from psycopg2.extras import RealDictCursor
import main


def _conn():
    return main.db_pool.getconn()


def _release(conn):
    main.db_pool.putconn(conn)


# ==============================================================================
# TASAS SFC: fuente oficial + cache validada en Neon
# ==============================================================================
TASAS_SFC_URL = "https://www.datos.gov.co/resource/pare-7x5i.json"
TASA_MODALIDAD = "Consumo y ordinario"
TASA_FUENTE = "Datos Abiertos Colombia / Superintendencia Financiera de Colombia"


def _normalizar_texto(valor):
    texto = str(valor or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto)
        if not unicodedata.combining(c)
    )


def _es_consumo_ordinario(modalidad):
    m = _normalizar_texto(modalidad)
    return "consumo" in m and "ordinario" in m and "bajo monto" not in m


def _parse_tasa(valor):
    if valor is None:
        return None
    texto = str(valor).strip().replace("%", "").replace(" ", "")
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        numero = float(texto)
        return numero / 100.0 if numero > 1 else numero
    except (TypeError, ValueError):
        return None


def _parse_fecha(valor):
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(str(valor)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _asegurar_tabla_tasas():
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS historico_tasas (
                        anio INTEGER NOT NULL,
                        mes INTEGER NOT NULL,
                        tasa_efectiva_anual NUMERIC(18,10) NOT NULL,
                        tasa_usura_ea NUMERIC(18,10),
                        ibc_ea NUMERIC(18,10),
                        modalidad VARCHAR(120) NOT NULL DEFAULT 'Consumo y ordinario',
                        vigencia_desde DATE,
                        vigencia_hasta DATE,
                        fuente TEXT,
                        consultado_en TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        validado_sfc BOOLEAN DEFAULT FALSE
                    )
                """)
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS tasa_usura_ea NUMERIC(18,10)")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS ibc_ea NUMERIC(18,10)")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS modalidad VARCHAR(120)")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS vigencia_desde DATE")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS vigencia_hasta DATE")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS fuente TEXT")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS consultado_en TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS validado_sfc BOOLEAN DEFAULT FALSE")
                # Invalidar todo lo anterior para que no vuelva a utilizarse sin validacion SFC.
                cur.execute("UPDATE historico_tasas SET validado_sfc=FALSE")
        print("[TASAS] Cache historico anterior invalidada: se exige validacion SFC", flush=True)
    except Exception as exc:
        print(f"[TASAS][ALERTA] No se pudo preparar historico_tasas: {exc!r}", flush=True)
        return False
    finally:
        _release(conn)
    return True


def _descargar_tasas_sfc():
    print("[TASAS][SFC] CONECTANDO A DATOS ABIERTOS DE LA SUPERFINANCIERA...", flush=True)
    try:
        respuesta = requests.get(
            TASAS_SFC_URL,
            params={"$limit": 5000, "$order": "vigencia_desde DESC"},
            timeout=15,
        )
        print(f"[TASAS][SFC] HTTP {respuesta.status_code}", flush=True)
        respuesta.raise_for_status()
        datos = respuesta.json()
    except Exception as exc:
        print(f"[TASAS][ALERTA] NO SE PUDO CONECTAR A LA API SFC: {exc!r}", flush=True)
        raise
    if not isinstance(datos, list):
        raise ValueError("La SFC no devolvio una lista")
    print(f"[TASAS][SFC] Registros recibidos: {len(datos)}", flush=True)
    return datos


def _extraer_tasas_consumo_ordinario(registros):
    seleccion = {}
    for fila in registros:
        if not _es_consumo_ordinario(fila.get("modalidad")):
            continue
        desde = _parse_fecha(fila.get("vigencia_desde"))
        hasta = _parse_fecha(fila.get("vigencia_hasta"))
        ibc = _parse_tasa(fila.get("interes_bancario_corriente"))
        if desde is None or ibc is None:
            continue
        clave = (desde.year, desde.month)
        actual = seleccion.get(clave)
        if actual is None or desde > actual[0]:
            seleccion[clave] = (desde, hasta, ibc)
    return seleccion


def _guardar_tasa(anio, mes, desde, hasta, ibc):
    usura = ibc * 1.5
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE historico_tasas
                       SET tasa_efectiva_anual=%s,
                           tasa_usura_ea=%s,
                           ibc_ea=%s,
                           modalidad=%s,
                           vigencia_desde=%s,
                           vigencia_hasta=%s,
                           fuente=%s,
                           consultado_en=CURRENT_TIMESTAMP,
                           validado_sfc=TRUE
                     WHERE anio=%s AND mes=%s
                """, (
                    usura, usura, ibc, TASA_MODALIDAD, desde, hasta,
                    TASA_FUENTE, int(anio), int(mes)
                ))
                if cur.rowcount == 0:
                    cur.execute("""
                        INSERT INTO historico_tasas
                            (anio, mes, tasa_efectiva_anual, tasa_usura_ea, ibc_ea,
                             modalidad, vigencia_desde, vigencia_hasta, fuente,
                             consultado_en, validado_sfc)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,TRUE)
                    """, (
                        int(anio), int(mes), usura, usura, ibc, TASA_MODALIDAD,
                        desde, hasta, TASA_FUENTE
                    ))
        return usura
    finally:
        _release(conn)


def _sincronizar_cache_sfc():
    """Una sola llamada SFC reconstruye la cache de consumo/ordinario."""
    try:
        registros = _descargar_tasas_sfc()
        seleccion = _extraer_tasas_consumo_ordinario(registros)
        if not seleccion:
            raise LookupError("La API respondio pero no se encontro Consumo y ordinario")
        for (anio, mes), (desde, hasta, ibc) in seleccion.items():
            usura = _guardar_tasa(anio, mes, desde, hasta, ibc)
            print(
                f"[TASAS][SFC] {anio}-{mes:02d}: IBC={ibc*100:.2f}% EA -> USURA={usura*100:.2f}% EA -> Neon",
                flush=True,
            )
        print(f"[TASAS][SFC] SINCRONIZACION COMPLETA: {len(seleccion)} periodos validados", flush=True)
        return True
    except Exception as exc:
        print(f"[TASAS][ALERTA] SINCRONIZACION SFC FALLIDA: {exc!r}", flush=True)
        return False


def _obtener_tasa_cache(anio, mes):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT tasa_efectiva_anual, tasa_usura_ea, vigencia_desde,
                       vigencia_hasta, validado_sfc
                  FROM historico_tasas
                 WHERE anio=%s AND mes=%s
                   AND COALESCE(modalidad, %s)=%s
                   AND COALESCE(validado_sfc,FALSE)=TRUE
                 LIMIT 1
            """, (int(anio), int(mes), TASA_MODALIDAD, TASA_MODALIDAD))
            fila = cur.fetchone()
            if not fila:
                return None
            tasa = fila.get("tasa_usura_ea") or fila.get("tasa_efectiva_anual")
            if tasa is None:
                return None
            tasa = float(tasa)
            if tasa > 1:
                tasa /= 100.0
            print(f"[TASAS] CACHE HIT Neon VALIDADA {anio}-{mes:02d}: USURA={tasa*100:.2f}% EA", flush=True)
            return tasa
    finally:
        _release(conn)


def obtener_tasa_bd_o_api(anio, mes):
    tasa = _obtener_tasa_cache(anio, mes)
    if tasa is not None:
        return tasa
    print(f"[TASAS] CACHE MISS Neon {anio}-{mes:02d}; consultando SFC", flush=True)
    if not _sincronizar_cache_sfc():
        raise RuntimeError(f"No fue posible validar tasa SFC para {anio}-{mes:02d}")
    tasa = _obtener_tasa_cache(anio, mes)
    if tasa is None:
        raise RuntimeError(f"La SFC no entrego tasa valida para {anio}-{mes:02d}")
    return tasa


main.obtener_tasa_bd_o_api = obtener_tasa_bd_o_api


# ==============================================================================
# MOTOR: asegura tasas antes de calcular y corrige tasa personalizada EA
# ==============================================================================
_ORIGINAL_MOTOR_CALCULO = main.motor_calculo_judicial


def _mes_inicial_deuda(inmueble_id):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MIN(periodo_anio), MIN(periodo_mes)
                FROM expensas_ph
                WHERE inmueble_id=%s
            """, (int(inmueble_id),))
            return cur.fetchone()
    finally:
        _release(conn)


def _asegurar_tasas_para_liquidacion(inmueble_id, tipo_tasa, fecha_corte):
    if "Fija" in str(tipo_tasa):
        return
    inicio = _mes_inicial_deuda(inmueble_id)
    if not inicio or inicio[0] is None or inicio[1] is None:
        return
    y, m = int(inicio[0]), int(inicio[1])
    limite = date(fecha_corte.year, fecha_corte.month, 1)
    while date(y, m, 1) <= limite:
        obtener_tasa_bd_o_api(y, m)
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


def motor_calculo_judicial_seguro(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos_globales, fecha_corte):
    if "Fija" in str(tipo_tasa):
        # La tasa personalizada se interpreta como E.A. y se convierte correctamente a mensual.
        ea = max(float(tasa_fija), 0.0) / 100.0
        mensual = ((1.0 + ea) ** (1.0 / 12.0)) - 1.0
        tasa_fija_interna = mensual * 100.0
        resultados, resumen, info = _ORIGINAL_MOTOR_CALCULO(
            inmueble_id, "Fija", tasa_fija_interna, honorarios_pct, gastos_globales, fecha_corte
        )
        for fila in resultados:
            fila["tasa_ea"] = f"{ea*100:.2f}%"
            fila["tasa_mes"] = f"{mensual*100:.4f}%"
            fila["tasa_str"] = f"EA: {ea*100:.2f}% (Mes: {mensual*100:.4f}%)"
        return resultados, resumen, info
    _asegurar_tasas_para_liquidacion(inmueble_id, tipo_tasa, fecha_corte)
    return _ORIGINAL_MOTOR_CALCULO(
        inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos_globales, fecha_corte
    )


main.motor_calculo_judicial = motor_calculo_judicial_seguro


# ==============================================================================
# VERIFICACION DE CONEXION SFC AL ARRANCAR
# ==============================================================================
def _prueba_conexion_sfc():
    try:
        registros = _descargar_tasas_sfc()
        seleccion = _extraer_tasas_consumo_ordinario(registros)
        hoy = (date.today().year, date.today().month)
        if hoy not in seleccion:
            raise LookupError(f"No existe registro SFC para el periodo actual {hoy[0]}-{hoy[1]:02d}")
        _, _, ibc = seleccion[hoy]
        print(
            f"[TASAS][SFC] CONEXION VERIFICADA AL ARRANCAR: IBC actual={ibc*100:.2f}% EA / USURA={ibc*150:.2f}% EA",
            flush=True,
        )
    except Exception as exc:
        print(f"[TASAS][ALERTA] CONEXION SFC NO VERIFICADA AL ARRANCAR: {exc!r}", flush=True)


# ==============================================================================
# RUTA DE DETALLE DE EXPEDIENTE EXISTENTE
# ==============================================================================
def _replace_detail_route():
    routes = main.app.router.routes
    main.app.router.routes[:] = [
        r for r in routes
        if not (getattr(r, "path", None) == "/expediente/{radicado}" and "GET" in getattr(r, "methods", set()))
    ]

    @main.app.get("/expediente/{radicado}")
    def detalle_expediente_corregido(request: Request, radicado: str):
        conn = _conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT p.*, c.nombre AS demandante_db, a.nombre AS abogado_asignado
                    FROM procesos p
                    LEFT JOIN contactos c ON c.identificacion=p.id_cliente
                    LEFT JOIN abogados a ON a.id=p.abogado_id
                    WHERE p.radicado_interno=%s
                    LIMIT 1
                """, (radicado,))
                proceso = cur.fetchone()
                if not proceso:
                    raise HTTPException(status_code=404, detail="Expediente no encontrado")
                proceso = dict(proceso)
                cur.execute("""
                    SELECT pl.identificacion_demandado, c.nombre
                    FROM procesos_litisconsorcio pl
                    LEFT JOIN contactos c ON c.identificacion=pl.identificacion_demandado
                    WHERE pl.radicado_interno=%s
                    ORDER BY c.nombre NULLS LAST
                """, (radicado,))
                demandados = cur.fetchall()
                if demandados:
                    proceso["id_demandado"] = " | ".join(
                        str(r.get("identificacion_demandado"))
                        for r in demandados if r.get("identificacion_demandado")
                    )
                    proceso["demandado"] = " | ".join(
                        str(r.get("nombre") or r.get("identificacion_demandado"))
                        for r in demandados
                        if r.get("nombre") or r.get("identificacion_demandado")
                    )
                cur.execute(
                    "SELECT * FROM actuaciones WHERE radicado_interno=%s ORDER BY fecha DESC, id DESC",
                    (radicado,)
                )
                actuaciones = [dict(r) for r in cur.fetchall()]
            return main.templates.TemplateResponse(
                request=request,
                name="detalle_expediente.html",
                context={"request": request, "proceso": proceso, "actuaciones": actuaciones},
            )
        finally:
            _release(conn)


# Preparacion y prueba de fuente oficial al importar el modulo.
_asegurar_tabla_tasas()
_prueba_conexion_sfc()
_sincronizar_cache_sfc()
_replace_detail_route()
print("[TASAS] SFC/Neon listo: solo se usan tasas validadas por la Superfinanciera", flush=True)
