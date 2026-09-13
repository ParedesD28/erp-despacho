"""Motor de liquidacion con tasas SFC validadas o tasa personalizada E.A."""
from datetime import date
import calendar
import os
import psycopg2
import pandas as pd
import main


def motor_calculo_judicial_corregido(
    inmueble_id,
    tipo_tasa,
    tasa_fija,
    honorarios_pct,
    gastos_globales,
    fecha_corte,
):
    inmueble_id = int(inmueble_id)
    fecha_corte = date.fromisoformat(str(fecha_corte)) if not isinstance(fecha_corte, date) else fecha_corte

    # Mantener la auto-causacion existente: si existe una ultima cuota ordinaria,
    # genera las cuotas mensuales faltantes hasta la fecha de corte.
    try:
        with psycopg2.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT periodo_anio, periodo_mes, valor_capital
                    FROM expensas_ph
                    WHERE inmueble_id = %s AND concepto = 'Expensa Ordinaria'
                    ORDER BY periodo_anio DESC, periodo_mes DESC
                    LIMIT 1
                """, (inmueble_id,))
                ultima = cur.fetchone()
                if ultima:
                    u_anio, u_mes, u_valor = ultima
                    if u_mes == 12:
                        sig_anio, sig_mes = u_anio + 1, 1
                    else:
                        sig_anio, sig_mes = u_anio, u_mes + 1
                    fecha_siguiente = date(sig_anio, sig_mes, 1)
                    corte_mes = date(fecha_corte.year, fecha_corte.month, 1)
                    while fecha_siguiente <= corte_mes:
                        cur.execute("""
                            SELECT 1
                            FROM expensas_ph
                            WHERE inmueble_id=%s AND concepto='Expensa Ordinaria'
                              AND periodo_anio=%s AND periodo_mes=%s
                            LIMIT 1
                        """, (inmueble_id, sig_anio, sig_mes))
                        if not cur.fetchone():
                            cur.execute("""
                                INSERT INTO expensas_ph
                                    (inmueble_id, concepto, periodo_mes, periodo_anio,
                                     valor_capital, fecha_vencimiento, estado)
                                VALUES (%s, 'Expensa Ordinaria', %s, %s, %s, %s, 'En Mora')
                            """, (
                                inmueble_id,
                                sig_mes,
                                sig_anio,
                                u_valor,
                                fecha_siguiente.strftime('%Y-%m-%d'),
                            ))
                        if sig_mes == 12:
                            sig_anio, sig_mes = sig_anio + 1, 1
                        else:
                            sig_mes += 1
                        fecha_siguiente = date(sig_anio, sig_mes, 1)
            conn.commit()
    except Exception as exc:
        print(f"[LIQUIDADOR][ALERTA] Error en auto-causacion: {exc!r}", flush=True)

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    try:
        df_deuda = pd.read_sql_query(
            """
            SELECT concepto, periodo_mes, periodo_anio, valor_capital
            FROM expensas_ph
            WHERE inmueble_id = %s
            """,
            conn,
            params=(inmueble_id,),
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT conjunto_residencial, torre_apto, nombre, identificacion
                FROM inmuebles_ph i
                JOIN contactos c ON i.contacto_id = c.id
                WHERE i.id = %s
            """, (inmueble_id,))
            inm_info = cur.fetchone()
    finally:
        conn.close()

    if df_deuda.empty:
        return [], {}, inm_info

    # Agrupar movimientos por periodo y concepto, igual que el motor anterior.
    df_agrupado = (
        df_deuda.groupby(['periodo_anio', 'periodo_mes', 'concepto'])['valor_capital']
        .sum()
        .unstack(fill_value=0)
        .reset_index()
    )
    for col in ['Expensa Ordinaria', 'Cuota Extraordinaria', 'Gastos', 'Abono']:
        if col not in df_agrupado.columns:
            df_agrupado[col] = 0.0
    df_agrupado = df_agrupado.sort_values(by=['periodo_anio', 'periodo_mes'])

    resultados = []
    cap_acumulado = 0.0
    int_acumulado = 0.0
    primer_anio = int(df_agrupado['periodo_anio'].min())
    primer_mes = int(
        df_agrupado[df_agrupado['periodo_anio'] == primer_anio]['periodo_mes'].min()
    )
    fecha_actual_loop = date(primer_anio, primer_mes, 1)

    es_fija = "Fija" in str(tipo_tasa)
    if es_fija:
        tasa_ea = max(float(tasa_fija), 0.0) / 100.0
        tasa_mensual_fija = ((1.0 + tasa_ea) ** (1.0 / 12.0)) - 1.0
        print(
            f"[LIQUIDADOR] TASA PERSONALIZADA: E.A.={tasa_ea*100:.4f}% / "
            f"MENSUAL={tasa_mensual_fija*100:.6f}%",
            flush=True,
        )

    while fecha_actual_loop <= fecha_corte:
        y, m = fecha_actual_loop.year, fecha_actual_loop.month
        _, last_day = calendar.monthrange(y, m)
        dias = (
            fecha_corte.day
            if (y == fecha_corte.year and m == fecha_corte.month)
            else last_day
        )
        desde = date(y, m, 1)
        hasta = (
            fecha_corte
            if (y == fecha_corte.year and m == fecha_corte.month)
            else date(y, m, last_day)
        )

        fila = df_agrupado[
            (df_agrupado['periodo_anio'] == y) &
            (df_agrupado['periodo_mes'] == m)
        ]
        ord_val = float(fila['Expensa Ordinaria'].values[0]) if not fila.empty else 0.0
        ext_val = float(fila['Cuota Extraordinaria'].values[0]) if not fila.empty else 0.0
        gas_val = float(fila['Gastos'].values[0]) if not fila.empty else 0.0
        abo_val = float(fila['Abono'].values[0]) if not fila.empty else 0.0

        cap_mes = ord_val + ext_val + gas_val
        cap_acumulado += cap_mes

        if es_fija:
            tasa_ea_periodo = tasa_ea
            tasa_mensual = tasa_mensual_fija
        else:
            # Esta es la misma funcion que mantiene la cache Neon/SFC validada.
            tasa_ea_periodo = float(main.obtener_tasa_bd_o_api(y, m))
            tasa_mensual = ((1.0 + tasa_ea_periodo) ** (1.0 / 12.0)) - 1.0

        str_tasa_ea = f"{tasa_ea_periodo * 100:.2f}%"
        str_tasa_mes = f"{tasa_mensual * 100:.4f}%"
        str_tasa_combinada = f"EA: {str_tasa_ea} (Mes: {str_tasa_mes})"

        interes_mes = (
            cap_acumulado * tasa_mensual * (dias / 30.0)
            if cap_acumulado > 0 else 0.0
        )
        int_acumulado += interes_mes

        if abo_val > 0:
            if abo_val <= int_acumulado:
                int_acumulado -= abo_val
            else:
                sobrante = abo_val - int_acumulado
                int_acumulado = 0.0
                cap_acumulado -= sobrante

        resultados.append({
            'desde': desde.strftime('%Y-%m-%d'),
            'hasta': hasta.strftime('%Y-%m-%d'),
            'tasa_str': str_tasa_combinada,
            'tasa_ea': str_tasa_ea,
            'tasa_mes': str_tasa_mes,
            'ordinarias': ord_val,
            'extraordinarias': ext_val,
            'gastos': gas_val,
            'abonos': abo_val,
            'capital_liquidable': cap_acumulado,
            'dias': dias,
            'intereses': interes_mes,
            'int_acumulado': int_acumulado,
            'cap_int': cap_acumulado + int_acumulado,
        })

        if m == 12:
            fecha_actual_loop = date(y + 1, 1, 1)
        else:
            fecha_actual_loop = date(y, m + 1, 1)

    total_capital = cap_acumulado
    total_intereses = int_acumulado
    total_honorarios = (total_capital + total_intereses) * (honorarios_pct / 100.0)
    gran_total = total_capital + total_intereses + total_honorarios + gastos_globales
    resumen = {
        "capital": total_capital,
        "intereses": total_intereses,
        "honorarios_pct": honorarios_pct,
        "honorarios": total_honorarios,
        "gastos": gastos_globales,
        "gran_total": gran_total,
    }
    return resultados, resumen, inm_info


main.motor_calculo_judicial = motor_calculo_judicial_corregido
print("[LIQUIDADOR] Motor matematico corregido activo: tasas SFC/Neon exactas + personalizada E.A.", flush=True)
