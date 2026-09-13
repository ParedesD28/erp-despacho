"""Correcciones puntuales aplicadas después del registro de compat_routes."""
from datetime import date, datetime

import requests
from fastapi import Request, HTTPException
from psycopg2.extras import RealDictCursor
import main


def _conn():
    return main.db_pool.getconn()


def _release(conn):
    main.db_pool.putconn(conn)


# ==============================================================================
# CACHE OFICIAL DE TASAS DE INTERES / USURA
# ==============================================================================
# Fuente primaria: Datos Abiertos Colombia, conjunto pare-7x5i, propiedad de la
# Superintendencia Financiera de Colombia (SFC). historico_tasas es la cache
# historica para no repetir consultas a la fuente.
#
# IMPORTANTE: main.py usa tasa_efectiva_anual como la tasa que debe aplicar el
# liquidador. Para la opcion "Superfinanciera" esa columna debe contener USURA,
# no el IBC. El IBC se conserva separadamente en ibc_ea.
# ==============================================================================
TASAS_SFC_URL = "https://www.datos.gov.co/resource/pare-7x5i.json"
TASA_MODALIDAD = "Consumo y ordinario"


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
                        consultado_en TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS tasa_usura_ea NUMERIC(18,10)")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS ibc_ea NUMERIC(18,10)")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS modalidad VARCHAR(120)")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS vigencia_desde DATE")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS vigencia_hasta DATE")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS fuente TEXT")
                cur.execute("ALTER TABLE historico_tasas ADD COLUMN IF NOT EXISTS consultado_en TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP")
                cur.execute("""
                    UPDATE historico_tasas
                       SET modalidad = COALESCE(modalidad, 'Consumo y ordinario')
                     WHERE modalidad IS NULL
                """)
                # Reparacion de datos historicos: main.py consumia esta columna
                # directamente. Registros antiguos contienen IBC; los convertimos
                # una sola vez a usura y preservamos el IBC en ibc_ea.
                cur.execute("""
                    UPDATE historico_tasas
                       SET ibc_ea = COALESCE(ibc_ea, tasa_efectiva_anual),
                           tasa_usura_ea = COALESCE(
                               tasa_usura_ea,
                               CASE
                                   WHEN tasa_efectiva_anual > 1 THEN tasa_efectiva_anual * 1.5 / 100.0
                                   ELSE tasa_efectiva_anual * 1.5
                               END
                           )
                     WHERE tasa_efectiva_anual IS NOT NULL
                """)
                cur.execute("""
                    UPDATE historico_tasas
                       SET tasa_efectiva_anual = tasa_usura_ea
                     WHERE tasa_usura_ea IS NOT NULL
                """)
        print("[TASAS] historico_tasas listo; tasa_efectiva_anual reparada para usar USURA", flush=True)
        return True
    except Exception as exc:
        print(f"[TASAS][ALERTA] No fue posible preparar historico_tasas: {exc!r}", flush=True)
        return False
    finally:
        _release(conn)


def _parse_tasa_porcentaje(valor):
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


def _parse_fecha_sfc(valor):
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(str(valor)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _consultar_tasa_sfc(anio, mes):
    """Consulta el IBC oficial de consumo y ordinario y calcula usura."""
    print(f"[TASAS][SFC] Consultando fuente oficial para {int(anio)}-{int(mes):02d}...", flush=True)
    desde = date(int(anio), int(mes), 1)
    try:
        respuesta = requests.get(
            TASAS_SFC_URL,
            params={"$limit": 5000, "$order": "vigencia_desde DESC"},
            timeout=15,
        )
        print(f"[TASAS][SFC] HTTP {respuesta.status_code} para {int(anio)}-{int(mes):02d}", flush=True)
        respuesta.raise_for_status()
        registros = respuesta.json()
    except Exception as exc:
        print(f"[TASAS][ALERTA] NO SE PUDO CONECTAR A LA API DE LA SFC para {int(anio)}-{int(mes):02d}: {exc!r}", flush=True)
        raise

    if not isinstance(registros, list):
        print("[TASAS][ALERTA] La SFC no devolvio una lista de registros", flush=True)
        raise ValueError("La SFC no devolvio una lista de registros")

    candidatos = []
    for fila in registros:
        modalidad = str(fila.get("modalidad") or "").strip()
        if modalidad.lower() != TASA_MODALIDAD.lower():
            continue
        vig_desde = _parse_fecha_sfc(fila.get("vigencia_desde"))
        vig_hasta = _parse_fecha_sfc(fila.get("vigencia_hasta"))
        if vig_desde is None:
            continue
        if vig_desde.year == desde.year and vig_desde.month == desde.month:
            ibc_ea = _parse_tasa_porcentaje(fila.get("interes_bancario_corriente"))
            if ibc_ea is None:
                continue
            candidatos.append((vig_desde, vig_hasta, ibc_ea, fila))

    if not candidatos:
        print(f"[TASAS][ALERTA] La API SFC respondio, pero no encontro tasa para {anio}-{mes:02d} / {TASA_MODALIDAD}", flush=True)
        raise LookupError(f"No existe tasa SFC para {anio}-{mes:02d} en modalidad {TASA_MODALIDAD}")

    candidatos.sort(key=lambda item: item[0], reverse=True)
    vig_desde, vig_hasta, ibc_ea, fila = candidatos[0]
    usura_ea = ibc_ea * 1.5

    print(
        f"[TASAS][SFC] OK {anio}-{mes:02d}: IBC={ibc_ea*100:.2f}% EA -> USURA={usura_ea*100:.2f}% EA",
        flush=True,
    )
    return {
        "anio": int(anio),
        "mes": int(mes),
        "ibc_ea": ibc_ea,
        "usura_ea": usura_ea,
        "vigencia_desde": vig_desde,
        "vigencia_hasta": vig_hasta,
        "modalidad": TASA_MODALIDAD,
        "fuente": TASAS_SFC_URL,
    }


def _guardar_tasa(datos):
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
                           consultado_en=CURRENT_TIMESTAMP
                     WHERE anio=%s AND mes=%s
                       AND COALESCE(modalidad, %s)=%s
                """, (
                    datos["usura_ea"], datos["usura_ea"], datos["ibc_ea"], datos["modalidad"],
                    datos["vigencia_desde"], datos["vigencia_hasta"], datos["fuente"],
                    datos["anio"], datos["mes"], TASA_MODALIDAD, TASA_MODALIDAD,
                ))
                if cur.rowcount == 0:
                    cur.execute("""
                        INSERT INTO historico_tasas
                            (anio, mes, tasa_efectiva_anual, tasa_usura_ea, ibc_ea, modalidad,
                             vigencia_desde, vigencia_hasta, fuente, consultado_en)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                    """, (
                        datos["anio"], datos["mes"], datos["usura_ea"], datos["usura_ea"], datos["ibc_ea"],
                        datos["modalidad"], datos["vigencia_desde"], datos["vigencia_hasta"], datos["fuente"],
                    ))
        print(
            f"[TASAS] SFC -> Neon {datos['anio']}-{datos['mes']:02d}: "
            f"IBC={datos['ibc_ea']*100:.2f}% EA, USURA={datos['usura_ea']*100:.2f}% EA",
            flush=True,
        )
    finally:
        _release(conn)


def obtener_tasa_bd_o_api(anio, mes):
    """Neon primero; SFC solo en cache miss o registro incompleto."""
    _asegurar_tabla_tasas()
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT tasa_efectiva_anual, tasa_usura_ea, ibc_ea, vigencia_desde, vigencia_hasta, fuente
                  FROM historico_tasas
                 WHERE anio=%s AND mes=%s
                   AND COALESCE(modalidad, %s)=%s
                 LIMIT 1
            """, (int(anio), int(mes), TASA_MODALIDAD, TASA_MODALIDAD))
            fila = cur.fetchone()
            if fila:
                tasa = fila.get("tasa_usura_ea") or fila.get("tasa_efectiva_anual")
                if tasa is not None:
                    tasa = float(tasa)
                    if tasa > 1:
                        tasa /= 100.0
                    print(f"[TASAS] CACHE HIT Neon {anio}-{mes:02d}: USURA={tasa*100:.2f}% EA", flush=True)
                    return tasa
    finally:
        _release(conn)

    datos = _consultar_tasa_sfc(anio, mes)
    _guardar_tasa(datos)
    return float(datos["usura_ea"])


# El motor de main.py llama esta funcion por nombre global.
main.obtener_tasa_bd_o_api = obtener_tasa_bd_o_api


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
                        str(r.get("identificacion_demandado")) for r in demandados if r.get("identificacion_demandado")
                    )
                    proceso["demandado"] = " | ".join(
                        str(r.get("nombre") or r.get("identificacion_demandado"))
                        for r in demandados
                        if r.get("nombre") or r.get("identificacion_demandado")
                    )
                cur.execute("SELECT * FROM actuaciones WHERE radicado_interno=%s ORDER BY fecha DESC, id DESC", (radicado,))
                actuaciones = [dict(r) for r in cur.fetchall()]
            return main.templates.TemplateResponse(
                request=request,
                name="detalle_expediente.html",
                context={"request": request, "proceso": proceso, "actuaciones": actuaciones},
            )
        finally:
            _release(conn)


_ensure_route_patch_ready = _asegurar_tabla_tasas()
_replace_detail_route()
print("[ROUTE_PATCHES] Detalle de expediente corregido", flush=True)
print("[TASAS] Cache historico SFC/Neon habilitado", flush=True)
print("[TASAS] ALERTA OPERATIVA: si la SFC no responde, aparecerá [TASAS][ALERTA] en el log y NO se usara 0% silenciosamente", flush=True)
