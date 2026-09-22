"""Tests de perfiles, permisos y panel de usuarios."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

import permisos
import usuarios_service


class PermisosUnitTests(unittest.TestCase):
    def test_perfiles_humanos_completos(self):
        self.assertEqual(
            set(permisos.PERFILES_HUMANOS),
            {"ADMIN", "ABOGADO", "AUXILIAR_COBRO", "CONSULTA"},
        )

    def test_admin_tiene_todo_incluido_usuarios(self):
        p = permisos.permisos_de_perfil(permisos.PERFIL_ADMIN)
        self.assertIn(permisos.NAV_ADMIN_USUARIOS, p)
        self.assertIn(permisos.ACCION_BORRAR, p)
        self.assertIn(permisos.NAV_COBRO_SMS, p)

    def test_abogado_sin_admin_usuarios(self):
        p = permisos.permisos_de_perfil(permisos.PERFIL_ABOGADO)
        self.assertNotIn(permisos.NAV_ADMIN_USUARIOS, p)
        self.assertIn(permisos.NAV_COBRO_SMS, p)
        self.assertIn(permisos.ACCION_BORRAR, p)
        self.assertIn(permisos.NAV_COBRO_SUPERVISION, p)

    def test_auxiliar_sin_borrar_ni_supervision_ni_admin(self):
        p = permisos.permisos_de_perfil(permisos.PERFIL_AUXILIAR)
        self.assertIn(permisos.NAV_COBRO_SMS, p)
        self.assertIn(permisos.NAV_COBRO_CARTAS, p)
        self.assertIn(permisos.ACCION_SMS, p)
        self.assertNotIn(permisos.ACCION_BORRAR, p)
        self.assertNotIn(permisos.NAV_COBRO_SUPERVISION, p)
        self.assertNotIn(permisos.NAV_ADMIN_USUARIOS, p)
        self.assertNotIn(permisos.NAV_PROCESOS, p)

    def test_consulta_solo_lectura(self):
        p = permisos.permisos_de_perfil(permisos.PERFIL_CONSULTA)
        self.assertIn(permisos.NAV_INFORMES, p)
        self.assertIn(permisos.NAV_COBRO_CRM, p)
        self.assertNotIn(permisos.NAV_COBRO_SMS, p)
        self.assertNotIn(permisos.NAV_COBRO_CARTAS, p)
        self.assertNotIn(permisos.ACCION_EDITAR, p)
        self.assertNotIn(permisos.ACCION_BORRAR, p)

    def test_perfil_invalido_cae_en_admin(self):
        self.assertEqual(permisos.normalizar_perfil(None), permisos.PERFIL_ADMIN)
        self.assertEqual(permisos.normalizar_perfil("xyz"), permisos.PERFIL_ADMIN)

    def test_guards_rutas_sensibles(self):
        consulta = permisos.permisos_de_perfil(permisos.PERFIL_CONSULTA)
        self.assertTrue(permisos.denegar_acceso(consulta, "GET", "/sms"))
        self.assertTrue(permisos.denegar_acceso(consulta, "GET", "/cartas-cobro"))
        self.assertTrue(permisos.denegar_acceso(consulta, "GET", "/admin/usuarios"))
        self.assertTrue(permisos.denegar_acceso(consulta, "GET", "/supervision-agente"))
        self.assertFalse(permisos.denegar_acceso(consulta, "GET", "/informes"))
        self.assertTrue(permisos.denegar_acceso(consulta, "POST", "/crm"))

        auxiliar = permisos.permisos_de_perfil(permisos.PERFIL_AUXILIAR)
        self.assertFalse(permisos.denegar_acceso(auxiliar, "GET", "/sms"))
        self.assertTrue(permisos.denegar_acceso(auxiliar, "POST", "/acuerdos/eliminar"))
        self.assertTrue(permisos.denegar_acceso(auxiliar, "GET", "/supervision-agente"))
        self.assertTrue(permisos.denegar_acceso(auxiliar, "GET", "/admin/usuarios"))

        admin = permisos.permisos_de_perfil(permisos.PERFIL_ADMIN)
        self.assertFalse(permisos.denegar_acceso(admin, "GET", "/admin/usuarios"))
        self.assertFalse(permisos.denegar_acceso(admin, "POST", "/acuerdos/eliminar"))


class UsuariosServiceLogicTests(unittest.TestCase):
    def test_columns_con_realdic_trow_no_usa_indice_entero(self):
        """Regresión prod: RealDictCursor + r[0] → KeyError(0) → login_error."""
        cur = MagicMock()
        cur.fetchall.return_value = [
            {"column_name": "id"},
            {"column_name": "email"},
            {"column_name": "password"},
            {"column_name": "perfil_id"},
            {"column_name": "activo"},
        ]
        cols = usuarios_service._columns(cur, "abogados")
        self.assertEqual(cols, {"id", "email", "password", "perfil_id", "activo"})

    def test_columns_con_tupla_sigue_funcionando(self):
        cur = MagicMock()
        cur.fetchall.return_value = [("id",), ("email",)]
        cols = usuarios_service._columns(cur, "abogados")
        self.assertEqual(cols, {"id", "email"})

    def test_cell_realdic_t_sin_indice(self):
        row = {"id": 7, "email": "a@b.com"}
        self.assertEqual(usuarios_service._cell(row, "id", 0), 7)
        # Simula el fallo histórico: no debe lanzar KeyError(0)
        with self.assertRaises(KeyError):
            _ = row[0]

    def test_cols_para_select_sin_join_si_falta_tabla_perfiles(self):
        cur = MagicMock()

        def fake_execute(sql, params=None):
            self._last_sql = sql

        cur.execute.side_effect = fake_execute

        def fake_fetchall():
            if "column_name" in self._last_sql:
                return [
                    {"column_name": "id"},
                    {"column_name": "email"},
                    {"column_name": "password"},
                    {"column_name": "perfil_id"},
                    {"column_name": "activo"},
                ]
            return []

        def fake_fetchone():
            # tabla perfiles no existe
            if "information_schema.tables" in self._last_sql:
                return None
            return None

        cur.fetchall.side_effect = fake_fetchall
        cur.fetchone.side_effect = fake_fetchone
        cols = usuarios_service._cols_para_select_usuario(cur)
        self.assertIn("activo", cols)
        self.assertNotIn("perfil_id", cols)

    def test_obtener_usuario_por_email_fallback_minimo(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        calls = {"n": 0}

        def execute(sql, params=None):
            calls["n"] += 1
            calls["sql"] = sql

        cur.execute.side_effect = execute

        def fetchone():
            sql = calls.get("sql", "")
            if "information_schema.tables" in sql:
                return {"ok": 1}
            if "information_schema.columns" in sql:
                raise RuntimeError("boom-introspeccion")
            if "FROM abogados" in sql and "LOWER(email)" in sql:
                return {
                    "id": 3,
                    "email": "demo@x.com",
                    "nombre": "Demo",
                    "password": "$2b$12$abcdefghijklmnopqrstuv",
                    "perfil_id": None,
                    "activo": True,
                    "perfil_codigo": None,
                    "perfil_nombre": None,
                }
            return None

        def fetchall():
            raise RuntimeError("boom-introspeccion")

        cur.fetchone.side_effect = fetchone
        cur.fetchall.side_effect = fetchall

        with patch.object(usuarios_service.db, "get_connection", return_value=conn):
            # _columns fallará → fallback mínimo
            with patch.object(
                usuarios_service,
                "_cols_para_select_usuario",
                side_effect=RuntimeError("boom"),
            ):
                user = usuarios_service.obtener_usuario_por_email("demo@x.com")
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], 3)
        self.assertEqual(user["email"], "demo@x.com")
        self.assertTrue(user["activo"])

    def test_contexto_fallback_si_db_falla(self):
        with patch.object(usuarios_service, "obtener_usuario_por_id", side_effect=RuntimeError("db")):
            ctx = usuarios_service.contexto_auth_usuario("1")
        self.assertTrue(ctx["activo"])
        self.assertEqual(ctx["perfil_codigo"], permisos.PERFIL_ADMIN)
        self.assertIn(permisos.NAV_ADMIN_USUARIOS, ctx["permisos"])

    def test_contexto_usuario_inactivo(self):
        with patch.object(
            usuarios_service,
            "obtener_usuario_por_id",
            return_value={
                "id": 7,
                "nombre": "Ana",
                "email": "ana@x.com",
                "activo": False,
                "perfil_codigo": "ABOGADO",
                "perfil_nombre": "Abogado",
            },
        ):
            ctx = usuarios_service.contexto_auth_usuario("7")
        self.assertFalse(ctx["activo"])
        self.assertEqual(ctx["perfil_codigo"], permisos.PERFIL_ABOGADO)

    def test_crear_usuario_valida_campos(self):
        with self.assertRaises(usuarios_service.UsuariosError):
            usuarios_service.crear_usuario(
                nombre="", email="a@b.com", password="12345678", perfil_codigo="CONSULTA"
            )
        with self.assertRaises(usuarios_service.UsuariosError):
            usuarios_service.crear_usuario(
                nombre="Ana", email="malo", password="12345678", perfil_codigo="CONSULTA"
            )
        with self.assertRaises(usuarios_service.UsuariosError):
            usuarios_service.crear_usuario(
                nombre="Ana", email="a@b.com", password="corta", perfil_codigo="CONSULTA"
            )


class LoginMiddlewareRegressionTests(unittest.TestCase):
    def test_login_success_con_usuario_sin_perfil(self):
        """Usuarios existentes sin columnas de perfil deben poder entrar."""
        from fastapi.testclient import TestClient
        import main as main_mod

        fake_user = {
            "id": 42,
            "email": "admin@despacho.com",
            "nombre": "Admin",
            "password": "hashed",
            "activo": True,
            "perfil_codigo": None,
            "perfil_nombre": None,
        }

        with patch.object(main_mod.usuarios_service, "obtener_usuario_por_email", return_value=fake_user), patch.object(
            main_mod, "verify_password", return_value=True
        ), patch.object(main_mod, "set_session_cookie") as set_cookie:
            client = TestClient(main_mod.app)
            response = client.post(
                "/login",
                data={"email": "admin@despacho.com", "password": "secreto123"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("/dashboard", response.headers.get("location", ""))
        set_cookie.assert_called()

    def test_login_no_500_si_servicio_lanza_y_se_captura(self):
        from fastapi.testclient import TestClient
        import main as main_mod

        with patch.object(
            main_mod.usuarios_service,
            "obtener_usuario_por_email",
            side_effect=KeyError(0),
        ):
            client = TestClient(main_mod.app)
            response = client.post(
                "/login",
                data={"email": "admin@despacho.com", "password": "secreto123"},
                follow_redirects=False,
            )
        # Sigue siendo 500 controlado (plantilla), no traceback; el fix real
        # evita KeyError en el servicio. Aquí validamos que el middleware no rompe.
        self.assertEqual(response.status_code, 500)
        self.assertIn("text/html", response.headers.get("content-type", ""))

    def test_login_credenciales_invalidas_401(self):
        from fastapi.testclient import TestClient
        import main as main_mod

        with patch.object(main_mod.usuarios_service, "obtener_usuario_por_email", return_value=None):
            client = TestClient(main_mod.app)
            response = client.post(
                "/login",
                data={"email": "nadie@x.com", "password": "x"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 401)


class PoolCursorFailureTests(unittest.TestCase):
    def test_pooled_cursor_exit_no_marca_fallida_por_keyerror(self):
        import db as db_mod

        raw = MagicMock()
        raw_cursor = MagicMock()
        raw.cursor.return_value = raw_cursor
        raw_cursor.__enter__ = MagicMock(return_value=raw_cursor)
        raw_cursor.__exit__ = MagicMock(return_value=False)

        pool = MagicMock()
        conn = db_mod.PooledConnection(raw, pool)
        cur = conn.cursor()
        # Simula KeyError de app dentro del with
        cur.__exit__(KeyError, KeyError(0), None)
        self.assertFalse(conn._failed)

    def test_pooled_cursor_exit_marca_fallida_por_pgerror(self):
        import db as db_mod
        from psycopg2 import OperationalError

        raw = MagicMock()
        raw_cursor = MagicMock()
        raw.cursor.return_value = raw_cursor
        raw_cursor.__enter__ = MagicMock(return_value=raw_cursor)
        raw_cursor.__exit__ = MagicMock(return_value=False)

        pool = MagicMock()
        conn = db_mod.PooledConnection(raw, pool)
        cur = conn.cursor()
        err = OperationalError("server closed the connection")
        cur.__exit__(OperationalError, err, None)
        self.assertTrue(conn._failed)


class IntegracionWiringTests(unittest.TestCase):
    def test_main_monta_router_usuarios_y_guards(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("usuarios_router", source)
        self.assertIn("denegar_acceso", source)
        self.assertIn("_aplicar_contexto_permisos", source)
        self.assertIn("login_inactive", source)

    def test_bot_api_sigue_con_api_key(self):
        source = (ROOT / "bot_api.py").read_text(encoding="utf-8")
        self.assertIn("X-API-Key", source)
        self.assertIn("_require_api_key", source)
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('/api/bot/', main_src)
        self.assertIn("_is_public_path", main_src)

    def test_sms_api_sigue_publico(self):
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('path.startswith("/sms/api/")', main_src)

    def test_migracion_sql_asigna_admin(self):
        sql = (ROOT / "migrations" / "20260922_perfiles_permisos.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS perfiles", sql)
        self.assertIn("ADMIN", sql)
        self.assertIn("perfil_id IS NULL", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS activo", sql)

    def test_menu_base_tiene_usuarios_y_cobro(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn(">Cobro<", base)
        self.assertIn('id="nav-cobro"', base)
        self.assertIn("/admin/usuarios", base)
        self.assertIn("nav.admin_usuarios", base)
        self.assertIn("nav.cobro.sms", base)

    def test_start_asegura_perfiles(self):
        source = (ROOT / "start.py").read_text(encoding="utf-8")
        self.assertIn("ensure_perfiles_schema", source)


if __name__ == "__main__":
    unittest.main()
