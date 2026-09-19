"""Regresiones ligeras de Fases 11 y 12.

No requieren Neon ni credenciales: validan contratos puros y protegen
reglas que no deben volver a romperse en el CI.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot_api
import sms_router
import expedientes_service
from obligacion_saldo_service import _saldo_generico


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class Fase11ContractsTests(unittest.TestCase):
    def test_normaliza_telefono_colombiano(self):
        self.assertEqual(sms_router.normalizar_telefono("573106927812"), "3106927812")
        self.assertEqual(sms_router.normalizar_telefono("+57 310 692 7812"), "3106927812")
        self.assertIsNone(sms_router.normalizar_telefono("6015551234"))

    def test_calendario_colombiano_incluye_festivos_trasladados(self):
        self.assertTrue(sms_router._es_festivo_colombia(__import__("datetime").datetime(2026, 10, 12)))
        self.assertFalse(sms_router._es_festivo_colombia(__import__("datetime").datetime(2026, 10, 13)))

    def test_balance_generico_usa_capital_y_movimientos(self):
        cur = FakeCursor([
            {
                "id": 99,
                "capital_inicial": 1000000,
                "estado": "ACTIVA",
                "fuente_saldo": "OBLIGACION",
                "tipo_obligacion_codigo": "PAGARE",
            },
            {"variacion": 175000},
        ])
        result = _saldo_generico(cur, 99)
        self.assertTrue(result["saldo_verificado"])
        self.assertEqual(result["saldo_total"], 1175000.0)
        self.assertEqual(result["saldo_fuente"], "OBLIGACION_MOVIMIENTOS")

    def test_ley_2300_bloquea_contacto_previo_en_siete_dias(self):
        cur = FakeCursor([{
            "fecha": "2026-09-18 10:00:00",
            "tipo_contacto": "WhatsApp",
            "resumen": "Contacto de cobranza",
        }])
        motivo = sms_router._contacto_bloqueado_por_ley_2300(cur, 99, 7)
        self.assertIn("WhatsApp", motivo)

    def test_wizard_requiere_mensaje_y_campana_validos(self):
        payload = sms_router.ConfirmarColaRequest(
            tipo_campana="PREJUDICIAL",
            mensajes=[{"contacto_id": 7, "mensaje_texto": "Texto de prueba"}],
        )
        self.assertEqual(payload.mensajes[0].contacto_id, 7)
        self.assertEqual(payload.tipo_campana, "PREJUDICIAL")



    def test_row_value_soporta_cursor_dict_y_tupla(self):
        self.assertEqual(expedientes_service._row_value({"column_name": "tipo_cartera"}, "column_name"), "tipo_cartera")
        self.assertEqual(expedientes_service._row_value(("tipo_cartera",), 0), "tipo_cartera")

    def test_crm_fuente_devuelve_nombre_demandado(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("AS demandado,", source)
        self.assertIn("STRING_AGG(DISTINCT c.nombre, ' | ' ORDER BY c.nombre)", source)

    def test_bootstrap_no_requiere_modulos_legacy_eliminados(self):
        start_source = (ROOT / "start.py").read_text(encoding="utf-8").lower()
        schema_source = (ROOT / "schema_preflight.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("proceso_partes_runtime", start_source)
        self.assertNotIn("proceso_partes_service", start_source)
        self.assertNotIn("deudor_contacto_id", schema_source)
        self.assertIn("20260919_fase14_eliminar_legacy_proceso_obligacion", schema_source)

    def test_codigo_no_referencia_relaciones_legacy_de_proceso_obligacion(self):
        archivos = {
            "main.py",
            "expedientes_service.py",
            "radicacion_service.py",
            "obligaciones_service.py",
            "agenda_service.py",
            "api_recaudos.py",
            "bot_api.py",
            "sms_router.py",
            "schema_preflight.py",
            "start.py",
        }
        prohibidas = (
            "id_cliente",
            "id_demandado",
            "demandante =",
            "demandado =",
            "procesos_litisconsorcio",
            "deudor_contacto_id",
        )
        for name in archivos:
            source = (ROOT / name).read_text(encoding="utf-8").lower()
            for token in prohibidas:
                self.assertNotIn(token, source, msg=f"{name} conserva referencia legacy: {token}")


class Fase11PdfSecurityTests(unittest.TestCase):
    def test_url_pdf_firmada_usa_hmac(self):
        old_secret = bot_api._PDF_SECRET
        try:
            bot_api._PDF_SECRET = "x" * 40
            url = bot_api.build_signed_pdf_url("Estado_Cuenta_7_demo.pdf", "https://erp.example")
            self.assertIn("/api/bot/pdf/Estado_Cuenta_7_demo.pdf?", url)
            query = url.split("?", 1)[1]
            values = dict(item.split("=", 1) for item in query.split("&"))
            self.assertEqual(
                values["token"],
                bot_api._sign("Estado_Cuenta_7_demo.pdf", int(values["expires"])),
            )
        finally:
            bot_api._PDF_SECRET = old_secret

    def test_pdf_bloquea_traversal(self):
        old_secret = bot_api._PDF_SECRET
        try:
            bot_api._PDF_SECRET = "x" * 40
            with self.assertRaises(Exception) as ctx:
                bot_api.servir_pdf_bot("../secreto.pdf", int(__import__("time").time()) + 60, "x")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 404)
        finally:
            bot_api._PDF_SECRET = old_secret

    def test_fecha_corte_futura_rechazada(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with self.assertRaises(Exception) as ctx:
            bot_api._parse_payload({
                "obligacion_id": 1,
                "fecha_corte": tomorrow,
            })
        self.assertEqual(getattr(ctx.exception, "status_code", None), 422)


if __name__ == "__main__":
    unittest.main()
