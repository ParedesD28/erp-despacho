"""Portal Cliente PH: permisos, orden numérico, filtro por conjuntos vinculados."""
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
import portal_ph_service


class ClientePhPermisosTests(unittest.TestCase):
    def test_perfil_cliente_ph_solo_portal(self):
        p = permisos.permisos_de_perfil(permisos.PERFIL_CLIENTE_PH)
        self.assertEqual(p, frozenset({permisos.NAV_PORTAL_PH}))
        self.assertNotIn(permisos.NAV_EXPEDIENTES, p)
        self.assertNotIn(permisos.ACCION_EDITAR, p)
        self.assertNotIn(permisos.NAV_ADMIN_USUARIOS, p)

    def test_cliente_ph_en_perfiles_humanos(self):
        self.assertIn(permisos.PERFIL_CLIENTE_PH, permisos.PERFILES_HUMANOS)

    def test_guards_portal_y_resto(self):
        cliente = permisos.permisos_de_perfil(permisos.PERFIL_CLIENTE_PH)
        self.assertFalse(permisos.denegar_acceso(cliente, "GET", "/portal-ph"))
        self.assertTrue(permisos.denegar_acceso(cliente, "GET", "/expedientes"))
        self.assertTrue(permisos.denegar_acceso(cliente, "GET", "/dashboard"))
        self.assertTrue(permisos.denegar_acceso(cliente, "GET", "/admin/usuarios"))
        self.assertTrue(permisos.denegar_acceso(cliente, "POST", "/portal-ph"))

    def test_home_path(self):
        self.assertEqual(
            permisos.home_path_para_perfil(permisos.PERFIL_CLIENTE_PH),
            "/portal-ph",
        )
        self.assertEqual(permisos.home_path_para_perfil("ADMIN"), "/dashboard")


class OrdenUnidadesTests(unittest.TestCase):
    def test_orden_numerico_torre_apto(self):
        items = ["10-1", "2-42", "2-9", "1-201", "TORRE 02 APTO 003"]
        ordenados = sorted(items, key=portal_ph_service.sort_key_unidad)
        self.assertEqual(ordenados[0], "1-201")
        # 2-3 (de TORRE 02 APTO 003) antes que 2-9 y 2-42
        self.assertEqual(ordenados[1], "TORRE 02 APTO 003")
        self.assertEqual(ordenados[2], "2-9")
        self.assertEqual(ordenados[3], "2-42")
        self.assertEqual(ordenados[4], "10-1")


class ListarUnidadesPortalTests(unittest.TestCase):
    def test_filtra_por_conjunto_habilitado_y_ordena(self):
        cur = MagicMock()

        def execute(sql, params=None):
            normalized = " ".join(str(sql).split())
            if "FROM usuario_conjuntos uc" in normalized:
                cur._fetchall = [
                    {"id": 7, "nombre": "CONJUNTO A"},
                    {"id": 8, "nombre": "CONJUNTO B"},
                ]
            elif "FROM inmuebles_ph i" in normalized:
                self.assertEqual(params, (7,))
                cur._fetchall = [
                    {
                        "id": 2,
                        "torre_apto": "10-1",
                        "conjunto_id": 7,
                        "conjunto_nombre": "CONJUNTO A",
                    },
                    {
                        "id": 1,
                        "torre_apto": "2-42",
                        "conjunto_id": 7,
                        "conjunto_nombre": "CONJUNTO A",
                    },
                ]
            elif "FROM inmueble_propietarios" in normalized:
                cur._fetchall = [
                    {
                        "inmueble_id": 1,
                        "nombre": "DEUDOR UNO",
                        "identificacion": "100",
                        "es_principal": True,
                    },
                    {
                        "inmueble_id": 1,
                        "nombre": "COPROPIETARIO",
                        "identificacion": "200",
                        "es_principal": False,
                    },
                    {
                        "inmueble_id": 2,
                        "nombre": "DEUDOR DOS",
                        "identificacion": "300",
                        "es_principal": True,
                    },
                ]
            elif "DISTINCT ON (p.inmueble_id)" in normalized and "tipo_cartera" in normalized:
                cur._fetchall = [
                    {"inmueble_id": 1, "tipo_cartera": "JURIDICO"},
                    {"inmueble_id": 2, "tipo_cartera": "PREJURIDICO"},
                ]
            else:
                cur._fetchall = []

        cur.execute = execute
        cur.fetchall = lambda: list(getattr(cur, "_fetchall", []))
        cur.fetchone = lambda: {"ok": 1}

        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        with patch.object(portal_ph_service, "ensure_usuario_conjuntos_schema", return_value=True):
            with patch.object(portal_ph_service, "_table_exists", return_value=True):
                with patch.object(portal_ph_service.db, "get_connection", return_value=conn):
                    data = portal_ph_service.listar_unidades_portal(
                        99, conjunto_id="7"
                    )

        self.assertEqual(data["conjunto_id"], 7)
        self.assertEqual(len(data["unidades"]), 2)
        self.assertEqual(data["unidades"][0]["torre_apto"], "2-42")
        self.assertEqual(data["unidades"][1]["torre_apto"], "10-1")
        self.assertEqual(data["unidades"][0]["demandado_principal"]["nombre"], "DEUDOR UNO")
        self.assertEqual(data["unidades"][0]["demandado_principal"]["cedula"], "100")
        self.assertEqual(data["unidades"][0]["demandados_extra_count"], 1)
        self.assertEqual(data["unidades"][0]["tipo_cartera"], "JURIDICO")
        self.assertEqual(data["unidades"][1]["tipo_cartera"], "PREJURIDICO")

    def test_conjunto_no_habilitado_cae_al_primero(self):
        with patch.object(
            portal_ph_service,
            "listar_conjuntos_habilitados",
            return_value=[{"id": 3, "nombre": "SOLO ESTE"}],
        ):
            with patch.object(
                portal_ph_service,
                "listar_unidades_portal",
                wraps=portal_ph_service.listar_unidades_portal,
            ):
                pass

        cur = MagicMock()
        calls = {"inmuebles": 0}

        def execute(sql, params=None):
            normalized = " ".join(str(sql).split())
            if "FROM usuario_conjuntos uc" in normalized:
                cur._fetchall = [{"id": 3, "nombre": "SOLO ESTE"}]
            elif "FROM inmuebles_ph i" in normalized:
                calls["inmuebles"] += 1
                self.assertEqual(params, (3,))
                cur._fetchall = []
            else:
                cur._fetchall = []

        cur.execute = execute
        cur.fetchall = lambda: list(getattr(cur, "_fetchall", []))
        cur.fetchone = lambda: {"ok": 1}
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        with patch.object(portal_ph_service, "ensure_usuario_conjuntos_schema", return_value=True):
            with patch.object(portal_ph_service, "_table_exists", return_value=True):
                with patch.object(portal_ph_service.db, "get_connection", return_value=conn):
                    data = portal_ph_service.listar_unidades_portal(
                        99, conjunto_id="999"
                    )

        self.assertEqual(data["conjunto_id"], 3)
        self.assertEqual(calls["inmuebles"], 1)


class BusquedaPortalTests(unittest.TestCase):
    def test_filtra_por_nombre_cedula_y_nomenclatura(self):
        unidades = [
            {
                "torre_apto": "2-42",
                "demandados": [
                    {"nombre": "ANA PEREZ", "cedula": "52000000"},
                    {"nombre": "OTRO", "cedula": "111"},
                ],
            },
            {
                "torre_apto": "9-401",
                "demandados": [{"nombre": "JUAN", "cedula": "900"}],
            },
        ]
        self.assertEqual(
            len(portal_ph_service.filtrar_unidades_por_busqueda(unidades, "2-42")),
            1,
        )
        self.assertEqual(
            portal_ph_service.filtrar_unidades_por_busqueda(unidades, "ana")[0][
                "torre_apto"
            ],
            "2-42",
        )
        self.assertEqual(
            portal_ph_service.filtrar_unidades_por_busqueda(unidades, "52000000")[0][
                "torre_apto"
            ],
            "2-42",
        )
        self.assertEqual(
            portal_ph_service.filtrar_unidades_por_busqueda(unidades, "no-existe"),
            [],
        )


class UiPortalClienteTests(unittest.TestCase):
    def test_template_portal_tiene_columnas_clave(self):
        html = (ROOT / "templates" / "portal_ph.html").read_text(encoding="utf-8")
        self.assertIn("base_portal_ph.html", html)
        self.assertIn("Torre / apto", html)
        self.assertIn("Cuentas asignadas para cobro", html)
        self.assertIn('name="conjunto_id"', html)
        self.assertIn('name="q"', html)
        self.assertIn("DEMANDADO", html)
        self.assertIn("Jurídico", html)
        self.assertIn("Prejurídico", html)
        self.assertIn("/portal-ph", html)

    def test_shell_movil_sin_sidebar_erp(self):
        shell = (ROOT / "templates" / "base_portal_ph.html").read_text(encoding="utf-8")
        self.assertIn("viewport-fit=cover", shell)
        self.assertIn("safe-area-inset", shell)
        self.assertIn("Navegación portal", shell)
        self.assertIn("/logout", shell)
        self.assertNotIn("hover:w-64", shell)
        self.assertNotIn("Radicar Proceso", shell)

    def test_admin_usuarios_vincula_conjuntos(self):
        html = (ROOT / "templates" / "admin_usuarios.html").read_text(encoding="utf-8")
        self.assertIn("bloque-conjuntos-cliente-ph", html)
        self.assertIn('name="conjunto_id"', html)
        self.assertIn("nav.portal_ph", (ROOT / "templates" / "base.html").read_text())

    def test_migration_existe(self):
        path = ROOT / "migrations" / "20261006_usuario_conjuntos_portal_ph.sql"
        self.assertTrue(path.is_file())
        sql = path.read_text(encoding="utf-8")
        self.assertIn("usuario_conjuntos", sql)
        self.assertIn("CLIENTE_PH", sql)


if __name__ == "__main__":
    unittest.main()
