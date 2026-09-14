"""Módulo oficial de Tasas de Interés de la Superfinanciera (SFC) y caché en Neon."""
from datetime import date, datetime
import unicodedata
import requests
from psycopg2.extras import RealDictCursor
import db

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


def asegurar_tabla_tasas():
    conn = db.get_connection()
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
        print("[TASAS] Tabla historico_tasas asegurada en Neon", flush=True)
        return True
    except Exception as exc:
        print(f"[TASAS][ALERTA] No se pudo preparar historico_tasas: {exc!r}", flush=True)
        return False
    finally:
        conn.release()


def descargar_tasas_sfc():
    print("[TASAS][SFC] Conectando a Datos Abiertos de la Superfinanciera...", flush=True)
    try:
        respuesta = requests.get(
            TASAS_SFC_URL,
            params={"$limit": 5000, "$order": "vigencia_desde DESC"},
            timeout=15,
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
    except Exception as exc:
        print(f"[TASAS][ALERTA] No se pudo conectar a la API SFC: {exc!r}", flush=True)
        raise
    if not isinstance(datos, list):
        raise ValueError("La SFC no devolvió una lista")
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
    conn = db.get_connection()
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
        conn.release()


def sincronizar_cache_sfc():
    """Descarga las tasas oficiales de la SFC y actualiza la caché de Neon."""
    try:
        registros = descargar_tasas_sfc()
        seleccion = _extraer_tasas_consumo_ordinario(registros)
        if not seleccion:
            raise LookupError("La API respondio pero no se encontro Consumo y ordinario")
        for (anio, mes), (desde, hasta, ibc) in seleccion.items():
            _guardar_tasa(anio, mes, desde, hasta, ibc)
        print(f"[TASAS][SFC] Sincronización completa: {len(seleccion)} periodos validados", flush=True)
        return True
    except Exception as exc:
        print(f"[TASAS][ALERTA] Sincronización SFC fallida: {exc!r}", flush=True)
        return False


def _obtener_tasa_cache(anio, mes):
    conn = db.get_connection()
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
            return tasa
    finally:
        conn.release()


def obtener_tasa_bd_o_api(anio, mes):
    tasa = _obtener_tasa_cache(anio, mes)
    if tasa is not None:
        return tasa
    print(f"[TASAS] Caché no encontrada en Neon para {anio}-{mes:02d}; consultando SFC...", flush=True)
    if not sincronizar_cache_sfc():
        conn = db.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT tasa_efectiva_anual, tasa_usura_ea
                      FROM historico_tasas
                     WHERE anio=%s AND mes=%s
                     LIMIT 1
                """, (int(anio), int(mes)))
                fila = cur.fetchone()
                if fila:
                    t = float(fila.get("tasa_usura_ea") or fila.get("tasa_efectiva_anual") or 0)
                    return t / 100.0 if t > 1 else t
        finally:
            conn.release()
        raise RuntimeError(f"No fue posible validar tasa SFC para {anio}-{mes:02d}")
    tasa = _obtener_tasa_cache(anio, mes)
    if tasa is None:
        raise RuntimeError(f"La SFC no entregó tasa válida para {anio}-{mes:02d}")
    return tasa


def prueba_conexion_sfc():
    try:
        registros = descargar_tasas_sfc()
        seleccion = _extraer_tasas_consumo_ordinario(registros)
        hoy = (date.today().year, date.today().month)
        if hoy in seleccion:
            _, _, ibc = seleccion[hoy]
            print(f"[TASAS][SFC] Conexión SFC verificada: IBC actual={ibc*100:.2f}% EA / Usura={ibc*150:.2f}% EA", flush=True)
    except Exception as exc:
        print(f"[TASAS][ALERTA] Conexión SFC no verificada al arrancar: {exc!r}", flush=True)
