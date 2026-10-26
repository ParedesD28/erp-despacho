"""Regresión: helpers que leen filas deben funcionar con cursor dict y tupla.

Bug de producción: sync_procesos_service._cols hacía r[0] sobre RealDictCursor
(KeyError: 0) en /dashboard cuando existió la tabla actuaciones_proceso.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("ERP_SESSION_SECRET", "ci-dummy-secret-not-used-in-production-000000")

import dbrows  # noqa: E402
import sync_procesos_service as sps  # noqa: E402

TEST_DSN = os.getenv("TEST_DATABASE_URL")


class _Cur:
    """Cursor falso: responde según un fragmento del SQL, en modo dict o tupla."""

    def __init__(self, dict_rows: bool, responses: dict[str, tuple[list[str], list[tuple]]]):
        self.dict_rows = dict_rows
        self.responses = responses
        self.description = None
        self._rows: list = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        for needle, (names, rows) in self.responses.items():
            if needle in text:
                self.description = [(n,) for n in names]
                self._rows = [dict(zip(names, r)) if self.dict_rows else r for r in rows]
                return
        self.description = [("x",)]
        self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _responses():
    return {
        "information_schema.tables": (["?column?"], [(1,)]),
        "information_schema.columns": (
            ["column_name"],
            [("id",), ("drive_url",), ("Drive_File_Id",)],
        ),
        "FROM alertas_seguimiento a": (
            ["id", "titulo", "drive_url"],
            [(1, "Nueva actuación", "http://x"), (2, "Otra", None)],
        ),
        "COUNT(*)": (["total"], [(7,)]),
        "FROM procesos p": (
            ["radicado_interno", "radicado_rama", "abogado_id"],
            [("EXP-1", "66001400300120230012300", 1), ("EXP-2", "corto", 1)],
        ),
    }


class DictAndTupleCursorTests(unittest.TestCase):
    def _both(self):
        for as_dict in (True, False):
            yield as_dict, _Cur(as_dict, _responses())

    def test_cols_dict_y_tupla(self):
        for as_dict, cur in self._both():
            with self.subTest(dict_rows=as_dict):
                self.assertEqual(
                    sps._cols(cur, "actuaciones_proceso"),
                    {"id", "drive_url", "drive_file_id"},
                )

    def test_listar_alertas_recientes_dict_y_tupla(self):
        for as_dict, cur in self._both():
            with self.subTest(dict_rows=as_dict):
                out = sps.listar_alertas_recientes(cur, limit=10)
                self.assertEqual([r["id"] for r in out], [1, 2])
                self.assertEqual(out[0]["drive_url"], "http://x")

    def test_contar_alertas_no_leidas_dict_y_tupla(self):
        for as_dict, cur in self._both():
            with self.subTest(dict_rows=as_dict):
                self.assertEqual(sps.contar_alertas_no_leidas(cur), 7)

    def test_listar_expedientes_activos_dict_y_tupla(self):
        for as_dict, cur in self._both():
            with self.subTest(dict_rows=as_dict):
                out = sps.listar_expedientes_activos_con_radicado(cur)
                self.assertEqual([r["radicado_interno"] for r in out], ["EXP-1"])

    def test_dbrows_helpers(self):
        self.assertEqual(dbrows.first_value({"existe": True}, "existe"), True)
        self.assertEqual(dbrows.first_value((False,), "existe"), False)
        self.assertEqual(dbrows.first_value({"otro": 5}), 5)
        self.assertIsNone(dbrows.row_value(None, "x"))
        self.assertEqual(dbrows.names_set([{"column_name": "A"}, ("b",)]), {"a", "b"})

    def test_no_quedan_acceso_posicional_sobre_information_schema(self):
        for name in ("sync_procesos_service.py", "sms_router.py", "tasas.py", "agenda_service.py", "schema_preflight.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("{str(row[0])", source, name)
            self.assertNotIn("{str(r[0])", source, name)


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL no configurada")
class RealDictCursorTests(unittest.TestCase):
    def test_funciones_sobre_tablas_nuevas_con_realdictcursor(self):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        import migrate
        from test_migrate_runner import _reset_schema

        _reset_schema(TEST_DSN)
        migrate.apply_pending(migrate._connect(TEST_DSN), log=lambda *_: None)
        conn = psycopg2.connect(TEST_DSN)
        try:
            for factory in (RealDictCursor, None):
                with conn.cursor(cursor_factory=factory) as cur:
                    self.assertIn("drive_url", sps._cols(cur, "actuaciones_proceso"))
                    self.assertEqual(sps.listar_alertas_recientes(cur), [])
                    self.assertEqual(sps.contar_alertas_no_leidas(cur), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
