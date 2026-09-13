"""Correccion de presentacion para /expedientes.

No modifica ni elimina registros de Neon. Deduplica los litisconsortes
solamente al construir las columnas visibles del listado de expedientes.
"""

import psycopg2
import pandas as pd

import main


def cargar_procesos_general_sin_duplicados():
    conn = None
    try:
        conn = psycopg2.connect(main.os.getenv("DATABASE_URL"))
        query = """
            SELECT
                p.radicado_interno,
                p.radicado_rama,
                p.naturaleza,
                p.juzgado,
                p.etapa_actual,
                p.estado,
                p.pretensiones,
                p.medidas_cautelares,
                p.id_cliente,
                c_dem.nombre AS demandante_db,
                a.nombre AS abogado_asignado,
                STRING_AGG(
                    DISTINCT NULLIF(TRIM(c_ddo.nombre), ''),
                    ' | '
                ) AS demandado,
                STRING_AGG(
                    DISTINCT NULLIF(TRIM(pl.identificacion_demandado), ''),
                    ' | '
                ) AS id_demandado
            FROM procesos p
            LEFT JOIN contactos c_dem
                ON p.id_cliente = c_dem.identificacion
            LEFT JOIN abogados a
                ON p.abogado_id = a.id
            LEFT JOIN procesos_litisconsorcio pl
                ON p.radicado_interno = pl.radicado_interno
            LEFT JOIN contactos c_ddo
                ON pl.identificacion_demandado = c_ddo.identificacion
            GROUP BY
                p.radicado_interno,
                p.radicado_rama,
                p.naturaleza,
                p.juzgado,
                p.etapa_actual,
                p.estado,
                p.pretensiones,
                p.medidas_cautelares,
                p.id_cliente,
                c_dem.nombre,
                a.nombre
            ORDER BY p.radicado_interno DESC
        """
        df = pd.read_sql_query(query, conn)
        return df.fillna("").to_dict(orient="records")
    except Exception as exc:
        print(f"[EXPEDIENTES] Error cargando procesos: {exc}", flush=True)
        return []
    finally:
        if conn is not None:
            conn.close()


main.cargar_procesos_general = cargar_procesos_general_sin_duplicados

print(
    "[EXPEDIENTES] Deduplicacion visual activa: demandados unicos por expediente",
    flush=True,
)
