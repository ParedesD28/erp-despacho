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

from jinja2 import DictLoader, Environment

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




    def test_template_expediente_compila_y_cierra_bloques(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        detalle = (ROOT / "templates" / "detalle_expediente_v4.html").read_text(encoding="utf-8")
        env = Environment(loader=DictLoader({"base.html": base, "detalle_expediente_v4.html": detalle}))
        env.get_template("detalle_expediente_v4.html")

    def test_navegacion_sms_visible_antes_de_cerrar_sesion(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertLess(base.index('href="/sms"'), base.index('href="/logout"'))
        self.assertIn("overflow-y-auto", base.split("<nav", 1)[1].split(">", 1)[0])
        self.assertIn(">Cobro<", base)
        self.assertLess(base.index('href="/crm"'), base.index('href="/acuerdos"'))
        self.assertLess(base.index('href="/acuerdos"'), base.index('href="/vencimientos"'))
        self.assertLess(base.index('href="/vencimientos"'), base.index('href="/supervision-agente"'))
        self.assertLess(base.index('href="/supervision-agente"'), base.index('href="/sms"'))
        self.assertLess(base.index('href="/sms"'), base.index('href="/cartas-cobro"'))
        self.assertLess(base.index('id="nav-cobro"'), base.index('href="/informes"'))
        self.assertGreater(base.index('href="/cartas-cobro"'), base.index('id="nav-cobro"'))
        self.assertLess(base.index('href="/cartas-cobro"'), base.index('href="/informes"'))

    def test_sms_nav_incluye_dropdown_cobro(self):
        sms = (ROOT / "templates" / "sms_campanas.html").read_text(encoding="utf-8")
        self.assertIn(">Cobro<", sms)
        self.assertIn('href="/crm"', sms)
        self.assertIn('href="/cartas-cobro"', sms)
        self.assertIn('href="/sms"', sms)
    def test_detalle_expediente_no_referencia_campos_legacy(self):
        detalle = (ROOT / "templates" / "detalle_expediente_v4.html").read_text(encoding="utf-8").lower()
        for token in ("id_cliente", "id_demandado", "proceso.demandante", "proceso.demandado"):
            self.assertNotIn(token, detalle)

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




    def test_radicacion_ejecutivo_crea_y_vincula_obligacion(self):
        source = (ROOT / "radicacion_service.py").read_text(encoding="utf-8")
        self.assertIn('if naturaleza == "EJECUTIVO":', source)
        self.assertIn('obligacion_id = obligaciones_service.crear_obligacion(', source)
        self.assertIn('obligaciones_service.vincular_partes_obligacion(', source)
        self.assertIn('obligaciones_service.vincular_obligacion_a_proceso(', source)

    def test_preflight_detecta_no_verbal_sin_obligacion(self):
        source = (ROOT / "schema_preflight.py").read_text(encoding="utf-8")
        self.assertIn("procesos no-VERBAL sin obligación canónica", source)
        self.assertIn("20260919_fase15_completar_obligaciones_ejecutivos_historicos", source)

    def test_bot_liquidar_usa_realdictcursor(self):
        """Evita TypeError al indexar ob['fuente_saldo'] / ob['id'] en /api/bot/liquidar."""
        source = (ROOT / "bot_api.py").read_text(encoding="utf-8")
        self.assertIn("from psycopg2.extras import RealDictCursor", source)
        self.assertIn("async def liquidar_para_bot", source)
        # El bloque de lectura de obligación debe usar dict rows, no tuplas.
        liquidar = source.split("async def liquidar_para_bot", 1)[1].split(
            "async def ", 1
        )[0]
        self.assertIn("cursor_factory=RealDictCursor", liquidar)

    def test_honorarios_liquidador_usan_base_historica_no_reducida_por_abonos(self):
        source = (ROOT / "liquidador.py").read_text(encoding="utf-8")
        self.assertIn("capital_historico", source)
        self.assertIn("intereses_historicos", source)
        self.assertIn("base_honorarios = capital_historico + intereses_historicos", source)
        self.assertIn("capital_historico += ord_val + ext_val", source)
        self.assertIn("intereses_historicos += interes_mes", source)
        self.assertIn('"base_honorarios": base_honorarios', source)
        self.assertNotIn("total_honorarios = (total_capital + total_intereses)", source)

    def test_guardar_recalcular_liquidacion_persiste_por_clave_natural(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("def _guardar_cambios_liquidacion", source)
        self.assertIn("SET valor_capital = %s,", source)
        self.assertIn("obligation_id = %s", source)
        self.assertIn("WHERE inmueble_id = %s", source)
        self.assertIn("AND concepto = %s", source)
        self.assertIn("motor_calculo_judicial(", source)
        self.assertNotIn('RedirectResponse(url="/liquidador", status_code=307)', source)
        template = (ROOT / "templates" / "liquidador.html").read_text(encoding="utf-8")
        self.assertIn("{% if mensaje %}", template)
        self.assertIn("Liquidación actualizada correctamente.", source)

    def test_carga_masiva_liquidador_persiste_obligation_id(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn(
            "(inmueble_id, obligation_id, concepto, periodo_mes, periodo_anio,",
            source,
        )
        self.assertNotIn(
            "INSERT INTO expensas_ph\n                                                    (inmueble_id, concepto, periodo_mes",
            source,
        )

    def test_resolver_ph_repara_ejecutivo_historico_sin_obligacion(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("Reparación histórica:", source)
        self.assertIn("CUOTAS_ADMINISTRACION", source)
        self.assertIn("vincular_obligacion_a_proceso", source)

    def test_informe_ejecutivo_no_referencia_relaciones_legacy(self):
        source = (ROOT / "exportaciones.py").read_text(encoding="utf-8").lower()
        for token in ("procesos_litisconsorcio", "id_cliente", "id_demandado"):
            self.assertNotIn(token, source)

    def test_expedientes_exige_motivo_para_inactivar(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('"/expediente/estado"', source)
        self.assertIn('accion == "INACTIVAR" and len(motivo) < 5', source)
        template = (ROOT / "templates" / "detalle_expediente_v4.html").read_text(encoding="utf-8")
        self.assertIn('name="motivo"', template)
        self.assertIn('minlength="5"', template)

    def test_expedientes_filtro_estado(self):
        source = (ROOT / "expedientes_service.py").read_text(encoding="utf-8")
        self.assertIn('estado_filtro: str = "ACTIVOS"', source)
        template = (ROOT / "templates" / "expedientes.html").read_text(encoding="utf-8")
        for value in ('ACTIVOS', 'INACTIVOS', 'TODOS'):
            self.assertIn(value, template)

    def test_sms_bloquea_proceso_inactivo(self):
        sms = (ROOT / "sms_router.py").read_text(encoding="utf-8")
        saldo = (ROOT / "sms_saldo_service.py").read_text(encoding="utf-8")
        self.assertIn("p_inact.estado", sms)
        self.assertIn("p.estado", saldo)

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

    def test_totales_cartera_agrega_capital_intereses_honorarios(self):
        import obligacion_saldo_service as saldo_svc
        from unittest.mock import patch

        class MultiRowCursor:
            def __init__(self, rows):
                self._rows = rows

            def execute(self, sql, params=None):
                return None

            def fetchall(self):
                return self._rows

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def __init__(self, rows):
                self._rows = rows

            def cursor(self, cursor_factory=None):
                return MultiRowCursor(self._rows)

            def release(self):
                return None

        rows = [
            {
                "id": 1,
                "inmueble_id": 10,
                "fuente_saldo": "EXPENSAS_PH",
                "estado": "ACTIVA",
                "tipo_obligacion_codigo": "CUOTAS_ADMINISTRACION",
            },
            {
                "id": 2,
                "inmueble_id": 20,
                "fuente_saldo": "EXPENSAS_PH",
                "estado": "ACTIVA",
                "tipo_obligacion_codigo": "CUOTAS_ADMINISTRACION",
            },
        ]
        liquidaciones = [
            {
                "saldo_verificado": True,
                "saldo_total": 123800.0,
                "detalle_liquidacion": {
                    "capital": 100000.0,
                    "intereses": 10000.0,
                    "honorarios": 13800.0,
                    "gran_total": 123800.0,
                },
            },
            {
                "saldo_verificado": True,
                "saldo_total": 61900.0,
                "detalle_liquidacion": {
                    "capital": 50000.0,
                    "intereses": 5000.0,
                    "honorarios": 6900.0,
                    "gran_total": 61900.0,
                },
            },
        ]

        with patch.object(saldo_svc.db, "get_connection", return_value=FakeConn(rows)):
            with patch.object(saldo_svc, "_saldo_ph", side_effect=liquidaciones):
                totales = saldo_svc.calcular_totales_cartera(fecha_corte=date(2026, 9, 21))

        self.assertEqual(totales["capital"], 150000.0)
        self.assertEqual(totales["intereses"], 15000.0)
        self.assertEqual(totales["honorarios"], 20700.0)
        self.assertEqual(totales["valor_cartera"], 185700.0)
        self.assertEqual(totales["total_actualizado"], 185700.0)
        self.assertEqual(totales["obligaciones_incluidas"], 2)

    def test_soft_delete_acuerdo_oculta_crm_y_cuotas(self):
        import agenda_service
        from unittest.mock import MagicMock

        class RecordingCursor:
            def __init__(self):
                self.statements = []
                self._fetch = {
                    "id": 7,
                    "identificacion_deudor": "123",
                    "nombre_deudor": "Prueba",
                    "inmueble_id": 9,
                    "estado": "PENDIENTE",
                }

            def execute(self, sql, params=None):
                self.statements.append((" ".join(str(sql).split()), params))

            def fetchone(self):
                # Primer SELECT del acuerdo; luego SELECT nombre abogado puede devolver None
                if any("FROM acuerdos_pago WHERE id" in s[0] for s in self.statements[-1:]):
                    return self._fetch
                return None

        cur = RecordingCursor()
        request = MagicMock()
        request.state.user_id = None

        # Forzar tablas existentes
        original = agenda_service._table_exists
        agenda_service._table_exists = lambda _cur, name: name in {
            "acuerdos_pago_cuotas",
            "vencimientos",
            "gestiones_crm",
        }
        try:
            result = agenda_service._soft_delete_acuerdo(cur, request, 7)
        finally:
            agenda_service._table_exists = original

        self.assertEqual(result["id"], 7)
        joined = " | ".join(s[0] for s in cur.statements)
        self.assertIn("estado='ANULADO'", joined)
        self.assertIn("acuerdos_pago_cuotas", joined)
        self.assertIn("gestiones_crm", joined)
        self.assertTrue(
            any(
                (params or ()) and "ELIMINAR_ACUERDO" in params
                for _, params in cur.statements
            )
        )


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
