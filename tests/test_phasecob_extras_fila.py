"""Regresión: ADMINISTRACIÓN→ordinaria; 2+ extras mismo mes sin sumar; override UI."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import certificados_deuda_service as svc
from bolsa_global_mora import _es_cuota_ordinaria
from estado_cuenta_etl import CLASIFICACION_CUOTA, clasificar_concepto


class AdministracionOrdinariaTests(unittest.TestCase):
    def test_administracion_sola_es_ordinaria_bolsa(self):
        self.assertTrue(_es_cuota_ordinaria("ADMINISTRACION"))
        self.assertTrue(_es_cuota_ordinaria("ADMINISTRACIÓN"))
        self.assertTrue(_es_cuota_ordinaria("ADMON"))
        self.assertTrue(_es_cuota_ordinaria("CUOTAS DE ADMINISTRACION"))

    def test_administracion_sola_es_ordinaria_word(self):
        self.assertTrue(svc.es_cuota_ordinaria("ADMINISTRACION"))
        self.assertTrue(svc.es_cuota_ordinaria("ADMINISTRACIÓN"))
        filas = svc.agrupar_capital_limpio(
            [
                {
                    "fecha": "2024.06.01",
                    "concepto": "ADMINISTRACION",
                    "valor_a_demandar": 250.0,
                }
            ]
        )
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].cuotas_ordinarias, 250.0)
        self.assertEqual(filas[0].cuotas_extraordinarias, 0.0)

    def test_extra_y_gastos_no_pasan_a_ordinaria(self):
        self.assertFalse(_es_cuota_ordinaria("CUOTA EXTRAORDINARIA"))
        self.assertFalse(svc.es_cuota_ordinaria("PINTURA FACHADA"))
        self.assertFalse(svc.es_cuota_ordinaria("GASTOS DE ADMINISTRACION"))
        self.assertFalse(svc.es_cuota_ordinaria("HONORARIOS ADMINISTRACION"))

    def test_etl_administracion_sola(self):
        self.assertEqual(clasificar_concepto("ADMINISTRACION"), CLASIFICACION_CUOTA)
        self.assertEqual(clasificar_concepto("ADMINISTRACIÓN"), CLASIFICACION_CUOTA)

    def test_override_manual_concepto_a_ordinaria(self):
        items = [
            {
                "fecha": "2024.03.01",
                "concepto": "PINTURA FACHADA",
                "valor_a_demandar": 90.0,
            }
        ]
        sin = svc.agrupar_capital_limpio(items)
        self.assertEqual(sin[0].cuotas_extraordinarias, 90.0)
        con = svc.agrupar_capital_limpio(
            items, conceptos_forzar_ordinaria=["PINTURA FACHADA"]
        )
        self.assertEqual(con[0].cuotas_ordinarias, 90.0)
        self.assertEqual(con[0].cuotas_extraordinarias, 0.0)


class ExtrasMismaFilaTests(unittest.TestCase):
    def test_dos_extras_mismo_mes_no_suman_segunda_en_fila_nueva(self):
        items = [
            {
                "fecha": "2024.02.05",
                "concepto": "CUOTA ADMINISTRACION",
                "valor_a_demandar": 100.0,
            },
            {
                "fecha": "2024.02.10",
                "concepto": "CUOTA EXTRAORDINARIA ASCENSOR",
                "valor_a_demandar": 50.0,
            },
            {
                "fecha": "2024.02.15",
                "concepto": "CUOTA EXTRAORDINARIA PINTURA",
                "valor_a_demandar": 30.0,
            },
        ]
        filas = svc.agrupar_capital_limpio(items)
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[0].mes, "FEBRERO")
        self.assertEqual(filas[0].cuotas_ordinarias, 100.0)
        self.assertEqual(filas[0].cuotas_extraordinarias, 50.0)
        self.assertFalse(filas[0].es_extra_adicional)
        self.assertEqual(filas[0].saldo, 150.0)

        self.assertEqual(filas[1].mes, "FEBRERO")
        self.assertEqual(filas[1].anio, 2024)
        self.assertEqual(filas[1].cuotas_ordinarias, 0.0)
        self.assertEqual(filas[1].cuotas_extraordinarias, 30.0)
        self.assertTrue(filas[1].es_extra_adicional)
        self.assertEqual(filas[1].indice_extra, 1)
        self.assertEqual(filas[1].saldo, 180.0)
        # No se sumó 50+30 en la primera fila.
        self.assertNotEqual(filas[0].cuotas_extraordinarias, 80.0)

    def test_tres_o_mas_extras_cada_una_en_fila(self):
        """3+: 1ª con ordinaria; 2ª y 3ª en filas nuevas (mismo mes/año)."""
        items = [
            {
                "fecha": "2024.04.01",
                "concepto": "ADMINISTRACION",
                "valor_a_demandar": 200.0,
            },
            {
                "fecha": "2024.04.02",
                "concepto": "EXTRA A",
                "valor_a_demandar": 10.0,
            },
            {
                "fecha": "2024.04.03",
                "concepto": "EXTRA B",
                "valor_a_demandar": 20.0,
            },
            {
                "fecha": "2024.04.04",
                "concepto": "EXTRA C",
                "valor_a_demandar": 40.0,
            },
        ]
        filas = svc.agrupar_capital_limpio(items)
        self.assertEqual(len(filas), 3)
        self.assertEqual(filas[0].cuotas_ordinarias, 200.0)
        self.assertEqual(filas[0].cuotas_extraordinarias, 10.0)
        self.assertEqual(filas[1].cuotas_extraordinarias, 20.0)
        self.assertEqual(filas[2].cuotas_extraordinarias, 40.0)
        self.assertEqual(filas[2].saldo, 270.0)
        self.assertTrue(all(f.mes == "ABRIL" and f.anio == 2024 for f in filas))

    def test_contexto_plantilla_incluye_cuota_extra_2_y_flag(self):
        filas = svc.agrupar_capital_limpio(
            [
                {
                    "fecha": "2024.01.01",
                    "concepto": "CUOTA ADMINISTRACION",
                    "valor_a_demandar": 10.0,
                },
                {
                    "fecha": "2024.01.02",
                    "concepto": "EXTRA 1",
                    "valor_a_demandar": 5.0,
                },
                {
                    "fecha": "2024.01.03",
                    "concepto": "EXTRA 2",
                    "valor_a_demandar": 7.0,
                },
            ]
        )
        ctx = svc.construir_contexto_plantilla(
            datos_neon={
                "titular_nombre": "TEST",
                "titular_cedula": "1",
                "copropiedad_nombre": "PH",
                "copropiedad_nit": "123",
                "torre_apto": "1-1",
            },
            filas=filas,
        )
        self.assertEqual(len(ctx["filas"]), 2)
        self.assertIn("cuota_extra_2", ctx["filas"][0])
        self.assertFalse(ctx["filas"][0]["es_extra_adicional"])
        self.assertTrue(ctx["filas"][1]["es_extra_adicional"])


class PhasecobRutaTests(unittest.TestCase):
    def test_phasecob_ruta_en_main_y_permisos(self):
        import permisos

        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('"/phasecob"', src)
        self.assertIn("modo_phasecob", src)
        self.assertIn("vista_phasecob", src)
        self.assertEqual(
            permisos.permiso_requerido_para_ruta("GET", "/phasecob"),
            permisos.NAV_INFORMES,
        )
        # No debe aparecer en el navbar (base.html).
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertNotIn("/phasecob", base)
        # Plantilla soporta modo secreto.
        tpl = (ROOT / "templates" / "estado_cuenta_pdf.html").read_text(encoding="utf-8")
        self.assertIn("MODO_PHASECOB", tpl)
        self.assertIn("edicion_por_indice", tpl)


if __name__ == "__main__":
    unittest.main()
