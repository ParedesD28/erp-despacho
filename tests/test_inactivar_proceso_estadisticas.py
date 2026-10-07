"""Inactivar expediente: UI cableada, persistencia y exclusión de KPIs/cartera."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)


class InactivarProcesoUiTests(unittest.TestCase):
    def test_modal_inactivar_tiene_abrir_modal(self):
        tpl = (ROOT / "templates" / "detalle_expediente_v4.html").read_text(encoding="utf-8")
        self.assertIn("function abrirModal(", tpl)
        self.assertIn("onclick=\"abrirModal('modal-inactivar')\"", tpl)
        self.assertIn('action="/expediente/estado"', tpl)
        self.assertIn('value="INACTIVAR"', tpl)
        self.assertIn('value="ACTIVAR"', tpl)

    def test_endpoint_persiste_estado_e_historial(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("UPDATE procesos SET estado=%s WHERE radicado_interno=%s", source)
        self.assertIn("INSERT INTO proceso_inactivaciones", source)
        self.assertIn("Ya+no+pesa+en+estadisticas", source)

    def test_dashboard_e_informes_excluyen_inactivos(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        dash = source.split('@app.get("/dashboard")', 1)[1].split(
            '@app.post("/dashboard/actualizar-cartera")', 1
        )[0]
        self.assertIn("UPPER(COALESCE(estado, 'ACTIVO')) <> 'INACTIVO'", dash)
        self.assertIn("excluye_proceso_inactivo", dash)
        informes = source.split('@app.get("/informes")', 1)[1].split(
            '@app.get("/herramientas/estado-cuenta")', 1
        )[0]
        self.assertIn("UPPER(COALESCE(estado, 'ACTIVO')) <> 'INACTIVO'", informes)

    def test_cartera_sql_excluye_proceso_inactivo(self):
        source = (ROOT / "obligacion_saldo_service.py").read_text(encoding="utf-8")
        block = source.split("def calcular_totales_cartera", 1)[1].split(
            "def ensure_cartera_snapshot_table", 1
        )[0]
        self.assertIn("proceso_obligaciones po_inact", block)
        self.assertIn("UPPER(COALESCE(p_inact.estado, 'ACTIVO')) = 'INACTIVO'", block)


class CarteraExcluyeInactivoBehaviorTests(unittest.TestCase):
    def test_calcular_totales_consulta_filtra_inactivos(self):
        import obligacion_saldo_service as saldo_svc

        executed = []

        class RecordingCursor:
            def execute(self, sql, params=None):
                executed.append(" ".join(str(sql).split()))

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def cursor(self, cursor_factory=None):
                return RecordingCursor()

            def release(self):
                return None

        with patch.object(saldo_svc.db, "get_connection", return_value=FakeConn()):
            with patch.object(saldo_svc, "_saldo_ph", side_effect=AssertionError("no liquidar")):
                totales = saldo_svc.calcular_totales_cartera(fecha_corte=date(2026, 10, 7))

        self.assertEqual(totales["obligaciones_incluidas"], 0)
        self.assertEqual(len(executed), 1)
        self.assertIn("proceso_obligaciones po_inact", executed[0])
        self.assertIn("= 'INACTIVO'", executed[0])


if __name__ == "__main__":
    unittest.main()
