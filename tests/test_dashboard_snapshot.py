"""Regresiones: totales de cartera del dashboard solo cambian al pulsar Actualizar.

Contrato:
- GET /dashboard lee el snapshot (no liquida).
- POST /dashboard/actualizar-cartera liquida y guarda.
- Arranque / background no recalcula cartera.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)


class DashboardSnapshotSourceTests(unittest.TestCase):
    def test_get_dashboard_lee_snapshot_no_recalcula(self):
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        get_block = main_src.split("@app.get(\"/dashboard\")", 1)[1].split(
            "@app.post(\"/dashboard/actualizar-cartera\")", 1
        )[0]
        self.assertIn("leer_snapshot_cartera()", get_block)
        self.assertNotIn("actualizar_snapshot_cartera", get_block)
        self.assertNotIn("calcular_totales_cartera", get_block)

    def test_post_actualizar_cartera_recalcula_y_guarda(self):
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        post_block = main_src.split(
            "@app.post(\"/dashboard/actualizar-cartera\")", 1
        )[1].split("@app.get(\"/logout\")", 1)[0]
        self.assertIn("actualizar_snapshot_cartera(", post_block)
        self.assertNotIn("leer_snapshot_cartera", post_block)

    def test_arranque_no_actualiza_snapshot_cartera(self):
        start_src = (ROOT / "start.py").read_text(encoding="utf-8")
        self.assertNotIn("actualizar_snapshot_cartera", start_src)
        self.assertNotIn("calcular_totales_cartera", start_src)
        self.assertNotIn("guardar_snapshot_cartera", start_src)
        self.assertIn("_ejecutar_mantenimiento_segundo_plano", start_src)
        # El hilo de mantenimiento solo migra contraseñas, no liquida cartera.
        self.assertIn("migrate_legacy_passwords", start_src)

    def test_template_solo_refresca_con_boton_post(self):
        tpl = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('method="POST" action="/dashboard/actualizar-cartera"', tpl)
        self.assertIn("Actualizar cartera", tpl)
        self.assertNotIn("setInterval", tpl)
        self.assertNotIn("meta http-equiv=\"refresh\"", tpl.lower())
        self.assertNotIn("htmx", tpl.lower())


class DashboardSnapshotBehaviorTests(unittest.TestCase):
    def test_leer_snapshot_no_invoca_liquidador(self):
        import obligacion_saldo_service as saldo_svc

        fake_row = {
            "capital": 150000.0,
            "intereses": 15000.0,
            "honorarios": 20700.0,
            "valor_cartera": 185700.0,
            "obligaciones_incluidas": 2,
            "obligaciones_sin_deuda": 0,
            "obligaciones_error": 0,
            "fecha_corte": date(2026, 9, 21),
            "calculado_en": "2026-09-21T12:00:00-05:00",
        }
        cur = MagicMock()
        cur.fetchone.return_value = fake_row
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        with patch.object(saldo_svc.db, "get_connection", return_value=conn):
            with patch.object(saldo_svc, "ensure_cartera_snapshot_table"):
                with patch.object(saldo_svc, "calcular_totales_cartera") as calc:
                    with patch.object(saldo_svc, "guardar_snapshot_cartera") as save:
                        data = saldo_svc.leer_snapshot_cartera(conn=conn)

        calc.assert_not_called()
        save.assert_not_called()
        self.assertEqual(data["valor_cartera"], 185700.0)
        self.assertEqual(data["capital"], 150000.0)
        self.assertTrue(data["actualizado"])

    def test_actualizar_snapshot_liquida_y_persiste(self):
        import obligacion_saldo_service as saldo_svc

        totales = {
            "capital": 10.0,
            "intereses": 2.0,
            "honorarios": 1.0,
            "valor_cartera": 13.0,
            "total_actualizado": 13.0,
            "obligaciones_incluidas": 1,
            "obligaciones_sin_deuda": 0,
            "obligaciones_error": 0,
            "saldo_calculado_en": "2026-09-22T10:00:00-05:00",
            "fecha_corte": date(2026, 9, 22),
        }
        with patch.object(saldo_svc, "calcular_totales_cartera", return_value=totales) as calc:
            with patch.object(
                saldo_svc, "guardar_snapshot_cartera", return_value={**totales, "actualizado": True}
            ) as save:
                out = saldo_svc.actualizar_snapshot_cartera(
                    fecha_corte=date(2026, 9, 22),
                    actualizado_por="abogado-1",
                )

        calc.assert_called_once_with(fecha_corte=date(2026, 9, 22))
        save.assert_called_once_with(totales, actualizado_por="abogado-1")
        self.assertEqual(out["valor_cartera"], 13.0)

    def test_vista_dashboard_solo_lee_snapshot(self):
        import main
        import obligacion_saldo_service as saldo_svc

        snapshot = {
            "capital": 1.0,
            "intereses": 0.0,
            "honorarios": 0.0,
            "valor_cartera": 1.0,
            "total_actualizado": 1.0,
            "obligaciones_incluidas": 1,
            "obligaciones_sin_deuda": 0,
            "obligaciones_error": 0,
            "saldo_calculado_en": "2026-09-21",
            "fecha_corte": date(2026, 9, 21),
            "actualizado": True,
        }

        class FakeCursor:
            def __init__(self):
                self._i = 0
                self._rows = [
                    {"total": 3},
                    {"total": 5},
                    {"suma": 0.0},
                ]

            def execute(self, sql, params=None):
                return None

            def fetchone(self):
                row = self._rows[self._i] if self._i < len(self._rows) else None
                self._i += 1
                return row

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def cursor(self, cursor_factory=None):
                return FakeCursor()

            def release(self):
                return None

        request = MagicMock()
        with patch.object(main.db, "get_connection", return_value=FakeConn()):
            with patch.object(main.expedientes_service, "_table_exists", return_value=False):
                with patch.object(saldo_svc, "leer_snapshot_cartera", return_value=snapshot) as leer:
                    with patch.object(saldo_svc, "actualizar_snapshot_cartera") as actualizar:
                        with patch.object(saldo_svc, "calcular_totales_cartera") as calc:
                            with patch.object(main, "render_template", return_value="ok") as render:
                                resp = main.vista_dashboard(request)

        self.assertEqual(resp, "ok")
        leer.assert_called_once()
        actualizar.assert_not_called()
        calc.assert_not_called()
        ctx = render.call_args[0][1]
        self.assertEqual(ctx["totales_cartera"]["valor_cartera"], 1.0)

    def test_post_actualizar_invoca_recalculo(self):
        import main
        import obligacion_saldo_service as saldo_svc

        request = MagicMock()
        request.state.user_id = "user-9"
        totales = {"valor_cartera": 99.0, "capital": 80.0, "obligaciones_incluidas": 2}

        with patch.object(saldo_svc, "actualizar_snapshot_cartera", return_value=totales) as actualizar:
            with patch.object(main, "_redirect", return_value="redir") as redir:
                resp = main.dashboard_actualizar_cartera(request)

        self.assertEqual(resp, "redir")
        actualizar.assert_called_once()
        kwargs = actualizar.call_args.kwargs
        self.assertEqual(kwargs.get("actualizado_por"), "user-9")
        redir.assert_called_once()


if __name__ == "__main__":
    unittest.main()
