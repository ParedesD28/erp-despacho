import os
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
import pandas as pd
import bcrypt
import requests
import calendar
import warnings
import io
import re
import json
from typing import List
from datetime import date, datetime, timedelta
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse, Response
from dotenv import load_dotenv
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime
import os

warnings.filterwarnings('ignore', message='.*SQLAlchemy connectable.*')
load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ==============================================================================
# --- MOTOR DE REGLAS DE TÉRMINOS JUDICIALES ---
# ==============================================================================
mapa_subetapas = {
    "1. Presentación de la demanda": {"Radicación": 30, "Requerimiento por DT": 25, "Impulso o memorial": 30, "Observación": 0},
    "2. Inadmisión": {"Auto de inadmisión": 5, "Subsanación": 30, "Requerimiento por DT": 25, "Impulso o memorial": 30, "Observación": 0},
    "3. Admisión": {"Solicitud de oficios": 15, "Requerimiento por DT": 25, "Impulso o memorial": 30, "Observación": 0},
    "4. Medidas Cautelares": {"Gestión de medidas cautelares": 15, "Requerimiento por DT": 25, "Impulso o memorial": 30, "Observación": 0},
    "5. Notificación": {"Envío de notificación": 10, "Envío de informe a despacho": 30, "Requerimiento por DT": 25, "Impulso o memorial": 30, "Observación": 0},
    "6. Excepciones": {"Traslado": 10, "Contestación a excepciones": 30, "Requerimiento por DT": 25, "Impulso o memorial": 30, "Observación": 0},
    "7. Sentencia": {"Requerimiento por DT": 25, "Impulso o memorial": 30, "Observación": 0},
    "8. Desistimiento tácito": {"Impulso o memorial": 30, "Observación": 0},
    "Auto de Trámite / General": {"Revisión": 10, "Observación": 0},
    "Terminación del Proceso": {"Archivo": 0, "Observación": 0}
}

def sumar_dias_habiles(fecha_inicial: date, dias: int) -> date:
    """Suma días hábiles saltándose fines de semana"""
    fecha_actual = fecha_inicial
    dias_agregados = 0
    while dias_agregados < dias:
        fecha_actual += timedelta(days=1)
        if fecha_actual.weekday() < 5:
            dias_agregados += 1
    return fecha_actual

# ... (DEJA INTACTO TODO TU CÓDIGO DEL MEDIO DE AQUÍ EN ADELANTE) ...
# --- 1. SEGURIDAD Y RENDIMIENTO (CONNECTION POOL) ---
# Creamos un pool de conexiones para reciclar recursos y no tumbar a Neon
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(
        1, 20,
        os.getenv("DATABASE_URL")
    )
except Exception as e:
    print(f"Error creando el pool de conexiones: {e}")

# --- 2. RUTA TRANSSACIONAL EN CASCADA (NUEVO PROCESO) ---
@app.post("/crear_proceso_cascada")
async def crear_proceso_cascada(
    request: Request,
    radicado_interno: str = Form(...),
    radicado_rama: str = Form(...),
    naturaleza: str = Form(...),
    juzgado: str = Form(...),
    demandante_nombre: str = Form(...),
    demandante_cedula: str = Form(...),
    demandado_cedulas: str = Form(...),
    demandado_nombres: str = Form(...),
    conjunto_residencial: str = Form(...),
    nomenclatura_apto: str = Form(...)
):
    conn = db_pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cedulas_list = [c.strip() for c in demandado_cedulas.split("|")]
                nombres_list = [n.strip() for n in demandado_nombres.split("|")]
                deudor_principal_cedula = cedulas_list[0]
                deudor_principal_nombre = nombres_list[0]
                cur.execute("""
                    INSERT INTO contactos (identificacion, nombre, tipo, ciudad)
                    VALUES (%s, %s, 'Cliente', 'PEREIRA')
                    ON CONFLICT (identificacion) DO NOTHING;
                """, (demandante_cedula, demandante_nombre))
                cur.execute("""
                    INSERT INTO contactos (identificacion, nombre, tipo, ciudad)
                    VALUES (%s, %s, 'Contraparte', 'PEREIRA')
                    ON CONFLICT (identificacion) DO UPDATE SET nombre = EXCLUDED.nombre
                    RETURNING id;
                """, (deudor_principal_cedula, deudor_principal_nombre))
                resultado_contacto = cur.fetchone()
                if resultado_contacto:
                    contacto_id = resultado_contacto[0]
                else:
                    cur.execute("SELECT id FROM contactos WHERE identificacion = %s", (deudor_principal_cedula,))
                    contacto_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO inmuebles_ph (contacto_id, conjunto_residencial, torre_apto)
                    VALUES (%s, %s, %s)
                    RETURNING id;
                """, (contacto_id, conjunto_residencial, nomenclatura_apto))
                inmueble_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO procesos (radicado_interno, radicado_rama, naturaleza, juzgado,
                                          estado, id_demandado, demandado, inmueble_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (radicado_interno, radicado_rama, naturaleza, juzgado,
                      'Activo', demandado_cedulas, demandado_nombres, inmueble_id))
                cur.execute("""
                    INSERT INTO actuaciones (radicado_interno, fecha, etapa, descripcion, usuario, tipificacion_sugerida)
                    VALUES (%s, CURRENT_DATE, 'Inicio', 'Presentación inicial de la demanda', 'Sistema', 'Radicación')
                """, (radicado_interno,))
        return RedirectResponse(url="/expedientes?mensaje=Proceso+creado+exitosamente", status_code=303)
    except Exception as e:
        print(f"❌ Error en la cascada transaccional: {e}")
        return RedirectResponse(url="/expedientes?error=Fallo+la+creacion", status_code=303)
    finally:
        db_pool.putconn(conn)

# ==============================================================================
# --- EL GUARDIA DE SEGURIDAD GLOBAL (MIDDLEWARE) ---
# ==============================================================================
@app.middleware("http")
async def validador_general_seguridad(request: Request, call_next):
    rutas_publicas = ["/login", "/logout"]
    if request.url.path in rutas_publicas:
        return await call_next(request)
    token = request.cookies.get("token_erp")
    if not token:
        return RedirectResponse(url="/login", status_code=303)
    response = await call_next(request)
    return response

# ==============================================================================
# --- RUTAS DE LOGIN Y LOGOUT ---
# ==============================================================================

def verificar_password(password_plana, password_hash):
    try:
        import bcrypt
        if password_hash and str(password_hash).startswith('$2'):
            return bcrypt.checkpw(password_plana.encode('utf-8'), password_hash.encode('utf-8'))
    except ImportError:
        pass
    return password_plana == password_hash

@app.head("/login")
@app.get("/login")
def vista_login(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})

@app.post("/login")
def procesar_login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM abogados WHERE email = %s", (email,))
                usuario = cur.fetchone()
                if usuario and verificar_password(password, usuario['password']):
                    respuesta = RedirectResponse(url="/dashboard", status_code=303)
                    respuesta.set_cookie(key="token_erp", value=str(usuario['id']))
                    return respuesta
                return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "Credenciales incorrectas. Revisa tu correo y contraseña."})
    except Exception as e:
        print(f"Error fatal en login: {e}")
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": f"Error interno: {str(e)}"})

@app.get("/dashboard")
def vista_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"request": request})

@app.get("/logout")
def cerrar_sesion():
    respuesta = RedirectResponse(url="/login", status_code=303)
    respuesta.delete_cookie("token_erp")
    return respuesta

# --- FUNCIÓN MAESTRA PARA CARGAR INMUEBLES ---
def cargar_inmuebles_ph():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute("""
            SELECT i.id, i.conjunto_residencial, i.torre_apto, c.identificacion, c.nombre,
                   COALESCE(p.radicado_interno, 'SIN EXPEDIENTE') AS expediente
            FROM inmuebles_ph i
            JOIN contactos c ON i.contacto_id = c.id
            LEFT JOIN procesos p ON p.inmueble_id = i.id
            UNION
            SELECT i.id, i.conjunto_residencial, i.torre_apto, c.identificacion, c.nombre,
                   COALESCE(p.radicado_interno, 'SIN EXPEDIENTE') AS expediente
            FROM inmuebles_ph i
            JOIN procesos p ON p.inmueble_id = i.id
            JOIN procesos_litisconsorcio pl ON pl.radicado_interno = p.radicado_interno
            JOIN contactos c ON c.identificacion = pl.identificacion_demandado
            ORDER BY expediente DESC, nombre ASC
        """)
        lista = [{"id": r[0], "conjunto_residencial": r[1], "torre_apto": r[2], "cedula": r[3], "nombre": r[4], "expediente": r[5]} for r in cur.fetchall()]
        cur.close()
        conn.close()
        return lista
    except Exception as e:
        print(f"❌ Error cargando inmuebles globales: {e}")
        return []

warnings.filterwarnings('ignore', message='.*SQLAlchemy connectable.*')
load_dotenv()
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")

def hashear_password(password_plana):
    return bcrypt.hashpw(password_plana.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def obtener_nombres_demandantes(id_cliente_str, conn):
    if not id_cliente_str: return "DEMANDANTE"
    ids = [i.strip() for i in str(id_cliente_str).split("|")]
    nombres = []
    with conn.cursor() as cursor:
        for i in ids:
            try:
                cursor.execute("SELECT nombre FROM clientes WHERE identificacion = %s", (i,))
                res = cursor.fetchone()
                if res:
                    nombres.append(res[0])
                else:
                    cursor.execute("SELECT nombre FROM contactos WHERE identificacion = %s", (i,))
                    res2 = cursor.fetchone()
                    if res2: nombres.append(res2[0])
                    else: nombres.append(i)
            except Exception:
                nombres.append(i)
    return " / ".join(nombres) if nombres else "DEMANDANTE"

# 1. FUNCIÓN GENERAL DE PROCESOS
def cargar_procesos_general():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        query = '''
            SELECT p.radicado_interno, p.radicado_rama, p.naturaleza, p.juzgado, p.etapa_actual, p.estado,
                   p.pretensiones, p.medidas_cautelares, p.id_cliente, c_dem.nombre AS demandante_db,
                   a.nombre AS abogado_asignado, STRING_AGG(c_ddo.nombre, ' | ') AS demandado,
                   STRING_AGG(pl.identificacion_demandado, ' | ') AS id_demandado
            FROM procesos p
            LEFT JOIN contactos c_dem ON p.id_cliente = c_dem.identificacion
            LEFT JOIN abogados a ON p.abogado_id = a.id
            LEFT JOIN procesos_litisconsorcio pl ON p.radicado_interno = pl.radicado_interno
            LEFT JOIN contactos c_ddo ON pl.identificacion_demandado = c_ddo.identificacion
            GROUP BY p.radicado_interno, p.radicado_rama, p.naturaleza, p.juzgado, p.etapa_actual, p.estado,
                     p.pretensiones, p.medidas_cautelares, p.id_cliente, c_dem.nombre, a.nombre
            ORDER BY p.radicado_interno DESC
        '''
        df = pd.read_sql_query(query, conn)
        conn.close()
        return df.fillna("").to_dict(orient="records")
    except Exception as e:
        print(f"❌ Error cargando procesos generales: {e}")
        return []

@app.get("/expedientes")
def ver_expedientes(request: Request):
    return templates.TemplateResponse(request=request, name="expedientes.html", context={"procesos": cargar_procesos_general()})

# ... (RESTO DEL CÓDIGO EXISTENTE SIN CAMBIOS) ...

# ==============================================================================
# --- MÓDULO LIQUIDADOR FINANCIERO ---
# ==============================================================================
@app.get("/liquidador")
def vista_liquidador(request: Request, inmueble_id: int = None, tipo_tasa: str = "Máxima Legal", tasa_fija: float = 2.5, honorarios_pct: float = 23.8, gastos: float = 0.0):
    from datetime import date
    fecha_corte_obj = date.today()
    resultados = []
    resumen = {}
    lista_inmuebles = cargar_inmuebles_ph()
    if inmueble_id:
        try:
            resultados, resumen, _ = motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte_obj)
            print(f"✅ Auto-liquidación generada para el inmueble {inmueble_id}")
        except Exception as e:
            print(f"❌ Error en auto-cálculo de liquidación: {e}")
    return templates.TemplateResponse(request=request, name="liquidador.html", context={"inmuebles": lista_inmuebles, "resultados": resultados, "resumen": resumen, "parametros": {"inmueble_id": inmueble_id, "tipo_tasa": tipo_tasa, "tasa_fija": tasa_fija, "honorarios_pct": honorarios_pct, "gastos": gastos, "fecha_corte": fecha_corte_obj.strftime('%Y-%m-%d')}})

# ==============================================================================
# --- MOTOR CENTRAL MATEMÁTICO ---
# ==============================================================================
def motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos_globales, fecha_corte):
    import calendar
    from datetime import date
    import pandas as pd
    import psycopg2
    import os
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT periodo_anio, periodo_mes, valor_capital FROM expensas_ph
                    WHERE inmueble_id = %s AND concepto = 'Expensa Ordinaria'
                    ORDER BY periodo_anio DESC, periodo_mes DESC LIMIT 1
                """, (inmueble_id,))
                ultima = cur.fetchone()
                if ultima:
                    u_anio, u_mes, u_valor = ultima
                    if u_mes == 12: sig_anio, sig_mes = u_anio + 1, 1
                    else: sig_anio, sig_mes = u_anio, u_mes + 1
                    fecha_siguiente = date(sig_anio, sig_mes, 1)
                    corte_mes = date(fecha_corte.year, fecha_corte.month, 1)
                    while fecha_siguiente <= corte_mes:
                        f_vencimiento = fecha_siguiente.strftime('%Y-%m-01')
                        cur.execute("""
                            INSERT INTO expensas_ph (inmueble_id, concepto, periodo_mes, periodo_anio, valor_capital, fecha_vencimiento, estado)
                            VALUES (%s, 'Expensa Ordinaria', %s, %s, %s, %s, 'En Mora')
                        """, (inmueble_id, sig_mes, sig_anio, u_valor, f_vencimiento))
                        if sig_mes == 12: sig_anio, sig_mes = sig_anio + 1, 1
                        else: sig_mes += 1
                        fecha_siguiente = date(sig_anio, sig_mes, 1)
                conn.commit()
    except Exception as e:
        print(f"❌ Error en auto-causación: {e}")
    conn_l = psycopg2.connect(os.getenv("DATABASE_URL"))
    df_deuda = pd.read_sql_query("SELECT concepto, periodo_mes, periodo_anio, valor_capital FROM expensas_ph WHERE inmueble_id = %s", conn_l, params=(inmueble_id,))
    cur = conn_l.cursor()
    cur.execute("SELECT anio, mes, tasa_efectiva_anual FROM historico_tasas")
    memoria_tasas = {(r[0], r[1]): float(r[2]) for r in cur.fetchall()}
    cur.execute("SELECT i.conjunto_residencial, i.torre_apto, c.nombre, c.identificacion FROM inmuebles_ph i JOIN contactos c ON i.contacto_id = c.id WHERE i.id = %s", (inmueble_id,))
    inm_info = cur.fetchone()
    cur.close()
    conn_l.close()
    if df_deuda.empty:
        return [], {}, inm_info
    df_agrupado = df_deuda.groupby(['periodo_anio', 'periodo_mes', 'concepto'])['valor_capital'].sum().unstack(fill_value=0).reset_index()
    for col in ['Expensa Ordinaria', 'Cuota Extraordinaria', 'Gastos', 'Abono']:
        if col not in df_agrupado.columns: df_agrupado[col] = 0
    df_agrupado = df_agrupado.sort_values(by=['periodo_anio', 'periodo_mes'])
    resultados = []
    cap_acumulado = 0.0
    int_acumulado = 0.0
    primer_anio = int(df_agrupado['periodo_anio'].min())
    primer_mes = int(df_agrupado[df_agrupado['periodo_anio'] == primer_anio]['periodo_mes'].min())
    fecha_actual_loop = date(primer_anio, primer_mes, 1)
    while fecha_actual_loop <= fecha_corte:
        y, m = fecha_actual_loop.year, fecha_actual_loop.month
        _, last_day = calendar.monthrange(y, m)
        dias = fecha_corte.day if (y == fecha_corte.year and m == fecha_corte.month) else last_day
        desde = date(y, m, 1)
        hasta = fecha_corte if (y == fecha_corte.year and m == fecha_corte.month) else date(y, m, last_day)
        fila = df_agrupado[(df_agrupado['periodo_anio'] == y) & (df_agrupado['periodo_mes'] == m)]
        ord_val = float(fila['Expensa Ordinaria'].values[0]) if not fila.empty else 0.0
        ext_val = float(fila['Cuota Extraordinaria'].values[0]) if not fila.empty else 0.0
        gas_val = float(fila['Gastos'].values[0]) if not fila.empty else 0.0
        abo_val = float(fila['Abono'].values[0]) if not fila.empty else 0.0
        cap_mes = ord_val + ext_val + gas_val
        cap_acumulado += cap_mes
        if "Fija" in tipo_tasa:
            tasa_ea = tasa_fija / 100.0
            tasa_mensual = tasa_fija / 100.0
        else:
            tasa_ea = memoria_tasas.get((y, m))
            if tasa_ea is None:
                try: tasa_ea = obtener_tasa_bd_o_api(y, m)
                except: tasa_ea = 0.0
                memoria_tasas[(y, m)] = tasa_ea
            tasa_mensual = ((1 + tasa_ea) ** (1/12)) - 1
        str_tasa_ea = f"{(tasa_ea*100):.2f}%"
        str_tasa_mes = f"{(tasa_mensual*100):.4f}%"
        str_tasa_combinada = f"EA: {str_tasa_ea} (Mes: {str_tasa_mes})"
        interes_mes = cap_acumulado * tasa_mensual * (dias / 30.0) if cap_acumulado > 0 else 0
        int_acumulado += interes_mes
        if abo_val > 0:
            if abo_val <= int_acumulado:
                int_acumulado -= abo_val
            else:
                sobrante = abo_val - int_acumulado
                int_acumulado = 0.0
                cap_acumulado -= sobrante
        resultados.append({'desde': desde.strftime('%Y-%m-%d'), 'hasta': hasta.strftime('%Y-%m-%d'), 'tasa_str': str_tasa_combinada, 'tasa_ea': str_tasa_ea, 'tasa_mes': str_tasa_mes, 'ordinarias': ord_val, 'extraordinarias': ext_val, 'gastos': gas_val, 'abonos': abo_val, 'capital_liquidable': cap_acumulado, 'dias': dias, 'intereses': interes_mes, 'int_acumulado': int_acumulado, 'cap_int': cap_acumulado + int_acumulado})
        if m == 12: fecha_actual_loop = date(y + 1, 1, 1)
        else: fecha_actual_loop = date(y, m + 1, 1)
    total_capital = cap_acumulado
    total_intereses = int_acumulado
    total_honorarios = (total_capital + total_intereses) * (honorarios_pct / 100.0)
    gran_total = total_capital + total_intereses + total_honorarios + gastos_globales
    resumen = {"capital": total_capital, "intereses": total_intereses, "honorarios_pct": honorarios_pct, "honorarios": total_honorarios, "gastos": gastos_globales, "gran_total": gran_total}
    return resultados, resumen, inm_info

# ==============================================================================
# --- RUTAS DEL LIQUIDADOR ---
# ==============================================================================
@app.post("/liquidador")
def calcular_liquidador(request: Request, inmueble_id: int = Form(...), tipo_tasa: str = Form(...), tasa_fija: float = Form(2.5), honorarios_pct: float = Form(23.8), gastos: float = Form(0.0), fecha_corte: date = Form(...)):
    resultados, resumen, _ = motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte)
    if not resultados:
        return templates.TemplateResponse(request=request, name="liquidador.html", context={"inmuebles": cargar_inmuebles_ph(), "error": "No hay deudas.", "resultados": None})
    return templates.TemplateResponse(request=request, name="liquidador.html", context={"inmuebles": cargar_inmuebles_ph(), "resultados": resultados, "resumen": resumen, "parametros": {"inmueble_id": inmueble_id, "tipo_tasa": tipo_tasa, "tasa_fija": tasa_fija, "honorarios_pct": honorarios_pct, "gastos": gastos, "fecha_corte": fecha_corte.strftime('%Y-%m-%d')}})

@app.post("/liquidador/actualizar")
async def actualizar_cuotas(request: Request):
    form_data = await request.form()
    inmueble_id = int(form_data.get("inmueble_id"))
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                for key, value in form_data.items():
                    if key.startswith(("ord_", "ext_", "gas_", "abo_")):
                        prefijo, y, m = key.split("_")
                        concepto = {'ord': 'Expensa Ordinaria', 'ext': 'Cuota Extraordinaria', 'gas': 'Gastos', 'abo': 'Abono'}[prefijo]
                        valor = float(value) if value else 0.0
                        cur.execute("UPDATE expensas_ph SET valor_capital = %s WHERE inmueble_id = %s AND concepto = %s AND periodo_anio = %s AND periodo_mes = %s", (valor, inmueble_id, concepto, int(y), int(m)))
                        if cur.rowcount == 0 and valor > 0:
                            f_vencimiento = f"{y}-{int(m):02d}-01"
                            cur.execute("INSERT INTO expensas_ph (inmueble_id, concepto, periodo_mes, periodo_anio, valor_capital, fecha_vencimiento, estado) VALUES (%s, %s, %s, %s, %s, %s, 'Aplicado')", (inmueble_id, concepto, int(m), int(y), valor, f_vencimiento))
            conn.commit()
    except Exception as e:
        print(f"❌ Error actualizando: {e}")
    return RedirectResponse(url="/liquidador", status_code=307)

@app.post("/liquidador/exportar/pdf")
async def exportar_pdf(request: Request, inmueble_id: int = Form(...), tipo_tasa: str = Form(...), tasa_fija: float = Form(2.5), honorarios_pct: float = Form(23.8), gastos: float = Form(0.0), fecha_corte: date = Form(...)):
    from datetime import datetime
    resultados, resumen, inm_info = motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte)
    contexto = {"resultados": resultados, "resumen": resumen, "fecha_generacion": datetime.now().strftime('%Y-%m-%d %H:%M'), "conjunto": f"{inm_info[0]} - {inm_info[1]}", "deudor": f"{inm_info[2]}", "identificacion": f"{inm_info[3]}", "parametros": {"fecha_corte": fecha_corte.strftime('%Y-%m-%d')}}
    return templates.TemplateResponse(request=request, name="liquidacion_pdf.html", context=contexto)

@app.post("/liquidador/exportar/excel")
async def exportar_excel(request: Request, inmueble_id: int = Form(...), tipo_tasa: str = Form(...), tasa_fija: float = Form(2.5), honorarios_pct: float = Form(23.8), gastos: float = Form(0.0), fecha_corte: date = Form(...)):
    from datetime import datetime
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    import io
    resultados, resumen, inm_info = motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Liquidación"
    ws['A1'] = "LIQUIDACIÓN DE CRÉDITO - PROPIEDAD HORIZONTAL"
    ws['A1'].font = Font(bold=True, size=14)
    ws['A2'] = f"FECHA DE GENERACIÓN: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws['A3'] = f"DEMANDANTE / CONJUNTO: {inm_info[0]} - {inm_info[1]}"
    ws['A4'] = f"DEUDOR: {inm_info[2]} (CC/NIT: {inm_info[3]})"
    ws['A5'] = f"FECHA DE CORTE: {fecha_corte.strftime('%Y-%m-%d')}"
    headers = ['Período', 'Ordinaria', 'Extraord.', 'Gastos', 'Abonos', 'Cap. Liquidable', 'Días', 'Tasa E.A.', 'Tasa Mensual', 'Interés Mes', 'Int. Acumulado', 'Saldo Final']
    ws.append([])
    ws.append(headers)
    for cell in ws[7]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")
    row_idx = 8
    for r in resultados:
        ws.cell(row=row_idx, column=1, value=r['desde'][:7])
        ws.cell(row=row_idx, column=2, value=r['ordinarias'])
        ws.cell(row=row_idx, column=3, value=r['extraordinarias'])
        ws.cell(row=row_idx, column=4, value=r['gastos'])
        ws.cell(row=row_idx, column=5, value=r['abonos'])
        ws.cell(row=row_idx, column=7, value=r['dias'])
        ws.cell(row=row_idx, column=8, value=float(r['tasa_ea'].replace('%', '')) / 100.0)
        ws.cell(row=row_idx, column=9, value=f"=(1+H{row_idx})^(1/12)-1")
        if row_idx == 8:
            ws.cell(row=row_idx, column=10, value=f"=ROUND((B{row_idx}+C{row_idx}+D{row_idx})*I{row_idx}*(G{row_idx}/30), 2)")
            ws.cell(row=row_idx, column=11, value=f"=MAX(0, J{row_idx}-E{row_idx})")
            ws.cell(row=row_idx, column=6, value=f"=(B{row_idx}+C{row_idx}+D{row_idx})-MAX(0, E{row_idx}-J{row_idx})")
            ws.cell(row=row_idx, column=12, value=f"=F{row_idx}+K{row_idx}")
        else:
            ws.cell(row=row_idx, column=10, value=f"=ROUND((F{row_idx-1}+B{row_idx}+C{row_idx}+D{row_idx})*I{row_idx}*(G{row_idx}/30), 2)")
            ws.cell(row=row_idx, column=11, value=f"=MAX(0, K{row_idx-1}+J{row_idx}-E{row_idx})")
            ws.cell(row=row_idx, column=6, value=f"=(F{row_idx-1}+B{row_idx}+C{row_idx}+D{row_idx})-MAX(0, E{row_idx}-(K{row_idx-1}+J{row_idx}))")
            ws.cell(row=row_idx, column=12, value=f"=F{row_idx}+K{row_idx}")
        for col in [2,3,4,5,6,10,11,12]: ws.cell(row=row_idx, column=col).number_format = '"$"#,##0'
        for col in [8,9]: ws.cell(row=row_idx, column=col).number_format = '0.0000%'
        row_idx += 1
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length: max_length = len(str(cell.value))
            except: pass
        ws.column_dimensions[column].width = max_length + 2
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=Liquidacion_{inm_info[2].replace(' ', '_')}.xlsx"})

@app.get("/liquidador/plantilla")
def descargar_plantilla_liquidador():
    datos_ejemplo = {"Desde": ["01/01/2025", "01/02/2025"], "Hasta": ["31/01/2025", "28/02/2025"], "ORDINARIAS": [250000, 250000], "EXTRAORDINARIAS": [0, 50000]}
    df = pd.DataFrame(datos_ejemplo)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Cuotas")
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=formato_cuotas_ph.xlsx"})

# ==============================================================================
# --- RUTA 5: CARGA MASIVA RELACIONAL ---
# ==============================================================================
@app.post("/liquidador/carga_masiva")
async def carga_masiva_excel(request: Request, inmueble_id: int = Form(...), tipo_tasa: str = Form(...), tasa_fija: float = Form(2.5), honorarios_pct: float = Form(23.8), gastos: float = Form(0.0), fecha_corte: date = Form(...), archivo: UploadFile = File(None)):
    import io
    import pandas as pd
    from datetime import datetime, date
    import psycopg2
    import os
    import traceback
    print(f"\n🚀 --- INICIANDO CARGA MASIVA PARA INMUEBLE ID: {inmueble_id} ---")
    if archivo and archivo.filename:
        print(f"📂 Archivo recibido en el servidor: {archivo.filename}")
        try:
            contents = await archivo.read()
            df = pd.read_excel(io.BytesIO(contents))
            print(f"📊 Excel leído exitosamente. Contiene {len(df)} filas.")
            df.columns = df.columns.str.strip().str.lower().str.replace('ñ', 'n')
            print(f"🏷️ Columnas detectadas: {df.columns.tolist()}")
            if 'desde' in df.columns:
                meses_es = {'ene': '01', 'feb': '02', 'mar': '03', 'abr': '04', 'may': '05', 'jun': '06', 'jul': '07', 'ago': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dic': '12'}
                def parse_spanish_date(d):
                    if pd.isna(d): return None
                    if isinstance(d, datetime) or isinstance(d, date): return d
                    d_str = str(d).lower().strip()
                    for es, num in meses_es.items(): d_str = d_str.replace(es, f"-{num}-")
                    try: return pd.to_datetime(d_str, dayfirst=True, errors='coerce')
                    except: return None
                df['desde'] = df['desde'].apply(parse_spanish_date)
                print("🗓️ Traducción de fechas completada.")
                cuotas_procesadas = 0
                with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
                    with conn.cursor() as cur:
                        for index, row in df.iterrows():
                            fecha = row['desde']
                            if pd.isna(fecha):
                                print(f"⚠️ Fila {index}: Columna 'desde' vacía o irreconocible. Omitiendo.")
                                continue
                            y, m = fecha.year, fecha.month
                            ord_val = row.get('ordinarias', row.get('ordinaria', 0))
                            ext_val = row.get('extraordinarias', row.get('extraordinaria', 0))
                            conceptos = {'Expensa Ordinaria': ord_val, 'Cuota Extraordinaria': ext_val}
                            for concepto, valor in conceptos.items():
                                try:
                                    if pd.isna(valor) or str(valor).strip() == '': valor_limpio = 0.0
                                    elif isinstance(valor, (int, float)): valor_limpio = float(valor)
                                    else:
                                        v_str = str(valor).replace('$', '').strip()
                                        if '.' in v_str and ',' in v_str: v_str = v_str.replace('.', '').replace(',', '.')
                                        elif ',' in v_str: v_str = v_str.replace(',', '.')
                                        valor_limpio = float(v_str)
                                except Exception as parse_e:
                                    print(f"❌ Error limpiando el valor '{valor}' en fila {index}: {parse_e}")
                                    valor_limpio = 0.0
                                if valor_limpio > 0:
                                    cur.execute("UPDATE expensas_ph SET valor_capital = %s WHERE inmueble_id = %s AND concepto = %s AND periodo_anio = %s AND periodo_mes = %s", (valor_limpio, inmueble_id, concepto, y, m))
                                    if cur.rowcount == 0:
                                        f_vencimiento = f"{y}-{m:02d}-01"
                                        cur.execute("INSERT INTO expensas_ph (inmueble_id, concepto, periodo_mes, periodo_anio, valor_capital, fecha_vencimiento, estado) VALUES (%s, %s, %s, %s, %s, %s, 'En Mora')", (inmueble_id, concepto, m, y, valor_limpio, f_vencimiento))
                                        cuotas_procesadas += 1
                        conn.commit()
                        print(f"✅ ÉXITO SQL: Se procesaron y guardaron {cuotas_procesadas} cuotas nuevas para el inmueble {inmueble_id}.")
            else:
                print("❌ ERROR: El Excel NO tiene una columna llamada 'desde' o 'DESDE'.")
        except Exception:
            print("❌ ERROR CRÍTICO EN CARGA MASIVA:")
            traceback.print_exc()
    else:
        print("⚠️ No se adjuntó ningún archivo Excel en la solicitud.")
    print("🔄 Recalculando y redibujando la pantalla...")
    resultados, resumen, _ = motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte)
    lista_inmuebles = cargar_inmuebles_ph()
    return templates.TemplateResponse(request=request, name="liquidador.html", context={"inmuebles": lista_inmuebles, "resultados": resultados, "resumen": resumen, "parametros": {"inmueble_id": inmueble_id, "tipo_tasa": tipo_tasa, "tasa_fija": tasa_fija, "honorarios_pct": honorarios_pct, "gastos": gastos, "fecha_corte": fecha_corte.strftime('%Y-%m-%d')}})

# ==============================================================================
# --- MÓDULO CRM Y RESTO DE RUTAS EXISTENTES ---
# ==============================================================================
# ... (RESTO DEL CÓDIGO EXISTENTE SIN CAMBIOS) ...

# ==============================================================================
# --- API DEL BOT ---
# ==============================================================================
# Esta ruta existía en main.py antes de crear bot_api.py. La mantenemos como
# compatibilidad, pero ahora delega al endpoint oficial para evitar que una
# versión antigua vuelva a usar gastos= y devuelva HTTP 200 con un error.
@app.post("/api/bot/liquidar")
async def api_bot_liquidar(request: Request):
    from bot_api import liquidar_para_bot
    return await liquidar_para_bot(request)
