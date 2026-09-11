import psycopg2
from psycopg2.extras import RealDictCursor # Asegúrate de tener esto arriba del todo
from typing import List
import re
import json
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File
import io
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from fastapi.responses import StreamingResponse, HTMLResponse
import os
import psycopg2
import pandas as pd
import bcrypt
import requests
import calendar
import warnings
from datetime import date, datetime
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from fastapi.responses import RedirectResponse

# 🔥 ESTAS SON LAS DOS LÍNEAS QUE FALTAN O QUEDARON ABAJO:
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ==============================================================================
# --- EL GUARDIA DE SEGURIDAD GLOBAL (MIDDLEWARE) ---
# ==============================================================================
@app.middleware("http")
async def validador_general_seguridad(request: Request, call_next):
    # 1. Definimos las únicas rutas públicas (La "calle" donde todos pueden estar)
    rutas_publicas = ["/login", "/logout"]

    # Si el usuario intenta acceder a una ruta pública, lo dejamos pasar libremente
    if request.url.path in rutas_publicas:
        return await call_next(request)

    # 2. Si intenta entrar a CUALQUIER otra parte del ERP, exigimos la credencial
    token = request.cookies.get("token_erp")
    
    if not token:
        # No tiene la cookie secreta: Redirección forzada e inmediata al Login
        return RedirectResponse(url="/login", status_code=303)

    # 3. Si tiene la credencial válida, le abrimos la puerta al módulo que solicitó
    response = await call_next(request)
    return response

# ==============================================================================
# --- RUTAS DE LOGIN Y LOGOUT ---
# ==============================================================================
@app.get("/login")
def vista_login(request: Request):
    # Si el abogado ya inició sesión antes, lo dejamos pasar directo al sistema
    if request.cookies.get("token_erp"):
        return RedirectResponse(url="/liquidador", status_code=303)
    # 🔥 CORRECCIÓN: Sintaxis moderna obligatoria de FastAPI
    return templates.TemplateResponse(request=request, name="login.html", context={})

@app.post("/login")
def procesar_login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                # Validamos las credenciales contra tu tabla real en Neon
                cur.execute("SELECT id, nombre, rol FROM abogados WHERE email = %s AND password = %s", (email, password))
                usuario = cur.fetchone()
                
        if usuario:
            # ¡Credenciales correctas! Preparamos la entrada al Liquidador
            respuesta = RedirectResponse(url="/liquidador", status_code=303)
            
            # 🔥 LA MAGIA INVISIBLE: Creamos la cookie segura HttpOnly
            respuesta.set_cookie(
                key="token_erp",
                value=str(usuario[0]), # Guardamos su ID de abogado
                httponly=True,         # Nadie en el Frontend puede verla ni robarla
                max_age=28800,         # Expira en 8 horas exactas
                samesite="lax"
            )
            return respuesta
        else:
            # 🔥 CORRECCIÓN AQUÍ TAMBIÉN
            return templates.TemplateResponse(request=request, name="login.html", context={"error": "Credenciales incorrectas o usuario no existe."})
            
    except Exception as e:
        print(f"❌ Error en Login: {e}")
        # 🔥 Y CORRECCIÓN AQUÍ
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Error de conexión con la base de datos."})

@app.get("/logout")
def cerrar_sesion():
    # Destruye la cookie y lo devuelve a la calle
    respuesta = RedirectResponse(url="/login", status_code=303)
    respuesta.delete_cookie("token_erp")
    return respuesta

# ==============================================================================
# --- FUNCIÓN MAESTRA PARA CARGAR INMUEBLES (USADA POR TODO EL ERP) ---
# ==============================================================================
def cargar_inmuebles_ph():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute("SELECT id, conjunto_residencial, torre_apto FROM inmuebles_ph ORDER BY conjunto_residencial ASC, torre_apto ASC")
        lista = [{"id": r[0], "conjunto_residencial": r[1], "torre_apto": r[2]} for r in cur.fetchall()]
        cur.close()
        conn.close()
        return lista
    except Exception as e:
        print(f"❌ Error cargando inmuebles: {e}")
        return []

# Silenciamos la queja de Pandas para mantener la consola limpia
warnings.filterwarnings('ignore', message='.*SQLAlchemy connectable.*')

load_dotenv()


from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- FUNCIONES DE SEGURIDAD (ENCRIPTACIÓN) ---
def hashear_password(password_plana):
    return bcrypt.hashpw(password_plana.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verificar_password(password_plana, password_hash):
    if not password_hash.startswith('$2'): 
        return password_plana == password_hash
    return bcrypt.checkpw(password_plana.encode('utf-8'), password_hash.encode('utf-8'))

# --- FUNCIÓN AUXILIAR PARA EL EXCEL ---
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
            except Exception as e:
                nombres.append(i)
    return " / ".join(nombres) if nombres else "DEMANDANTE"

# 1. TU FUNCIÓN EXACTA (Adaptada a FastAPI)
def cargar_procesos_general():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    query = '''SELECT p.radicado_interno, p.juzgado, p.etapa_actual, 
                      c.nombre AS demandante_db, a.nombre AS abogado_asignado 
               FROM procesos p 
               LEFT JOIN clientes c ON p.id_cliente = c.identificacion 
               LEFT JOIN abogados a ON p.abogado_id = a.id'''
    df = pd.read_sql_query(query, conn)
    conn.close()
    # Convertimos la tabla de Pandas en un diccionario para la web
    return df.to_dict(orient="records")

# 2. LA RUTA (El reemplazo de tu botón de Streamlit)
@app.get("/expedientes")
def ver_expedientes(request: Request):
    datos = cargar_procesos_general()
    return templates.TemplateResponse(
        request=request, 
        name="expedientes.html", 
        context={"procesos": datos}
    )
# --- MÓDULO CRM: MONITOREO DE LA IA ---
def cargar_gestiones_crm():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    # Cruzamos el CRM con la tabla de contactos para traer el nombre real
    query = '''
        SELECT g.identificacion_deudor, c.nombre AS deudor_nombre, 
               g.fecha_hora, g.tipo_contacto, g.resumen, 
               g.promesa_pago_fecha, g.usuario 
        FROM gestiones_cartera g 
        LEFT JOIN contactos c ON g.identificacion_deudor = c.identificacion
        ORDER BY g.fecha_hora DESC
    '''
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    # Limpiamos los datos vacíos para que la web no muestre "NaN"
    df = df.fillna("")
    # Formateamos la fecha para que se vea elegante (Año-Mes-Día Hora:Minuto)
    if not df.empty and 'fecha_hora' in df.columns:
        df['fecha_hora'] = pd.to_datetime(df['fecha_hora']).dt.strftime('%Y-%m-%d %H:%M')
        
    return df.to_dict(orient="records")

# LA RUTA DE CARTERA
@app.get("/cartera")
def ver_cartera(request: Request):
    datos_crm = cargar_gestiones_crm()
    return templates.TemplateResponse(
        request=request, 
        name="cartera.html", 
        context={"gestiones": datos_crm}
    )

# LA RUTA DE INICIO (DASHBOARD)
@app.get("/")
def ver_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="dashboard.html", 
        context={}
    )
# --- MÓDULO: VENCIMIENTOS Y AGENDA ---
def cargar_vencimientos_pendientes():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    # Traemos solo lo que está pendiente, ordenado por el más urgente primero
    df = pd.read_sql_query("SELECT * FROM vencimientos WHERE estado = 'Pendiente' ORDER BY fecha_vencimiento ASC", conn)
    conn.close()
    
    df = df.fillna("")
    return df.to_dict(orient="records")

def cargar_radicados_activos():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    # Solo necesitamos el radicado interno para el menú desplegable
    df = pd.read_sql_query("SELECT radicado_interno FROM procesos", conn)
    conn.close()
    
    return df['radicado_interno'].tolist() if not df.empty else ["GENERAL"]

@app.get("/vencimientos")
def ver_vencimientos(request: Request):
    datos_vencimientos = cargar_vencimientos_pendientes()
    lista_radicados = cargar_radicados_activos()
    
    return templates.TemplateResponse(
        request=request, 
        name="vencimientos.html", 
        context={
            "vencimientos": datos_vencimientos,
            "radicados": lista_radicados
        }
    )
# --- MÓDULO: INFORMES Y EXPORTACIÓN ---
@app.get("/informes")
def ver_informes(request: Request):
    # Solo consultamos el total de procesos para la tarjeta de métricas
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM procesos")
    total_p = cursor.fetchone()[0]
    conn.close()
    
    return templates.TemplateResponse(
        request=request, 
        name="informes.html", 
        context={"total_procesos": total_p}
    )

@app.get("/descargar-excel")
def descargar_excel():
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        conn_rep = psycopg2.connect(os.getenv("DATABASE_URL"))
        
        # A. Extraer datos judiciales
        query_procesos = "SELECT p.*, a.nombre AS abogado_responsable FROM procesos p LEFT JOIN abogados a ON p.abogado_id = a.id"
        df_proc_r = pd.read_sql_query(query_procesos, conn_rep)
        df_act_r = pd.read_sql_query("SELECT * FROM actuaciones ORDER BY fecha DESC, id DESC", conn_rep)
        df_venc_r = pd.read_sql_query("SELECT * FROM vencimientos", conn_rep)
        df_gas_r = pd.read_sql_query("SELECT * FROM gastos", conn_rep)
        df_cont_r = pd.read_sql_query("SELECT * FROM contactos", conn_rep)
        
        # B. Extraer datos del CRM
        query_crm = """
            SELECT identificacion_deudor, 'En Cobro Activo' AS estado_cartera, 
                   fecha_hora, tipo_contacto, resumen, promesa_pago_fecha, usuario 
            FROM gestiones_cartera 
            WHERE identificacion_deudor IS NOT NULL
            ORDER BY fecha_hora DESC
        """
        df_crm_r = pd.read_sql_query(query_crm, conn_rep)
        
        # 1. Cruzar el Nombre del Demandante (Asumiendo que tienes la función obtener_nombres_demandantes arriba en tu main.py)
        df_proc_r['nombre_demandante'] = df_proc_r['id_cliente'].apply(lambda x: obtener_nombres_demandantes(x, conn_rep))

        # 2. Lógica de Etapa y Actuación
        actuaciones_consolidadas = {}
        etapa_real = {}
        ultima_actuacion = {}
        
        for rad in df_proc_r['radicado_interno']:
            acts_subset = df_act_r[df_act_r['radicado_interno'] == rad]
            if not acts_subset.empty:
                latest = acts_subset.iloc[0]
                etapa_real[rad] = latest['etapa']
                ultima_actuacion[rad] = latest['descripcion']
                actuaciones_consolidadas[rad] = "\n".join([f"[{a['fecha']}] {a['etapa']} - {a['descripcion']} (Por: {a['usuario']})" for _, a in acts_subset.iterrows()])
            else:
                etapa_real[rad] = "Sin actuaciones registradas"
                ultima_actuacion[rad] = "N/A"
                actuaciones_consolidadas[rad] = "Sin historial"
                
        df_proc_r['Etapa_Procesal_Real'] = df_proc_r['radicado_interno'].map(etapa_real)
        df_proc_r['Ultima_Actuacion'] = df_proc_r['radicado_interno'].map(ultima_actuacion)
        df_proc_r['Historial_Actuaciones'] = df_proc_r['radicado_interno'].map(actuaciones_consolidadas)
        
        # 3. Lógica CRM
        historial_crm_dict = {}
        estado_promesa_dict = {}
        
        if not df_crm_r.empty:
            for cedula, grupo in df_crm_r.groupby('identificacion_deudor'):
                cedula_str = str(cedula).replace('.0', '').strip()
                hist_str = "\n".join([f"[{row['fecha_hora']}] {row['tipo_contacto']}: {row['resumen']} (Por: {row['usuario']})" for _, row in grupo.iterrows()])
                historial_crm_dict[cedula_str] = hist_str
                
                ultima_gestion = grupo.iloc[0]
                if pd.notna(ultima_gestion['promesa_pago_fecha']) and str(ultima_gestion['promesa_pago_fecha']).strip() != "":
                    estado_promesa_dict[cedula_str] = f"PROMESA VIGENTE: {ultima_gestion['promesa_pago_fecha']} ({ultima_gestion['estado_cartera']})"
                else:
                    estado_promesa_dict[cedula_str] = str(ultima_gestion['estado_cartera'])

        def mapear_crm(id_demandados_str, diccionario, es_promesa=False):
            if pd.isna(id_demandados_str) or str(id_demandados_str).strip() == "": 
                return "Sin gestión" if es_promesa else "Sin historial CRM"
            ids = [str(i).replace('.0', '').strip() for i in str(id_demandados_str).split("|")]
            resultados = []
            for i in ids:
                if i in diccionario:
                    if es_promesa:
                        resultados.append(diccionario[i])
                    else:
                        resultados.append(f"--- GESTIÓN DE {i} ---\n{diccionario[i]}")
            if not resultados:
                return "Sin gestión" if es_promesa else "Sin historial CRM"
            return " | ".join(resultados) if es_promesa else "\n\n".join(resultados)

        df_proc_r['Estado_Acuerdo_CRM'] = df_proc_r['id_demandado'].apply(lambda x: mapear_crm(x, estado_promesa_dict, True))
        df_proc_r['Historial_Gestiones_CRM'] = df_proc_r['id_demandado'].apply(lambda x: mapear_crm(x, historial_crm_dict, False))

        columnas_ordenadas = [
            'radicado_interno', 'radicado_rama', 'naturaleza', 'juzgado', 
            'id_cliente', 'nombre_demandante', 'id_demandado', 'demandado', 
            'estado', 'Etapa_Procesal_Real', 'Ultima_Actuacion', 'pretensiones', 
            'medidas_cautelares', 'abogado_responsable', 'Historial_Actuaciones',
            'Estado_Acuerdo_CRM', 'Historial_Gestiones_CRM' 
        ]
        columnas_ordenadas = [col for col in columnas_ordenadas if col in df_proc_r.columns]
        df_proc_r = df_proc_r[columnas_ordenadas]
        
        # 4. Sanitización Anti-Hackeo
        tablas_a_limpiar = [df_proc_r, df_crm_r, df_venc_r, df_gas_r, df_cont_r]
        for df_limpio in tablas_a_limpiar:
            for col in df_limpio.columns:
                if df_limpio[col].dtype == 'object':
                    df_limpio[col] = df_limpio[col].apply(
                        lambda x: f"'{x}" if isinstance(x, str) and str(x).startswith(('=', '+', '-', '@')) else x
                    )
                    
        # 5. Exportación
        df_proc_r.to_excel(writer, sheet_name='Procesos_y_CRM', index=False)
        df_crm_r.to_excel(writer, sheet_name='CRM_Crudo', index=False)
        df_venc_r.to_excel(writer, sheet_name='Vencimientos', index=False)
        df_gas_r.to_excel(writer, sheet_name='Gastos', index=False)
        df_cont_r.to_excel(writer, sheet_name='Directorio', index=False)
        conn_rep.close()

    output.seek(0)
    
    # Preparamos el archivo para que el navegador lo descargue automáticamente
    headers = {
        'Content-Disposition': 'attachment; filename="Reporte_Inteligente_Firma.xlsx"'
    }
    
    return Response(
        content=output.getvalue(), 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 
        headers=headers
    )
# ==============================================================================
# --- MÓDULO LIQUIDADOR FINANCIERO (CON AUTO-CÁLCULO DESDE CRM) ---
# ==============================================================================
@app.get("/liquidador")
def vista_liquidador(
    request: Request, 
    inmueble_id: int = None,         
    tipo_tasa: str = "Máxima Legal", 
    tasa_fija: float = 2.5,
    honorarios_pct: float = 23.8,
    gastos: float = 0.0
):
    from datetime import date
    fecha_corte_obj = date.today()
    
    resultados = []
    resumen = {}
    
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("SELECT id, conjunto_residencial, torre_apto FROM inmuebles_ph ORDER BY conjunto_residencial ASC, torre_apto ASC")
    
    # 🔥 LA SOLUCIÓN: Usar los nombres exactos que el HTML está esperando
    lista_inmuebles = [{"id": r[0], "conjunto_residencial": r[1], "torre_apto": r[2]} for r in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    # Si el usuario llegó desde el botón azul del CRM
    if inmueble_id:
        try:
            resultados, resumen, _ = motor_calculo_judicial(
                inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte_obj
            )
            print(f"✅ Auto-liquidación generada para el inmueble {inmueble_id}")
        except Exception as e:
            print(f"❌ Error en auto-cálculo de liquidación: {e}")

    return templates.TemplateResponse(request=request, name="liquidador.html", context={
        "inmuebles": lista_inmuebles, 
        "resultados": resultados, 
        "resumen": resumen,
        "parametros": {
            "inmueble_id": inmueble_id, 
            "tipo_tasa": tipo_tasa, 
            "tasa_fija": tasa_fija, 
            "honorarios_pct": honorarios_pct, 
            "gastos": gastos, 
            "fecha_corte": fecha_corte_obj.strftime('%Y-%m-%d')
        }
    })

# ==============================================================================
# --- MOTOR CENTRAL MATEMÁTICO (USADO POR HTML, PDF Y EXCEL) ---
# ==============================================================================
def motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos_globales, fecha_corte):
    import calendar 
    from datetime import date
    import pandas as pd
    import psycopg2
    import os

    # 1. AUTO-CAUSACIÓN
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
    except Exception as e: print(f"❌ Error en auto-causación: {e}")

    # 2. DESCARGA DE DATOS
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

    # 3. PIVOT DE CONCEPTOS
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

    # 4. CICLO MATEMÁTICO (REGLAS EXCEL JUDICIAL)
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
        
        # FÓRMULA EXACTA: Capital * Tasa Mensual * (Dias / 30)
        interes_mes = cap_acumulado * tasa_mensual * (dias / 30.0) if cap_acumulado > 0 else 0
        int_acumulado += interes_mes

        if abo_val > 0:
            if abo_val <= int_acumulado:
                int_acumulado -= abo_val 
            else:
                sobrante = abo_val - int_acumulado
                int_acumulado = 0.0
                cap_acumulado -= sobrante

        resultados.append({
            'desde': desde.strftime('%Y-%m-%d'), 'hasta': hasta.strftime('%Y-%m-%d'),
            'tasa_str': str_tasa_combinada, 'tasa_ea': str_tasa_ea, 'tasa_mes': str_tasa_mes,
            'ordinarias': ord_val, 'extraordinarias': ext_val, 'gastos': gas_val, 'abonos': abo_val,
            'capital_liquidable': cap_acumulado, 'dias': dias, 'intereses': interes_mes,
            'int_acumulado': int_acumulado, 'cap_int': cap_acumulado + int_acumulado
        })

        if m == 12: fecha_actual_loop = date(y + 1, 1, 1)
        else: fecha_actual_loop = date(y, m + 1, 1)

    total_capital = cap_acumulado
    total_intereses = int_acumulado
    total_honorarios = (total_capital + total_intereses) * (honorarios_pct / 100.0)
    gran_total = total_capital + total_intereses + total_honorarios + gastos_globales

    resumen = {"capital": total_capital, "intereses": total_intereses, "honorarios_pct": honorarios_pct, "honorarios": total_honorarios, "gastos": gastos_globales, "gran_total": gran_total}
    return resultados, resumen, inm_info

# ==============================================================================
# --- RUTA 1: VISTA PANTALLA HTML ---
# ==============================================================================
@app.post("/liquidador")
def calcular_liquidador(request: Request, inmueble_id: int = Form(...), tipo_tasa: str = Form(...), tasa_fija: float = Form(2.5), honorarios_pct: float = Form(23.8), gastos: float = Form(0.0), fecha_corte: date = Form(...)):
    resultados, resumen, _ = motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte)
    if not resultados:
        return templates.TemplateResponse(request=request, name="liquidador.html", context={"inmuebles": cargar_inmuebles_ph(), "error": "No hay deudas.", "resultados": None})
    return templates.TemplateResponse(request=request, name="liquidador.html", context={
        "inmuebles": cargar_inmuebles_ph(), "resultados": resultados, "resumen": resumen,
        "parametros": {"inmueble_id": inmueble_id, "tipo_tasa": tipo_tasa, "tasa_fija": tasa_fija, "honorarios_pct": honorarios_pct, "gastos": gastos, "fecha_corte": fecha_corte.strftime('%Y-%m-%d')}
    })

# ==============================================================================
# --- RUTA 2: GUARDAR Y RECALCULAR ---
# ==============================================================================
@app.post("/liquidador/actualizar")
async def actualizar_cuotas(request: Request):
    form_data = await request.form()
    inmueble_id = int(form_data.get("inmueble_id"))
    import psycopg2
    import os
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                for key, value in form_data.items():
                    if key.startswith(("ord_", "ext_", "gas_", "abo_")):
                        prefijo, y, m = key.split("_")
                        if prefijo == 'ord': concepto = 'Expensa Ordinaria'
                        elif prefijo == 'ext': concepto = 'Cuota Extraordinaria'
                        elif prefijo == 'gas': concepto = 'Gastos'
                        elif prefijo == 'abo': concepto = 'Abono'
                        valor = float(value) if value else 0.0
                        cur.execute("UPDATE expensas_ph SET valor_capital = %s WHERE inmueble_id = %s AND concepto = %s AND periodo_anio = %s AND periodo_mes = %s", (valor, inmueble_id, concepto, int(y), int(m)))
                        if cur.rowcount == 0 and valor > 0:
                            f_vencimiento = f"{y}-{int(m):02d}-01"
                            cur.execute("INSERT INTO expensas_ph (inmueble_id, concepto, periodo_mes, periodo_anio, valor_capital, fecha_vencimiento, estado) VALUES (%s, %s, %s, %s, %s, %s, 'Aplicado')", (inmueble_id, concepto, int(m), int(y), valor, f_vencimiento))
            conn.commit()
    except Exception as e: print(f"❌ Error actualizando: {e}")
    return RedirectResponse(url="/liquidador", status_code=307)

# ==============================================================================
# --- RUTA 3: EXPORTAR PDF OFICIAL ---
# ==============================================================================
@app.post("/liquidador/exportar/pdf")
async def exportar_pdf(request: Request, inmueble_id: int = Form(...), tipo_tasa: str = Form(...), tasa_fija: float = Form(2.5), honorarios_pct: float = Form(23.8), gastos: float = Form(0.0), fecha_corte: date = Form(...)):
    from datetime import datetime
    resultados, resumen, inm_info = motor_calculo_judicial(inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte)
    
    contexto = {
        "resultados": resultados, "resumen": resumen,
        "fecha_generacion": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "conjunto": f"{inm_info[0]} - {inm_info[1]}",
        "deudor": f"{inm_info[2]}", "identificacion": f"{inm_info[3]}",
        "parametros": {"fecha_corte": fecha_corte.strftime('%Y-%m-%d')}
    }
    
    return templates.TemplateResponse(
        request=request, 
        name="liquidacion_pdf.html", 
        context=contexto
    )

# ==============================================================================
# --- RUTA 4: EXPORTAR EXCEL (FÓRMULAS COLOMBIANAS SIN REFERENCIA CIRCULAR) ---
# ==============================================================================
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

    # Encabezados corporativos
    ws['A1'] = "LIQUIDACIÓN DE CRÉDITO - PROPIEDAD HORIZONTAL"
    ws['A1'].font = Font(bold=True, size=14)
    ws['A2'] = f"FECHA DE GENERACIÓN: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws['A3'] = f"DEMANDANTE / CONJUNTO: {inm_info[0]} - {inm_info[1]}"
    ws['A4'] = f"DEUDOR: {inm_info[2]} (CC/NIT: {inm_info[3]})"
    ws['A5'] = f"FECHA DE CORTE: {fecha_corte.strftime('%Y-%m-%d')}"
    
    headers = ['Período', 'Ordinaria', 'Extraord.', 'Gastos', 'Abonos', 'Cap. Liquidable', 'Días', 'Tasa E.A.', 'Tasa Mensual', 'Interés Mes', 'Int. Acumulado', 'Saldo Final']
    ws.append([]) 
    ws.append(headers)
    
    # Estilo de encabezados
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
        
        # FÓRMULAS MATEMÁTICAS A PRUEBA DE BALAS
        ws.cell(row=row_idx, column=9, value=f"=(1+H{row_idx})^(1/12)-1") 
        
        if row_idx == 8: # Primera Fila
            ws.cell(row=row_idx, column=10, value=f"=ROUND((B{row_idx}+C{row_idx}+D{row_idx})*I{row_idx}*(G{row_idx}/30), 2)")
            ws.cell(row=row_idx, column=11, value=f"=MAX(0, J{row_idx}-E{row_idx})")
            ws.cell(row=row_idx, column=6, value=f"=(B{row_idx}+C{row_idx}+D{row_idx})-MAX(0, E{row_idx}-J{row_idx})") 
            ws.cell(row=row_idx, column=12, value=f"=F{row_idx}+K{row_idx}")
        else: # Siguientes Filas
            ws.cell(row=row_idx, column=10, value=f"=ROUND((F{row_idx-1}+B{row_idx}+C{row_idx}+D{row_idx})*I{row_idx}*(G{row_idx}/30), 2)")
            ws.cell(row=row_idx, column=11, value=f"=MAX(0, K{row_idx-1}+J{row_idx}-E{row_idx})")
            ws.cell(row=row_idx, column=6, value=f"=(F{row_idx-1}+B{row_idx}+C{row_idx}+D{row_idx})-MAX(0, E{row_idx}-(K{row_idx-1}+J{row_idx}))") 
            ws.cell(row=row_idx, column=12, value=f"=F{row_idx}+K{row_idx}")

        # Formatos de Celda
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
        ws.column_dimensions[column].width = (max_length + 2)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    return StreamingResponse(
        output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=Liquidacion_{inm_info[2].replace(' ', '_')}.xlsx"}
    )

# ==============================================================================
# --- RUTA 5: CARGA MASIVA RELACIONAL (CON TRAZABILIDAD EXTREMA) ---
# ==============================================================================
@app.post("/liquidador/carga_masiva")
async def carga_masiva_excel(
    request: Request, 
    inmueble_id: int = Form(...), 
    tipo_tasa: str = Form(...), 
    tasa_fija: float = Form(2.5), 
    honorarios_pct: float = Form(23.8), 
    gastos: float = Form(0.0), 
    fecha_corte: date = Form(...),
    archivo: UploadFile = File(None) 
):
    import io 
    import pandas as pd
    from datetime import datetime, date
    import psycopg2
    import os
    import traceback # 🔥 NUEVO: Para rastrear errores exactos

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
                meses_es = {'ene': '01', 'feb': '02', 'mar': '03', 'abr': '04', 'may': '05', 'jun': '06', 
                            'jul': '07', 'ago': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dic': '12'}
                
                def parse_spanish_date(d):
                    if pd.isna(d): return None
                    if isinstance(d, datetime) or isinstance(d, date): return d
                    d_str = str(d).lower().strip()
                    for es, num in meses_es.items():
                        if es in d_str:
                            d_str = d_str.replace(es, f"-{num}-")
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
                                    if pd.isna(valor) or str(valor).strip() == '':
                                        valor_limpio = 0.0
                                    elif isinstance(valor, (int, float)):
                                        valor_limpio = float(valor)
                                    else:
                                        v_str = str(valor).replace('$', '').strip()
                                        if '.' in v_str and ',' in v_str:
                                            v_str = v_str.replace('.', '').replace(',', '.')
                                        elif ',' in v_str:
                                            v_str = v_str.replace(',', '.')
                                        valor_limpio = float(v_str)
                                except Exception as parse_e:
                                    print(f"❌ Error limpiando el valor '{valor}' en fila {index}: {parse_e}")
                                    valor_limpio = 0.0
                                    
                                if valor_limpio > 0:
                                    # Intentamos actualizar primero
                                    cur.execute("""
                                        UPDATE expensas_ph SET valor_capital = %s 
                                        WHERE inmueble_id = %s AND concepto = %s AND periodo_anio = %s AND periodo_mes = %s
                                    """, (valor_limpio, inmueble_id, concepto, y, m))
                                    
                                    # Si no existía, insertamos
                                    if cur.rowcount == 0:
                                        f_vencimiento = f"{y}-{m:02d}-01"
                                        cur.execute("""
                                            INSERT INTO expensas_ph (inmueble_id, concepto, periodo_mes, periodo_anio, valor_capital, fecha_vencimiento, estado)
                                            VALUES (%s, %s, %s, %s, %s, %s, 'En Mora')
                                        """, (inmueble_id, concepto, m, y, valor_limpio, f_vencimiento))
                                        cuotas_procesadas += 1
                        conn.commit()
                        print(f"✅ ÉXITO SQL: Se procesaron y guardaron {cuotas_procesadas} cuotas nuevas para el inmueble {inmueble_id}.")
            else:
                print("❌ ERROR: El Excel NO tiene una columna llamada 'desde' o 'DESDE'.")
        except Exception as e:
            print(f"❌ ERROR CRÍTICO EN CARGA MASIVA:")
            traceback.print_exc() # 🔥 Esto imprimirá la línea exacta del error
    else:
        print("⚠️ No se adjuntó ningún archivo Excel en la solicitud.")

    print("🔄 Recalculando y redibujando la pantalla...")
    # 1. Siempre redibuja la pantalla
    resultados, resumen, _ = motor_calculo_judicial(
        inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte
    )

    # 2. Re-Cargar inmuebles usando la función maestra
    lista_inmuebles = cargar_inmuebles_ph()

    # 3. Retornamos la vista
    return templates.TemplateResponse(request=request, name="liquidador.html", context={
        "inmuebles": lista_inmuebles,
        "resultados": resultados,
        "resumen": resumen,
        "parametros": {
            "inmueble_id": inmueble_id,
            "tipo_tasa": tipo_tasa,
            "tasa_fija": tasa_fija,
            "honorarios_pct": honorarios_pct,
            "gastos": gastos,
            "fecha_corte": fecha_corte.strftime('%Y-%m-%d')
        }
    })
# ==============================================================================
# --- MÓDULO CRM (ENFOQUE EN INMUEBLES Y MÚLTIPLES PROPIETARIOS) ---
# ==============================================================================
@app.get("/crm")
def vista_crm(request: Request, buscar_inmueble: str = None):
    inmueble_id = None
    
    # Extraemos el ID del inmueble (si viene con nombre o solo número)
    if buscar_inmueble:
        try:
            if " - " in buscar_inmueble:
                inmueble_id = int(buscar_inmueble.split(" - ")[0].strip())
            else:
                inmueble_id = int(buscar_inmueble)
        except:
            pass
            
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    
    # 1. DICCIONARIO DE INMUEBLES AGRUPADOS POR CONJUNTO
    cur.execute("""
        SELECT i.id, COALESCE(i.conjunto_residencial, 'OTROS'), i.torre_apto
        FROM inmuebles_ph i
        ORDER BY i.conjunto_residencial ASC, i.torre_apto ASC
    """)
    relaciones = cur.fetchall()
    
    diccionario_filtro = {}
    for i_id, conjunto, apto in relaciones:
        if conjunto not in diccionario_filtro:
            diccionario_filtro[conjunto] = []
        diccionario_filtro[conjunto].append({"id": i_id, "nombre": apto})
            
    json_filtro = json.dumps(diccionario_filtro)
    
    inmueble_actual = None
    propietarios = []
    historial = []
    conjunto_actual = "TODOS"
    
    # 2. CARGAR EL PERFIL DEL INMUEBLE SELECCIONADO
    if inmueble_id:
        cur.execute("SELECT id, conjunto_residencial, torre_apto FROM inmuebles_ph WHERE id = %s", (inmueble_id,))
        res_inm = cur.fetchone()
        
        if res_inm:
            inmueble_actual = {"id": res_inm[0], "conjunto": res_inm[1], "apto": res_inm[2]}
            conjunto_actual = res_inm[1]
            
            # Cargar TODOS los propietarios vinculados a este inmueble
            cur.execute("""
                SELECT c.identificacion, c.nombre, c.telefono, c.email 
                FROM contactos c
                JOIN inmuebles_ph i ON i.contacto_id = c.id
                WHERE i.id = %s
            """, (inmueble_id,))
            
            for r in cur.fetchall():
                propietarios.append({"identificacion": r[0], "nombre": r[1], "telefono": r[2], "email": r[3]})
            
            # 🔥 AQUÍ ESTÁ EL CAMBIO DE LA PAPELERA (SOLO NOTAS ACTIVAS) 🔥
            if propietarios:
                cedulas = tuple([p['identificacion'] for p in propietarios])
                cur.execute("""
                    SELECT id, fecha_hora, tipo_contacto, resumen, promesa_pago_fecha, usuario, identificacion_deudor 
                    FROM gestiones_cartera 
                    WHERE identificacion_deudor IN %s AND activo = TRUE
                    ORDER BY fecha_hora DESC
                """, (cedulas,))
                
                for r in cur.fetchall():
                    nombre_deudor = next((p['nombre'] for p in propietarios if p['identificacion'] == r[6]), r[6])
                    
                    historial.append({
                        "id": r[0],
                        "fecha": r[1].strftime('%Y-%m-%d %H:%M') if r[1] else '',
                        "tipo": r[2],
                        "resumen": r[3],
                        "promesa": r[4],
                        "usuario": r[5],
                        "deudor_nombre": nombre_deudor
                    })
                
    cur.close()
    conn.close()
    
    return templates.TemplateResponse(
        request=request, 
        name="crm.html", 
        context={
            "json_filtro": json_filtro,
            "conjuntos": sorted(list(diccionario_filtro.keys())),
            "inmueble_actual": inmueble_actual,
            "propietarios": propietarios,
            "conjunto_actual": conjunto_actual,
            "historial": historial
        }
    )

# ==============================================================================
# --- RUTAS DE GUARDADO Y ANULACIÓN DEL CRM ---
# ==============================================================================
@app.post("/crm/guardar")
async def guardar_gestion_manual(
    request: Request,
    inmueble_id: int = Form(...),
    identificacion_deudor: str = Form(...),
    tipo_contacto: str = Form(...),
    resumen: str = Form(...),
    promesa_pago_fecha: str = Form(None)
):
    try:
        fecha_promesa = promesa_pago_fecha if promesa_pago_fecha and promesa_pago_fecha.strip() != "" else None
        
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO gestiones_cartera (identificacion_deudor, tipo_contacto, resumen, promesa_pago_fecha, usuario) 
                    VALUES (%s, %s, %s, %s, %s)
                """, (identificacion_deudor, tipo_contacto, resumen, fecha_promesa, 'Gestor Humano'))
        conn.commit()
    except Exception as e:
        print(f"❌ Error guardando gestión manual: {e}")
        
    return RedirectResponse(url=f"/crm?buscar_inmueble={inmueble_id}", status_code=303)

# 🔥 AQUÍ ESTÁ EL BOTÓN DE BORRAR INYECTADO 🔥
@app.post("/crm/anular")
async def anular_gestion_manual(
    request: Request,
    gestion_id: int = Form(...),
    inmueble_id: int = Form(...)
):
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE gestiones_cartera SET activo = FALSE WHERE id = %s", (gestion_id,))
        conn.commit()
    except Exception as e:
        print(f"❌ Error anulando gestión: {e}")
        
    return RedirectResponse(url=f"/crm?buscar_inmueble={inmueble_id}", status_code=303)
# ==============================================================================
# --- MÓDULO DE CONTACTOS (DIRECTORIO) ---
# ==============================================================================
@app.get("/contactos")
def vista_contactos(request: Request, buscar_cedula: str = None):
    if buscar_cedula and " - " in buscar_cedula:
        buscar_cedula = buscar_cedula.split(" - ")[0].strip()
        
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    
    cur.execute("SELECT identificacion, nombre FROM contactos ORDER BY nombre ASC")
    lista_contactos = [{"identificacion": r[0], "nombre": r[1]} for r in cur.fetchall()]
    
    contacto_actual = None
    
    if buscar_cedula:
        # 🔥 CORRECCIÓN: Buscamos la columna 'tipo'
        cur.execute("""
            SELECT id, identificacion, nombre, tipo, telefono, email, direccion, ciudad 
            FROM contactos WHERE identificacion = %s
        """, (buscar_cedula,))
        res = cur.fetchone()
        
        if res:
            contacto_actual = {
                "id": res[0], "identificacion": res[1], "nombre": res[2], 
                "tipo": res[3], "telefono": res[4], "email": res[5], 
                "direccion": res[6], "ciudad": res[7]
            }
            
    cur.close()
    conn.close()
    
    return templates.TemplateResponse(
        request=request, 
        name="contactos.html", 
        context={"lista_contactos": lista_contactos, "contacto_actual": contacto_actual}
    )

@app.post("/contactos/guardar")
async def guardar_contacto(
    request: Request,
    id_contacto: int = Form(None), 
    identificacion: str = Form(...),
    nombre: str = Form(...),
    tipo: str = Form(...), # 🔥 CORRECCIÓN: Recibimos 'tipo'
    telefono: str = Form(None),
    email: str = Form(None),
    direccion: str = Form(None),
    ciudad: str = Form(None)
):
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                if id_contacto:
                    # 🔥 CORRECCIÓN: Actualizamos 'tipo'
                    cur.execute("""
                        UPDATE contactos 
                        SET nombre=%s, tipo=%s, telefono=%s, email=%s, direccion=%s, ciudad=%s 
                        WHERE id=%s
                    """, (nombre, tipo, telefono, email, direccion, ciudad, id_contacto))
                else:
                    cur.execute("SELECT id FROM contactos WHERE identificacion = %s", (identificacion,))
                    if cur.fetchone():
                        print(f"⚠️ La cédula {identificacion} ya existe.")
                    else:
                        # 🔥 CORRECCIÓN: Insertamos en 'tipo'
                        cur.execute("""
                            INSERT INTO contactos (identificacion, nombre, tipo, telefono, email, direccion, ciudad) 
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (identificacion, nombre, tipo, telefono, email, direccion, ciudad))
            conn.commit()
    except Exception as e:
        print(f"❌ Error guardando contacto: {e}")
        
    return RedirectResponse(url=f"/contactos?buscar_cedula={identificacion}", status_code=303)
# ==============================================================================
# --- MÓDULO DE PROCESOS JUDICIALES ---
# ==============================================================================
@app.get("/procesos") # O el nombre que tenga tu ruta para abrir esta pantalla
def vista_procesos(request: Request):
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # 1. Traer Clientes
                cur.execute("SELECT identificacion, nombre FROM contactos WHERE tipo = 'Cliente' ORDER BY nombre ASC")
                clientes = cur.fetchall()
                
                # 2. Traer Contrapartes
                cur.execute("SELECT identificacion, nombre FROM contactos WHERE tipo = 'Contraparte' ORDER BY nombre ASC")
                contrapartes = cur.fetchall()

                # 3. Traer Abogados (Para el nuevo campo que faltaba)
                cur.execute("SELECT id, nombre FROM abogados ORDER BY nombre ASC")
                abogados = cur.fetchall()
                
        return templates.TemplateResponse(
            "procesos.html", 
            {
                "request": request, 
                "contactos_clientes": clientes, 
                "contactos_contrapartes": contrapartes,
                "abogados": abogados
            }
        )
    except Exception as e:
        print(f"Error cargando datos de Neon: {e}")
        return templates.TemplateResponse("procesos.html", {"request": request, "contactos_clientes": [], "contactos_contrapartes": [], "abogados": []})

@app.post("/procesos/guardar")
async def guardar_proceso(
    request: Request,
    radicado_interno: str = Form(...),
    radicado_rama: str = Form(...),
    naturaleza: str = Form(...),
    juzgado: str = Form(...),
    id_cliente: str = Form(...),
    id_demandado: str = Form(...),
    pretensiones: float = Form(...),
    medidas_cautelares: str = Form(""),
    abogado_id: int = Form(...)
):
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                # 1. Buscar el nombre real del demandado cruzando con su cédula
                cur.execute("SELECT nombre FROM contactos WHERE identificacion = %s", (id_demandado,))
                res_dem = cur.fetchone()
                nombre_demandado = res_dem[0] if res_dem else "SIN NOMBRE"
                
                # 2. Guardar el nuevo proceso
                cur.execute("""
                    INSERT INTO procesos (
                        radicado_interno, radicado_rama, naturaleza, juzgado, 
                        etapa_actual, id_cliente, demandado, id_demandado, 
                        estado, pretensiones, medidas_cautelares, abogado_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    radicado_interno, radicado_rama, naturaleza, juzgado, 
                    "1. Presentación de la demanda", id_cliente, nombre_demandado, id_demandado, 
                    "Activo", pretensiones, medidas_cautelares, abogado_id
                ))
            conn.commit()
    except Exception as e:
        print(f"❌ Error guardando proceso: {e}")
        
def limpiar_identificacion(texto: str) -> str:
    """Rescatada del código antiguo: limpia puntos y comas de la cédula"""
    if not texto: return ""
    return re.sub(r'[.,\s]', '', str(texto)).strip().upper()

@app.post("/crear_expediente_completo")
def crear_expediente_completo(
    request: Request,
    demandantes_existentes: List[str] = Form(default=[]),
    nuevo_dem_id: List[str] = Form(default=[]),
    nuevo_dem_nombre: List[str] = Form(default=[]),
    demandados_existentes: List[str] = Form(default=[]),
    nuevo_ddo_id: List[str] = Form(default=[]),
    nuevo_ddo_nombre: List[str] = Form(default=[]),
    
    conjunto: str = Form(...),
    apto: str = Form(...),
    naturaleza: str = Form(...),
    radicado_rama: str = Form(...),
    
    # --- LOS CAMPOS NUEVOS QUE FALTABAN ---
    juzgado: str = Form(...),
    pretensiones: float = Form(0.0),
    abogado_id: int = Form(...),
    medidas_cautelares: str = Form("")
):
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                
                # 1. PROCESAR DEMANDANTES
                ids_demandantes = list(demandantes_existentes)
                for c_id, c_nom in zip(nuevo_dem_id, nuevo_dem_nombre):
                    id_limpio = limpiar_identificacion(c_id)
                    cur.execute("INSERT INTO contactos (identificacion, nombre, tipo, ciudad) VALUES (%s, %s, 'Cliente', 'PEREIRA') ON CONFLICT DO NOTHING;", (id_limpio, c_nom.strip().upper()))
                    ids_demandantes.append(id_limpio)
                
                # 2. PROCESAR DEMANDADOS
                ids_demandados = list(demandados_existentes)
                for d_id, d_nom in zip(nuevo_ddo_id, nuevo_ddo_nombre):
                    id_limpio = limpiar_identificacion(d_id)
                    cur.execute("INSERT INTO contactos (identificacion, nombre, tipo, ciudad) VALUES (%s, %s, 'Contraparte', 'PEREIRA') ON CONFLICT DO NOTHING;", (id_limpio, d_nom.strip().upper()))
                    ids_demandados.append(id_limpio)
                
                id_cliente_final = " | ".join(ids_demandantes)
                id_demandado_final = " | ".join(ids_demandados)
                
                # 3. CREAR EL INMUEBLE (Asignado al primer demandado)
                deudor_principal_cedula = ids_demandados[0]
                cur.execute("SELECT id FROM contactos WHERE identificacion = %s", (deudor_principal_cedula,))
                resultado_contacto = cur.fetchone()
                contacto_id_interno = resultado_contacto[0] if resultado_contacto else None
                
                cur.execute("INSERT INTO inmuebles_ph (contacto_id, conjunto_residencial, torre_apto) VALUES (%s, %s, %s) RETURNING id;", (contacto_id_interno, conjunto.strip().upper(), apto.strip().upper()))
                nuevo_inmueble_id = cur.fetchone()[0]
                
                # 4. CREAR EL PROCESO (Ahora con todos los campos)
                cur.execute("SELECT nextval('radicado_seq')")
                radicado_interno = f"EXP-{cur.fetchone()[0]:04d}"
                
                cur.execute("""
                    INSERT INTO procesos (
                        radicado_interno, radicado_rama, naturaleza, juzgado, 
                        id_cliente, id_demandado, estado, pretensiones, 
                        medidas_cautelares, abogado_id, inmueble_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s, %s, %s, %s)
                """, (radicado_interno, radicado_rama, naturaleza, juzgado, id_cliente_final, id_demandado_final, pretensiones, medidas_cautelares, abogado_id, nuevo_inmueble_id))
                
            conn.commit()
        return RedirectResponse(url="/procesos", status_code=303)
        
    except Exception as e:
        print(f"Error: {e}")
        return HTMLResponse("❌ Error al procesar el expediente.")
