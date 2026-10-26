"""Inactivar/activar expediente: estado + historial atómicos y errores claros."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("ERP_SESSION_SECRET", "ci-dummy-secret-not-used-in-production-000000")

import main  # noqa: E402

TEST_DSN = os.getenv("TEST_DATABASE_URL")
RADICADO = "EXP-T-0001"


def _request():
    return SimpleNamespace(state=SimpleNamespace(user_id="tester"))


def _query(response) -> dict[str, str]:
    q = parse_qs(urlparse(response.headers["location"]).query)
    return {k: v[0] for k, v in q.items()}


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.conn.statements.append(text)
        self._last = text
        if "INSERT INTO proceso_inactivaciones" in text and self.conn.fail_insert:
            raise RuntimeError("fallo simulado al insertar historial")

    def fetchone(self):
        if "to_regclass" in self._last:
            return {"existe": self.conn.tabla_existe}
        if "SELECT estado FROM procesos" in self._last:
            return {"estado": "Activo"}
        return None


class _FakeConn:
    def __init__(self, *, fail_insert=False, tabla_existe=True):
        self.fail_insert = fail_insert
        self.tabla_existe = tabla_existe
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.released = 0

    def cursor(self, *a, **k):
        return _FakeCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollbacks += 1
        else:
            self.commits += 1
        return False

    def release(self):
        self.released += 1


class InactivarSinBdTests(unittest.TestCase):
    def _call(self, conn, accion="INACTIVAR", motivo="motivo valido"):
        with patch.object(main.db, "get_connection", return_value=conn):
            return main.cambiar_estado_expediente(
                _request(), radicado_interno=RADICADO, accion=accion, motivo=motivo
            )

    def test_fallo_al_insertar_historial_revierte_y_muestra_error(self):
        conn = _FakeConn(fail_insert=True)
        resp = self._call(conn)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 1)
        self.assertIn("error", _query(resp))
        self.assertIn("No se cambió", _query(resp)["error"])
        self.assertNotIn("mensaje", _query(resp))

    def test_error_fuera_del_historial_no_culpa_al_historial(self):
        class BoomConn(_FakeConn):
            def cursor(self_inner, *a, **k):
                raise RuntimeError("boom")

        resp = self._call(BoomConn())
        msg = _query(resp)["error"]
        self.assertNotIn("registrar el historial", msg)
        self.assertIn("paso", msg)

    def test_sin_tabla_historial_no_modifica_estado(self):
        conn = _FakeConn(tabla_existe=False)
        resp = self._call(conn)
        self.assertFalse(any(s.startswith("UPDATE procesos") for s in conn.statements))
        self.assertEqual(conn.commits, 0)
        self.assertIn("migración", _query(resp)["error"])

    def test_camino_feliz_hace_update_e_insert_en_un_commit(self):
        conn = _FakeConn()
        resp = self._call(conn)
        self.assertEqual((conn.commits, conn.rollbacks), (1, 0))
        kinds = [s.split()[0] for s in conn.statements if s.startswith(("UPDATE", "INSERT"))]
        self.assertEqual(kinds, ["UPDATE", "INSERT"])
        self.assertIn("mensaje", _query(resp))

    def test_motivo_corto_no_toca_la_bd(self):
        conn = _FakeConn()
        resp = self._call(conn, motivo="abc")
        self.assertEqual(conn.statements, [])
        self.assertIn("error", _query(resp))

    def test_endpoint_no_hace_ddl_en_runtime(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        block = source.split('@app.post("/expediente/estado"', 1)[1].split(
            '@app.post("/actuacion/nueva")', 1
        )[0]
        self.assertNotIn("CREATE TABLE", block)
        self.assertNotIn("ALTER TABLE", block)


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL no configurada")
class InactivarConBdRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import migrate
        from test_migrate_runner import _reset_schema

        _reset_schema(TEST_DSN)
        migrate.apply_pending(migrate._connect(TEST_DSN), log=lambda *_: None)

    def setUp(self):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        self.raw = psycopg2.connect(TEST_DSN, cursor_factory=RealDictCursor)
        with self.raw.cursor() as cur:
            cur.execute("DELETE FROM proceso_inactivaciones")
            cur.execute("DELETE FROM procesos WHERE radicado_interno=%s", (RADICADO,))
            cur.execute(
                "INSERT INTO procesos (radicado_interno, estado, naturaleza, tipo_cartera, tipo_proceso_id) VALUES (%s, 'Activo', 'EJECUTIVO', 'JURIDICO', 6)",
                (RADICADO,),
            )
        self.raw.commit()
        self.addCleanup(self.raw.close)

    def _conn(self):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        # Cursor por defecto = tupla, igual que el pool de producción.
        raw = psycopg2.connect(TEST_DSN)

        class Wrapper:
            def __enter__(self_inner):
                raw.__enter__()
                return self_inner

            def __exit__(self_inner, *a):
                return raw.__exit__(*a)

            def cursor(self_inner, *args, **kw):
                return raw.cursor(*args, **kw)

            def release(self_inner):
                if not raw.closed:
                    raw.close()

        return Wrapper()

    def _post(self, accion, motivo=""):
        with patch.object(main.db, "get_connection", side_effect=self._conn):
            return main.cambiar_estado_expediente(
                _request(), radicado_interno=RADICADO, accion=accion, motivo=motivo
            )

    def _estado(self):
        with self.raw.cursor() as cur:
            cur.execute("SELECT estado FROM procesos WHERE radicado_interno=%s", (RADICADO,))
            row = cur.fetchone()
        self.raw.commit()
        return row["estado"]

    def _historial(self):
        with self.raw.cursor() as cur:
            cur.execute(
                "SELECT accion, motivo, usuario FROM proceso_inactivaciones "
                "WHERE radicado_interno=%s ORDER BY id",
                (RADICADO,),
            )
            rows = cur.fetchall()
        self.raw.commit()
        return rows

    def test_inactivar_y_activar_dejan_historial(self):
        resp = self._post("INACTIVAR", "cliente pagó todo")
        self.assertIn("mensaje", _query(resp))
        self.assertEqual(self._estado(), "INACTIVO")
        self.assertEqual(
            [(h["accion"], h["motivo"], h["usuario"]) for h in self._historial()],
            [("INACTIVAR", "cliente pagó todo", "tester")],
        )
        self._post("ACTIVAR")
        self.assertEqual(self._estado(), "Activo")
        self.assertEqual([h["accion"] for h in self._historial()], ["INACTIVAR", "ACTIVAR"])

    def test_si_falla_el_insert_el_estado_no_cambia(self):
        with self.raw.cursor() as cur:
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION t_falla_historial() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'historial caido'; END $$ LANGUAGE plpgsql
                """
            )
            cur.execute(
                "CREATE TRIGGER t_falla BEFORE INSERT ON proceso_inactivaciones "
                "FOR EACH ROW EXECUTE FUNCTION t_falla_historial()"
            )
        self.raw.commit()
        try:
            resp = self._post("INACTIVAR", "motivo suficiente")
        finally:
            with self.raw.cursor() as cur:
                cur.execute("DROP TRIGGER t_falla ON proceso_inactivaciones")
            self.raw.commit()
        self.assertIn("No se cambió", _query(resp)["error"])
        self.assertEqual(self._estado(), "Activo")
        self.assertEqual(self._historial(), [])

    def test_camino_completo_con_conexion_de_pool_cursor_tupla(self):
        """Regresión EXP-0022: con cursor tupla fallaba _cols(procesos) antes del INSERT."""
        resp = self._post("INACTIVAR", "motivo suficiente")
        self.assertNotIn("error", _query(resp))
        self.assertEqual(self._estado(), "INACTIVO")
        self.assertEqual(len(self._historial()), 1)

    def test_fallo_se_loguea_con_etapa_y_sqlstate_sin_motivo(self):
        import io
        from contextlib import redirect_stdout

        with self.raw.cursor() as cur:
            cur.execute(
                """
                CREATE OR REPLACE FUNCTION t_falla_historial() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'historial caido'; END $$ LANGUAGE plpgsql
                """
            )
            cur.execute(
                "CREATE TRIGGER t_falla BEFORE INSERT ON proceso_inactivaciones "
                "FOR EACH ROW EXECUTE FUNCTION t_falla_historial()"
            )
        self.raw.commit()
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                self._post("INACTIVAR", "motivo ultrasecreto")
        finally:
            with self.raw.cursor() as cur:
                cur.execute("DROP TRIGGER t_falla ON proceso_inactivaciones")
            self.raw.commit()
        log = buf.getvalue()
        self.assertIn("etapa=registrar_historial", log)
        self.assertIn("sqlstate=P0001", log)
        self.assertIn(RADICADO, log)
        self.assertNotIn("ultrasecreto", log)

    def test_sin_tabla_de_historial_error_claro_y_estado_intacto(self):
        with self.raw.cursor() as cur:
            cur.execute("ALTER TABLE proceso_inactivaciones RENAME TO proceso_inactivaciones_off")
        self.raw.commit()
        try:
            resp = self._post("INACTIVAR", "motivo suficiente")
        finally:
            with self.raw.cursor() as cur:
                cur.execute("ALTER TABLE proceso_inactivaciones_off RENAME TO proceso_inactivaciones")
            self.raw.commit()
        self.assertIn("migración", _query(resp)["error"])
        self.assertEqual(self._estado(), "Activo")


if __name__ == "__main__":
    unittest.main()
