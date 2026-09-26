"""Deep-link CRM desde expediente: auto-selección de cuenta.

Sin Neon: contratos de plantilla + resolución de query params.
"""
from __future__ import annotations

import os
import sys
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

import main


class _FakeCursor:
    def __init__(self, responses=None):
        self.statements = []
        self._responses = list(responses or [])
        self._idx = 0

    def execute(self, sql, params=None):
        self.statements.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        if self._idx < len(self._responses):
            value = self._responses[self._idx]
            self._idx += 1
            return value
        return None


class CrmDeeplinkExpedienteTests(unittest.TestCase):
    def test_detalle_expediente_link_crm_pasa_radicado_y_conjunto(self):
        html = (ROOT / "templates" / "detalle_expediente_v4.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("radicado_interno={{ proceso.radicado_interno }}", html)
        self.assertIn("conjunto_id={{ proceso.inmueble.conjunto_id }}", html)
        self.assertIn("inmueble_id={{ proceso.inmueble_id }}", html)
        self.assertNotIn("buscar_inmueble=", html)
        self.assertIn("Ver en CRM", html)
        self.assertIn("Ver y gestionar en CRM", html)

    def test_crm_handler_acepta_inmueble_y_buscar_legacy(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("def _resolver_contexto_crm(", source)
        self.assertIn("buscar_inmueble: int | None = None", source)
        self.assertIn("inmueble_id: int | None = None", source)
        self.assertIn("_resolver_contexto_crm(", source)

    def test_resuelve_conjunto_y_inmueble_desde_radicado(self):
        cur = _FakeCursor(
            responses=[{"inmueble_id": 88, "conjunto_id": 35}]
        )
        with patch.object(main.expedientes_service, "_table_exists", return_value=True):
            cid, rid, iid = main._resolver_contexto_crm(
                cur, radicado_interno="EXP-0065"
            )
        self.assertEqual(cid, 35)
        self.assertEqual(rid, "EXP-0065")
        self.assertEqual(iid, 88)
        self.assertIn("WHERE p.radicado_interno=%s", cur.statements[0][0])

    def test_resuelve_radicado_desde_inmueble_legacy_buscar(self):
        cur = _FakeCursor(
            responses=[
                {"conjunto_id": 12},
                {"radicado_interno": "EXP-0042"},
            ]
        )
        with patch.object(main.expedientes_service, "_table_exists", return_value=True):
            cid, rid, iid = main._resolver_contexto_crm(
                cur, buscar_inmueble=501
            )
        self.assertEqual(cid, 12)
        self.assertEqual(rid, "EXP-0042")
        self.assertEqual(iid, 501)

    def test_no_pisa_filtros_manuales_ya_presentes(self):
        cur = _FakeCursor(responses=[])
        with patch.object(main.expedientes_service, "_table_exists", return_value=True):
            cid, rid, iid = main._resolver_contexto_crm(
                cur,
                conjunto_id=7,
                radicado_interno="EXP-0001",
                inmueble_id=9,
            )
        self.assertEqual(cid, 7)
        self.assertEqual(rid, "EXP-0001")
        self.assertEqual(iid, 9)
        self.assertEqual(cur.statements, [])

    def test_sin_params_no_fuerza_seleccion(self):
        cur = _FakeCursor(responses=[])
        with patch.object(main.expedientes_service, "_table_exists", return_value=True):
            cid, rid, iid = main._resolver_contexto_crm(cur)
        self.assertIsNone(cid)
        self.assertIsNone(rid)
        self.assertIsNone(iid)
        self.assertEqual(cur.statements, [])


if __name__ == "__main__":
    unittest.main()
