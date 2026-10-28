"""Aplica la corrección de 5 cuotas distorsionadas si hay TEST_DATABASE_URL."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DSN = os.getenv("TEST_DATABASE_URL")
SQL_PATH = ROOT / "migrations" / "20261028_0237_corregir_cuotas_distorsionadas_mz2.sql"


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL no configurada")
class AplicarCorreccionEnBdTest(unittest.TestCase):
    def setUp(self):
        import psycopg2

        self.conn = psycopg2.connect(TEST_DSN)
        self.conn.autocommit = False
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS expensas_ph (
                    id serial PRIMARY KEY,
                    inmueble_id integer,
                    concepto varchar(100) NOT NULL,
                    periodo_mes integer NOT NULL,
                    periodo_anio integer NOT NULL,
                    valor_capital numeric(12,2) NOT NULL,
                    fecha_vencimiento date NOT NULL DEFAULT CURRENT_DATE,
                    estado varchar(20) DEFAULT 'En Mora'
                )
                """
            )
            cur.execute("DELETE FROM expensas_ph")
            filas = [
                (10, 8, 45000),
                (10, 9, 45000),
                (10, 7, 35000),  # no se toca
                (14, 8, 35001),
                (14, 9, 35002),
                (14, 9, 99999),  # otro concepto no: espera, mismo concepto rompe unique. usar otro mes
                (19, 9, 35001),
                (19, 8, 35000),
                (12, 9, 45000),  # otro inmueble: no se toca
            ]
            # 14,9 99999 would duplicate 14,9 ordinaria. skip that row.
            filas = [
                (10, 8, 45000),
                (10, 9, 45000),
                (10, 7, 35000),
                (14, 8, 35001),
                (14, 9, 35002),
                (19, 9, 35001),
                (19, 8, 35000),
                (12, 9, 45000),
            ]
            for inm, mes, val in filas:
                cur.execute(
                    """
                    INSERT INTO expensas_ph (inmueble_id, concepto, periodo_mes, periodo_anio, valor_capital)
                    VALUES (%s, 'Expensa Ordinaria', %s, 2026, %s)
                    """,
                    (inm, mes, val),
                )
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.rollback()
            self.conn.close()
        except Exception:
            pass

    def test_update_solo_distorsionadas_y_segunda_pasada_noop(self):
        sql = SQL_PATH.read_text(encoding="utf-8")
        with self.conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                """
                SELECT inmueble_id, periodo_mes, valor_capital
                  FROM expensas_ph
                 ORDER BY inmueble_id, periodo_mes
                """
            )
            got = {(int(r[0]), int(r[1]), float(r[2])) for r in cur.fetchall()}
        esperado = {
            (10, 8, 35000.0),
            (10, 9, 35000.0),
            (10, 7, 35000.0),
            (14, 8, 35000.0),
            (14, 9, 35000.0),
            (19, 9, 35000.0),
            (19, 8, 35000.0),
            (12, 9, 45000.0),
        }
        self.assertEqual(got, esperado)

        with self.conn.cursor() as cur:
            cur.execute(sql)
            self.assertEqual(cur.rowcount, 0)
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
