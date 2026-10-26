"""Fuente única de verdad de vencimientos: `estado` escribible, `completado` derivado."""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

import vencimientos_estado as ve

MIG_RECONCILIAR = ROOT / "migrations" / "20261026_0235_vencimientos_reconciliar_estado.sql"
MIG_FUENTE_UNICA = ROOT / "migrations" / "20261026_0236_vencimientos_estado_fuente_unica.sql"


class _Cursor:
    def __init__(self, fetchone_rows):
        self.statements = []
        self._rows = list(fetchone_rows)

    def execute(self, sql, params=None):
        self.statements.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.released = False

    def cursor(self, **kw):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def release(self):
        self.released = True


class DerivacionTests(unittest.TestCase):
    def test_completado_se_deriva_solo_de_estado(self):
        self.assertTrue(ve.esta_completado("Completado"))
        self.assertTrue(ve.esta_completado(" completado "))
        self.assertFalse(ve.esta_completado("Pendiente"))
        self.assertFalse(ve.esta_completado(None))
        self.assertFalse(ve.esta_completado(""))
        self.assertFalse(ve.esta_completado("en proceso"))

    def test_normalizar_estado(self):
        self.assertEqual(ve.normalizar_estado("COMPLETADO"), "Completado")
        self.assertEqual(ve.normalizar_estado("lo que sea"), "Pendiente")
        self.assertEqual(ve.normalizar_estado(None), "Pendiente")

    def test_predicado_sql_abierto_no_usa_completado(self):
        self.assertNotIn("completado", ve.sql_abierto().lower().replace("'completado'", ""))
        self.assertIn("v.estado", ve.sql_abierto("v"))

    def test_migracion_declara_columna_generada_y_check(self):
        sql = MIG_FUENTE_UNICA.read_text(encoding="utf-8")
        self.assertIn("GENERATED ALWAYS AS (estado = 'Completado') STORED", sql)
        self.assertIn("CHECK (estado IN ('Pendiente', 'Completado'))", sql)
        self.assertIn("RAISE EXCEPTION", sql)

    def test_migraciones_con_nombre_unico_y_ordenado(self):
        self.assertLess(MIG_RECONCILIAR.name, MIG_FUENTE_UNICA.name)
        nombres = [p.name[:13] for p in (ROOT / "migrations").glob("20261026_*.sql")]
        self.assertEqual(len(nombres), len(set(
            p.name for p in (ROOT / "migrations").glob("20261026_*.sql"))))


class CodigoNoEscribeCompletadoTests(unittest.TestCase):
    def test_ningun_escritor_toca_completado(self):
        for name in ("agenda_service.py", "main.py", "bot_api.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("SET completado", src, msg=name)
            self.assertNotIn("completado=TRUE", src, msg=name)
            self.assertNotIn("completado,tipo", src, msg=name)
            self.assertNotIn("v.completado", src, msg=name)
            self.assertNotIn("COALESCE(completado", src, msg=name)


class EndpointsEstadoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import agenda_service
        import main

        cls.agenda = agenda_service
        cls.main = main
        cls.router = agenda_service._crear_router_agenda()

    def _endpoint(self, name):
        for route in self.router.routes:
            ep = getattr(route, "endpoint", None)
            if ep is not None and ep.__name__ == name:
                return ep
        self.fail(f"endpoint {name} no registrado")

    def _llamar(self, name, fetchone_rows):
        cur = _Cursor(fetchone_rows)
        conn = _Conn(cur)
        with patch.object(self.agenda, "ensure_schema"), \
                patch.object(self.agenda.db, "get_connection", return_value=conn), \
                patch.object(self.agenda, "_audit") as audit:
            resp = self._endpoint(name)(object(), vencimiento_id=7)
        return resp, cur, conn, audit

    def test_completar_escribe_estado_completado_y_no_completado(self):
        fila = {"radicado_interno": "EXP-1", "titulo": "t", "inmueble_id": None}
        resp, cur, conn, audit = self._llamar("agenda_completar_vencimiento", [fila])
        updates = [s for s in cur.statements if s[0].startswith("UPDATE vencimientos")]
        self.assertEqual(updates, [("UPDATE vencimientos SET estado=%s WHERE id=%s", ("Completado", 7))])
        self.assertTrue(conn.released)
        self.assertEqual(audit.call_args.args[2], "COMPLETAR_VENCIMIENTO")
        self.assertIn("completado", resp.headers["location"].lower())

    def test_reabrir_escribe_estado_pendiente(self):
        fila = {"radicado_interno": "EXP-1", "titulo": "t", "inmueble_id": None}
        resp, cur, conn, audit = self._llamar("agenda_reabrir_vencimiento", [fila])
        updates = [s for s in cur.statements if s[0].startswith("UPDATE vencimientos")]
        self.assertEqual(updates, [("UPDATE vencimientos SET estado=%s WHERE id=%s", ("Pendiente", 7))])
        self.assertEqual(audit.call_args.args[2], "REABRIR_VENCIMIENTO")
        self.assertIn("pendiente", resp.headers["location"].lower())

    def test_completar_inexistente_da_404_sin_update(self):
        from fastapi import HTTPException

        cur = _Cursor([None])
        conn = _Conn(cur)
        with patch.object(self.agenda, "ensure_schema"), \
                patch.object(self.agenda.db, "get_connection", return_value=conn), \
                patch.object(self.agenda, "_audit"):
            with self.assertRaises(HTTPException) as ctx:
                self._endpoint("agenda_completar_vencimiento")(object(), vencimiento_id=99)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertFalse([s for s in cur.statements if s[0].startswith("UPDATE")])
        self.assertTrue(conn.released)

    def test_guardar_vencimiento_inserta_estado_pendiente(self):
        from datetime import date

        cur = _Cursor([(1,), [55]])
        conn = _Conn(cur)
        with patch.object(self.agenda, "ensure_schema"), \
                patch.object(self.agenda.db, "get_connection", return_value=conn), \
                patch.object(self.agenda, "_audit"):
            request = types.SimpleNamespace(state=types.SimpleNamespace(user_id=""))
            self._endpoint("agenda_guardar_vencimiento")(
                request, radicado_interno="EXP-1", titulo="x",
                fecha_vencimiento=date(2026, 11, 1), observaciones="", categoria="TERMINO",
                inmueble_id=None, obligacion_id=5,
            )
        inserts = [s for s in cur.statements if s[0].startswith("INSERT INTO vencimientos")]
        self.assertEqual(len(inserts), 1)
        sql, params = inserts[0]
        self.assertIn("estado", sql)
        self.assertNotIn("completado", sql)
        self.assertIn("Pendiente", params)

    def test_listado_y_dashboard_filtran_por_estado(self):
        import inspect

        src = inspect.getsource(self.agenda)
        self.assertIn("sql_abierto()", src)
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("sql_abierto('v')", main_src)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "requiere TEST_DATABASE_URL (Postgres desechable)")
class MigracionPostgresRealTests(unittest.TestCase):
    """Ejecuta las migraciones contra un Postgres de pruebas (NUNCA producción)."""

    def setUp(self):
        import psycopg2

        self.conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
        self.conn.autocommit = True
        self.cur = self.conn.cursor()
        self.cur.execute("DROP SCHEMA IF EXISTS t_venc CASCADE")
        self.cur.execute("CREATE SCHEMA t_venc")
        self.cur.execute("SET search_path TO t_venc")
        self.cur.execute("CREATE TABLE schema_migrations(version text PRIMARY KEY, applied_at timestamp DEFAULT now())")
        self.cur.execute(
            """
            CREATE TABLE vencimientos(
                id serial PRIMARY KEY, titulo text, fecha_vencimiento text,
                estado text DEFAULT 'Pendiente', completado boolean NOT NULL DEFAULT false)
            """
        )
        self.cur.execute("CREATE INDEX idx_vencimientos_fecha ON vencimientos(fecha_vencimiento, completado)")
        self.cur.execute(
            "INSERT INTO vencimientos(estado,completado) VALUES "
            "('Completado',true),('Pendiente',false),('Pendiente',true),('Completado',false),(NULL,true),('COMPLETADO',true)"
        )

    def tearDown(self):
        self.cur.execute("DROP SCHEMA IF EXISTS t_venc CASCADE")
        self.conn.close()

    def _run(self, path):
        self.cur.execute(path.read_text(encoding="utf-8"))

    def test_secuencia_completa_e_idempotencia(self):
        import psycopg2

        with self.assertRaises(psycopg2.Error):
            self._run(MIG_FUENTE_UNICA)  # aborta mientras haya contradicciones

        self._run(MIG_RECONCILIAR)
        self._run(MIG_RECONCILIAR)
        self.cur.execute("SELECT estado, completado FROM vencimientos ORDER BY id")
        self.assertEqual(self.cur.fetchall(), [
            ("Completado", True), ("Pendiente", False), ("Pendiente", False),
            ("Completado", True), ("Pendiente", False), ("Completado", True)])

        self._run(MIG_FUENTE_UNICA)
        self._run(MIG_FUENTE_UNICA)
        self._run(MIG_RECONCILIAR)  # no-op con columna ya derivada

        self.cur.execute("INSERT INTO vencimientos(titulo) VALUES ('nuevo') RETURNING estado, completado")
        self.assertEqual(self.cur.fetchone(), ("Pendiente", False))
        self.cur.execute("UPDATE vencimientos SET estado='Completado' WHERE titulo='nuevo' RETURNING completado")
        self.assertEqual(self.cur.fetchone(), (True,))
        with self.assertRaises(psycopg2.Error):
            self.cur.execute("UPDATE vencimientos SET completado=FALSE WHERE titulo='nuevo'")
        with self.assertRaises(psycopg2.Error):
            self.cur.execute("INSERT INTO vencimientos(estado,completado) VALUES ('Pendiente',true)")
        with self.assertRaises(psycopg2.Error):
            self.cur.execute("UPDATE vencimientos SET estado='Hecho' WHERE titulo='nuevo'")
        with self.assertRaises(psycopg2.Error):
            self.cur.execute("UPDATE vencimientos SET estado=NULL WHERE titulo='nuevo'")
        self.cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname='idx_vencimientos_fecha' AND schemaname='t_venc'")
        self.assertEqual(len(self.cur.fetchall()), 1)


if __name__ == "__main__":
    unittest.main()
