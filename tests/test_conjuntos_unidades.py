"""Conjuntos: listado de unidades asociadas (torre/apto + demandados)."""
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

import catalogos_service
import permisos


class ListarUnidadesConjuntoTests(unittest.TestCase):
    def test_ordena_y_enriquece_demandados(self):
        cur = MagicMock()

        def execute(sql, params=None):
            normalized = " ".join(str(sql).split())
            if "FROM inmuebles_ph i" in normalized and "WHERE i.conjunto_id" in normalized:
                self.assertEqual(params, (12,))
                cur._fetchall = [
                    {
                        "id": 2,
                        "torre_apto": "10-1",
                        "conjunto_id": 12,
                        "conjunto_nombre": "MIRADOR",
                    },
                    {
                        "id": 1,
                        "torre_apto": "2-42",
                        "conjunto_id": 12,
                        "conjunto_nombre": "MIRADOR",
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

        with patch("portal_ph_service._table_exists", return_value=True):
            unidades = catalogos_service.listar_unidades_conjunto(cur, 12)

        self.assertEqual(len(unidades), 2)
        self.assertEqual(unidades[0]["torre_apto"], "2-42")
        self.assertEqual(unidades[1]["torre_apto"], "10-1")
        self.assertEqual(unidades[0]["demandado_principal"]["nombre"], "DEUDOR UNO")
        self.assertEqual(unidades[0]["demandados_extra_count"], 1)
        self.assertEqual(unidades[0]["tipo_cartera"], "JURIDICO")
        self.assertEqual(unidades[1]["tipo_cartera"], "PREJURIDICO")
        self.assertEqual(unidades[0]["conjunto_id"], 12)

    def test_sin_tabla_inmuebles_retorna_vacio(self):
        cur = MagicMock()
        with patch("portal_ph_service._table_exists", return_value=False):
            self.assertEqual(catalogos_service.listar_unidades_conjunto(cur, 1), [])


class UiConjuntosUnidadesTests(unittest.TestCase):
    def test_template_muestra_unidades_asociadas(self):
        html = (ROOT / "templates" / "conjuntos.html").read_text(encoding="utf-8")
        self.assertIn("Unidades asociadas", html)
        self.assertIn("unidades-asociadas", html)
        self.assertIn("Ver unidades", html)
        self.assertIn("Torre / apto", html)
        self.assertIn("Demandado", html)
        self.assertIn("unidades_count", html)
        self.assertIn("conjunto_actual", html)

    def test_ruta_conjuntos_protegida_por_nav(self):
        self.assertEqual(
            permisos.permiso_requerido_para_ruta("GET", "/conjuntos"),
            permisos.NAV_CONJUNTOS,
        )
        admin = permisos.permisos_de_perfil(permisos.PERFIL_ADMIN)
        self.assertFalse(permisos.denegar_acceso(admin, "GET", "/conjuntos"))
        cliente = permisos.permisos_de_perfil(permisos.PERFIL_CLIENTE_PH)
        self.assertTrue(permisos.denegar_acceso(cliente, "GET", "/conjuntos"))

    def test_servicio_listar_conjuntos_incluye_count(self):
        src = (ROOT / "catalogos_service.py").read_text(encoding="utf-8")
        self.assertIn("unidades_count", src)
        self.assertIn("def listar_unidades_conjunto", src)


if __name__ == "__main__":
    unittest.main()
