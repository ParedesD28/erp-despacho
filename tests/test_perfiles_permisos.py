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
