"""Runner de migraciones: lint estático (siempre) e integración (TEST_DATABASE_URL).

Las pruebas de integración usan una BD PostgreSQL DEDICADA a tests: el nombre
de la BD debe contener "test" porque se hace DROP SCHEMA public CASCADE.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import migrate  # noqa: E402

TEST_DSN = os.getenv("TEST_DATABASE_URL")


def _reset_schema(dsn: str) -> None:
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            name = cur.fetchone()[0]
            if "test" not in name.lower():
                raise RuntimeError(f"Se rechaza resetear una BD que no parece de test: {name}")
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


def _created_tables(sql: str) -> set[str]:
    return {
        m.group(1).lower().replace("public.", "")
        for m in re.finditer(
            r"CREATE TABLE (?:IF NOT EXISTS )?([a-zA-Z_.]+)", sql, flags=re.I
        )
    }


def _altered_tables(sql: str) -> set[str]:
    return {
        m.group(1).lower().replace("public.", "")
        for m in re.finditer(r"ALTER TABLE (?:IF EXISTS )?(?:ONLY )?([a-zA-Z_.]+)", sql, flags=re.I)
    }


class MigrationFilesLintTests(unittest.TestCase):
    def test_versiones_unicas_y_con_formato(self):
        migrations = migrate.discover()
        versions = [m.version for m in migrations]
        self.assertEqual(len(versions), len(set(versions)))
        for m in migrations:
            self.assertRegex(m.version, migrate.VERSION_RE, m.path.name)

    def test_baseline_existe_y_esta_fuera_del_glob(self):
        self.assertTrue(migrate.BASELINE_FILE.exists())
        self.assertNotIn(
            migrate.BASELINE_FILE.stem, [m.version for m in migrate.discover()]
        )

    def test_migraciones_nuevas_sin_control_transaccional(self):
        for m in migrate.discover():
            if m.legacy:
                continue
            self.assertIsNone(
                re.search(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;", m.sql, flags=re.I | re.M),
                f"{m.version} no debe incluir BEGIN/COMMIT: lo gestiona el runner",
            )
            self.assertNotIn("CONCURRENTLY", m.sql.upper(), m.version)

    def test_migraciones_nuevas_son_idempotentes_en_ddl(self):
        for m in migrate.discover():
            if m.legacy:
                continue
            for stmt in re.findall(r"CREATE (?:UNIQUE )?(?:TABLE|INDEX)[^;]*", m.sql, flags=re.I):
                head = " ".join(stmt.split()[:6]).upper()
                self.assertIn("IF NOT EXISTS", head, f"{m.version}: {head}")

    def test_orden_tabla_alterada_existe_antes(self):
        """Regresión: 20261020_actuaciones_proceso_drive alteraba una tabla que
        solo creaba una migración posterior en orden alfabético."""
        baseline_tables = _created_tables(migrate.BASELINE_FILE.read_text(encoding="utf-8"))
        known = set(baseline_tables)
        for m in migrate.discover():
            if m.legacy:
                continue
            created_here = _created_tables(m.sql)
            for table in _altered_tables(m.sql):
                self.assertIn(
                    table,
                    known | created_here,
                    f"{m.version} altera {table} antes de que exista",
                )
            known |= created_here

    def test_proceso_inactivaciones_tiene_migracion_post_baseline(self):
        owners = [
            m.version
            for m in migrate.discover()
            if not m.legacy and "proceso_inactivaciones" in _created_tables(m.sql)
        ]
        self.assertTrue(owners)


class EnsureUpToDateTests(unittest.TestCase):
    def _state(self, **kw):
        base = {"baseline_ok": True, "pending": [], "applied": [], "empty_db": False}
        base.update(kw)
        return base

    def test_check_falla_con_pendientes(self):
        with patch.object(migrate, "status", return_value=self._state(pending=["20261099_x"])):
            with self.assertRaises(migrate.MigrationError) as ctx:
                migrate.ensure_up_to_date("check", log=lambda *_: None)
        self.assertIn("20261099_x", str(ctx.exception))

    def test_check_ok_sin_pendientes(self):
        with patch.object(migrate, "status", return_value=self._state()):
            migrate.ensure_up_to_date("check", log=lambda *_: None)

    def test_check_falla_sin_baseline(self):
        with patch.object(migrate, "status", return_value=self._state(baseline_ok=False)):
            with self.assertRaises(migrate.MigrationError):
                migrate.ensure_up_to_date("check", log=lambda *_: None)

    def test_modo_apply_aplica_y_luego_verifica(self):
        with patch.object(migrate, "apply_pending", return_value=["a"]) as ap, patch.object(
            migrate, "status", return_value=self._state()
        ):
            migrate.ensure_up_to_date("apply", log=lambda *_: None)
        ap.assert_called_once()

    def test_modo_apply_propaga_fallo(self):
        with patch.object(
            migrate, "apply_pending", side_effect=migrate.MigrationError("boom")
        ):
            with self.assertRaises(migrate.MigrationError):
                migrate.ensure_up_to_date("apply", log=lambda *_: None)

    def test_modo_invalido(self):
        with self.assertRaises(migrate.MigrationError):
            migrate.ensure_up_to_date("raro", log=lambda *_: None)

    def test_modo_off_no_toca_la_bd(self):
        with patch.object(migrate, "status", side_effect=AssertionError("no debe llamarse")):
            migrate.ensure_up_to_date("off", log=lambda *_: None)

    def test_main_check_devuelve_codigo_1(self):
        with patch.object(migrate, "status", return_value=self._state(pending=["x"])):
            self.assertEqual(migrate.main(["check"]), 1)

    def test_start_py_invoca_la_guardia_antes_del_preflight(self):
        source = (ROOT / "start.py").read_text(encoding="utf-8")
        self.assertLess(
            source.index("migrate.ensure_up_to_date"),
            source.index("schema_preflight.verify()"),
        )


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL no configurada")
class MigrationRunnerDbTests(unittest.TestCase):
    def setUp(self):
        _reset_schema(TEST_DSN)

    def _table(self, name: str) -> bool:
        import psycopg2

        conn = psycopg2.connect(TEST_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
                return bool(cur.fetchone()[0])
        finally:
            conn.close()

    def test_bd_vacia_aplica_todo_y_es_idempotente(self):
        conn = migrate._connect(TEST_DSN)
        try:
            first = migrate.apply_pending(conn, log=lambda *_: None)
            second = migrate.apply_pending(conn, log=lambda *_: None)
            state = migrate.status(conn)
        finally:
            conn.close()
        self.assertTrue(first[0].startswith("baseline:"))
        self.assertEqual(second, [])
        self.assertEqual(state["pending"], [])
        for table in ("procesos", "proceso_inactivaciones", "actuaciones_proceso", "app_settings"):
            self.assertTrue(self._table(table), table)

    def test_sql_nuevo_se_puede_reejecutar(self):
        import psycopg2

        migrate.apply_pending(migrate._connect(TEST_DSN), log=lambda *_: None)
        conn = psycopg2.connect(TEST_DSN)
        try:
            for m in migrate.discover():
                if m.legacy:
                    continue
                with conn.cursor() as cur:
                    cur.execute(m.sql)
                conn.commit()
        finally:
            conn.close()

    def test_bd_con_esquema_sin_marca_de_baseline_se_rechaza(self):
        import psycopg2

        conn = psycopg2.connect(TEST_DSN)
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE procesos (radicado_interno TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(migrate.MigrationError):
            migrate.apply_pending(migrate._connect(TEST_DSN), log=lambda *_: None)

    def test_pendiente_detectado_aplicado_y_fallo_revierte(self):
        conn = migrate._connect(TEST_DSN)
        try:
            migrate.apply_pending(conn, log=lambda *_: None)
            with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                (tmpdir / "20261201_ok_tabla.sql").write_text(
                    "CREATE TABLE IF NOT EXISTS t_ok (id INT);", encoding="utf-8"
                )
                (tmpdir / "20261202_rota.sql").write_text(
                    "CREATE TABLE t_parcial (id INT);\nSELECT 1/0;", encoding="utf-8"
                )
                migrations = migrate.discover(tmpdir)
                self.assertEqual(
                    migrate.status(conn, migrations)["pending"],
                    ["20261201_ok_tabla", "20261202_rota"],
                )
                with self.assertRaises(migrate.MigrationError) as ctx:
                    migrate.apply_pending(conn, migrations, log=lambda *_: None)
                self.assertIn("20261202_rota", str(ctx.exception))
                state = migrate.status(conn, migrations)
                self.assertEqual(state["pending"], ["20261202_rota"])
        finally:
            conn.close()
        self.assertTrue(self._table("t_ok"))
        self.assertFalse(self._table("t_parcial"))

    def test_dry_run_no_persiste(self):
        migrate.apply_pending(migrate._connect(TEST_DSN), log=lambda *_: None)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20261203_dry.sql").write_text(
                "CREATE TABLE t_dry (id INT);", encoding="utf-8"
            )
            migrations = migrate.discover(Path(tmp))
            done = migrate.apply_pending(
                migrate._connect(TEST_DSN), migrations, dry_run=True, log=lambda *_: None
            )
            self.assertEqual(done, ["20261203_dry"])
            self.assertFalse(self._table("t_dry"))
            self.assertEqual(
                migrate.status(migrate._connect(TEST_DSN), migrations)["pending"],
                ["20261203_dry"],
            )

    def test_guardia_falla_con_pendientes_en_bd_real(self):
        migrate.apply_pending(migrate._connect(TEST_DSN), log=lambda *_: None)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20261204_nueva.sql").write_text("SELECT 1;", encoding="utf-8")
            migrations = migrate.discover(Path(tmp))
            real_status = migrate.status
            with patch.dict(os.environ, {"DATABASE_URL": TEST_DSN}), patch.object(
                migrate, "status", lambda conn=None, migrations_=None: real_status(conn, migrations)
            ):
                with self.assertRaises(migrate.MigrationError) as ctx:
                    migrate.ensure_up_to_date("check", log=lambda *_: None)
        self.assertIn("20261204_nueva", str(ctx.exception))

    def test_runners_concurrentes_no_duplican(self):
        errors: list[Exception] = []
        results: list[list[str]] = []

        def run():
            try:
                results.append(migrate.apply_pending(migrate._connect(TEST_DSN), log=lambda *_: None))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(errors, [])
        applied_runs = [r for r in results if r]
        self.assertEqual(len(applied_runs), 1, results)


if __name__ == "__main__":
    unittest.main()
