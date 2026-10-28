"""Cargador de facturación mensual: barreras, abonos, visor y autocausación apagada."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

import liquidador
import permisos
from facturacion_mensual_parser import (
    Y_MITAD,
    parse_facturacion_pdf,
    periodo_desde_facturas,
    recortar_media_pagina_png,
)
from facturacion_mensual_service import (
    capital_neto_antes,
    clasificar_cargo_mes_recibo,
    cruzar_conjunto_estricto,
    cruzar_lote,
    cruzar_unidad_en_conjunto,
    decidir_abono,
    evaluar_elegibilidad,
    extraer_etapa_o_manzana,
    mes_anterior,
    normalizar_nit_base,
)

PDF_OCT = Path(
    "/cursor/stores/bc-2b92a817-5490-4c88-a556-8ad39d11cb5d/internal/facturacion_octubre_2026_mz_2.pdf"
)

CALLERS_AUTOCAUSAR = (
    "main.py",
    "bot_api.py",
    "api_recaudos.py",
    "grok_api.py",
    "obligacion_saldo_service.py",
)


class AutocausacionGlobalTests(unittest.TestCase):
    def test_flag_y_default_apagados(self):
        import inspect
        self.assertIs(liquidador.AUTOCAUSACION_HABILITADA, False)
        sig = inspect.signature(liquidador.motor_calculo_judicial)
        self.assertIs(sig.parameters["autocausar"].default, False)

    def test_insert_autocausado_queda_tras_el_kill_switch(self):
        src = (ROOT / "liquidador.py").read_text(encoding="utf-8")
        self.assertIn("if autocausar and AUTOCAUSACION_HABILITADA:", src)
        self.assertIn("AUTOCAUSACION_HABILITADA = False", src)

    def test_ningun_caller_pide_autocausar_true(self):
        for name in CALLERS_AUTOCAUSAR:
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("autocausar=True", src, name)
            self.assertIn("autocausar=False", src, name)


class BarreraConjuntoTests(unittest.TestCase):
    def test_nit_base_sin_dv(self):
        self.assertEqual(normalizar_nit_base("901542561-4"), "901542561")
        self.assertEqual(normalizar_nit_base("901.542.561"), "901542561")

    def test_no_borra_etapa(self):
        mz, et = extraer_etapa_o_manzana("MIRADOR DE LLANO GRANDE ETAPA 2 P.H")
        self.assertEqual(et, "2")
        mz2, _ = extraer_etapa_o_manzana("URBANIZACION SANTA CLARA MANZANA 2 P.H.")
        self.assertEqual(mz2, "2")

    def test_nit_elige_manzana_2_no_manzana_5(self):
        conjuntos = [
            {"conjunto_id": 1, "conjunto_nombre": "URBANIZACIÓN SANTA CLARA MANZANA 5 PH", "pj_nit": "901458387"},
            {"conjunto_id": 2, "conjunto_nombre": "URBANIZACIÓN SANTA CLARA MANZANA 2 PH", "pj_nit": "901542561"},
        ]
        ok = cruzar_conjunto_estricto(
            "URBANIZACION SANTA CLARA MANZANA 2 P.H.", "901542561-4", conjuntos
        )
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["conjunto"]["conjunto_id"], 2)

        fail = cruzar_conjunto_estricto(
            "URBANIZACION SANTA CLARA MANZANA 2 P.H.", "901458387", conjuntos
        )
        self.assertFalse(fail["ok"])

    def test_etapa_1_no_cruza_con_etapa_2_aunque_el_nucleo_coincida(self):
        solo_etapa_1 = [
            {"conjunto_id": 35, "conjunto_nombre": "MIRADOR DE LLANO GRANDE ETAPA 1 P.H", "pj_nit": "900111111"},
        ]
        r = cruzar_conjunto_estricto(
            "MIRADOR DE LLANO GRANDE ETAPA 2 P.H", "900111111", solo_etapa_1
        )
        self.assertFalse(r["ok"])
        self.assertEqual(r["motivo"], "CONFLICTO_ETAPA_MANZANA_CON_MISMO_NIT")

        ambas = solo_etapa_1 + [
            {"conjunto_id": 36, "conjunto_nombre": "MIRADOR DE LLANO GRANDE ETAPA 2 P.H", "pj_nit": "900111111"},
        ]
        ok = cruzar_conjunto_estricto(
            "MIRADOR DE LLANO GRANDE ETAPA 2 P.H", "900111111", ambas
        )
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["conjunto"]["conjunto_id"], 36)


class BarreraUnidadTests(unittest.TestCase):
    def test_bloque_apto_canonico(self):
        inms = [{"inmueble_id": 12, "torre_apto": "23-201"}]
        m = cruzar_unidad_en_conjunto("Bloque 23   APTO 201", "23201", inms)
        self.assertIsNotNone(m)
        self.assertEqual(m["inmueble_id"], 12)

    def test_no_cruza_compuesta_contra_plana_por_ref_pago(self):
        inms = [{"inmueble_id": 99, "torre_apto": "201"}]
        self.assertIsNone(cruzar_unidad_en_conjunto("Bloque 2 APTO 01", "201", inms))

    def test_no_cruza_14_31_con_4_31(self):
        inms = [{"inmueble_id": 1, "torre_apto": "14-31"}]
        self.assertIsNone(cruzar_unidad_en_conjunto("Bloque 4 APTO 31", "431", inms))
        inms2 = [{"inmueble_id": 2, "torre_apto": "431"}]
        self.assertIsNone(cruzar_unidad_en_conjunto("Bloque 14 APTO 31", "1431", inms2))

    def test_ceros_izquierda(self):
        inms = [{"inmueble_id": 3, "torre_apto": "02-042"}]
        m = cruzar_unidad_en_conjunto("Bloque 2 APTO 42", "2042", inms)
        self.assertIsNotNone(m)

    def test_sin_fallback_por_nombre(self):
        inms = [{"inmueble_id": 17, "torre_apto": "34-103", "contacto_nombre": "BANCO DAVIVIENDA"}]
        self.assertIsNone(cruzar_unidad_en_conjunto("Bloque 99 APTO 999", "99999", inms))


class BarreraConceptosTests(unittest.TestCase):
    def test_solo_valor_mes_mapea_ordinario(self):
        r = clasificar_cargo_mes_recibo("APORTE CUOTA DE ADMINISTRACION")
        self.assertEqual(r["concepto_erp"], "Expensa Ordinaria")
        self.assertFalse(r["requiere_confirmacion"])

    def test_intereses_se_ignoran(self):
        r = clasificar_cargo_mes_recibo("INTERESES DE MORA")
        self.assertIsNone(r["concepto_erp"])
        self.assertEqual(r["accion"], "IGNORAR_SIEMPRE")

    def test_sancion_pide_check(self):
        r = clasificar_cargo_mes_recibo("SANCION REGLAMENTO")
        self.assertEqual(r["concepto_erp"], "Cuota Extraordinaria")
        self.assertTrue(r["requiere_confirmacion"])

    def test_honorarios_ignorados(self):
        r = clasificar_cargo_mes_recibo("HONORARIOS ABOGADO / COBRO JURIDICO")
        self.assertIsNone(r["concepto_erp"])


class ElegibilidadTests(unittest.TestCase):
    def test_proceso_inactivo_bloquea(self):
        b = evaluar_elegibilidad(
            proceso_estado="Inactivo",
            obligacion_estado="ACTIVA",
            expensas=[{"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": 9, "valor_capital": 35000}],
            periodo_anio=2026,
            periodo_mes=10,
        )
        self.assertTrue(any("Activo" in x or "Inactivo" in x for x in b))

    def test_sin_historial_bloquea(self):
        b = evaluar_elegibilidad(
            proceso_estado="Activo",
            obligacion_estado="ACTIVA",
            expensas=[],
            periodo_anio=2026,
            periodo_mes=10,
        )
        self.assertTrue(any("historial" in x.lower() or "Certificado" in x for x in b))

    def test_hueco_cronologico_bloquea(self):
        b = evaluar_elegibilidad(
            proceso_estado="Activo",
            obligacion_estado="ACTIVA",
            expensas=[{"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": 7, "valor_capital": 35000}],
            periodo_anio=2026,
            periodo_mes=10,
        )
        self.assertTrue(any("anti-hueco" in x.lower() or "2026-09" in x for x in b))

    def test_sep_o_oct_permiten_octubre(self):
        for mes in (9, 10):
            b = evaluar_elegibilidad(
                proceso_estado="Activo",
                obligacion_estado="ACTIVA",
                expensas=[{"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": mes, "valor_capital": 35000}],
                periodo_anio=2026,
                periodo_mes=10,
            )
            self.assertEqual(b, [], mes)


class AbonoReglaTests(unittest.TestCase):
    """Regla 3: sin bandeja. 23-401 inserta $250.000 en sep; 25-103 cierra desfase."""

    def test_mes_anterior_octubre_es_septiembre(self):
        self.assertEqual(mes_anterior(2026, 10), (2026, 9))
        self.assertEqual(mes_anterior(2026, 1), (2025, 12))

    def test_23_401_abono_mes_anterior(self):
        # 14 ord * 35000 + 1 ext 35000 = 525000 antes de octubre; PDF saldo 382338; abono 250000
        r = decidir_abono(
            abono_ultimo_mes=250000,
            saldo_anterior_pdf=382338,
            capital_neon=525000,
            expensas=[],
            periodo_anio=2026,
            periodo_mes=10,
        )
        self.assertEqual(r["accion"], "INSERTAR_ABONO_MES_ANTERIOR")
        self.assertEqual(r["monto"], 250000)
        self.assertEqual(r["periodo"], "2026-09")
        self.assertFalse(r["alerta_fuerte"])

    def test_23_401_no_duplica_mismo_monto(self):
        r = decidir_abono(
            abono_ultimo_mes=250000,
            saldo_anterior_pdf=382338,
            capital_neon=525000,
            expensas=[{
                "concepto": "Abono",
                "periodo_anio": 2026,
                "periodo_mes": 9,
                "valor_capital": 250000,
            }],
            periodo_anio=2026,
            periodo_mes=10,
        )
        self.assertEqual(r["accion"], "NO_OP_YA_EXISTE")

    def test_25_103_cierra_desfase_no_solo_ultimo_abono(self):
        capital = 471400 + 108837  # 580237
        r = decidir_abono(
            abono_ultimo_mes=70800,
            saldo_anterior_pdf=0,
            capital_neon=capital,
            expensas=[],
            periodo_anio=2026,
            periodo_mes=10,
        )
        self.assertEqual(r["accion"], "INSERTAR_ABONO_CIERRE")
        self.assertEqual(r["monto"], capital)
        self.assertEqual(r["periodo"], "2026-09")
        self.assertTrue(r["alerta_fuerte"])
        self.assertNotEqual(r["monto"], 70800)
        self.assertIn("70", r["motivo"] or "")

    def test_capital_neto_antes_excluye_mes_factura(self):
        exp = [
            {"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": 9, "valor_capital": 35000},
            {"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": 10, "valor_capital": 35000},
            {"concepto": "Abono", "periodo_anio": 2026, "periodo_mes": 8, "valor_capital": 10000},
        ]
        self.assertEqual(capital_neto_antes(exp, 2026, 10), 25000.0)


class CruceLoteSinteticoTests(unittest.TestCase):
    def _inv(self, **over):
        base = {
            "page": 7,
            "half": "top",
            "factura_no": "7126",
            "unidad_dir": "Bloque 23   APTO 401",
            "ref_pago": "23401",
            "propietario_nombre": "TELLEZ ARIAS CHRIATIAN DAVID",
            "propietario_identificacion": "1089386615",
            "saldo_anterior_pie": 382338,
            "abono_ultimo_mes": 250000,
            "conceptos": [{
                "concepto": "APORTE CUOTA DE ADMINISTRACION",
                "saldo_ant": 20000,
                "valor_mes": 35000,
                "intereses_mes": 0,
                "total_concepto": 55000,
            }],
        }
        base.update(over)
        return base

    def test_23_401_y_25_103_en_el_plan(self):
        conjunto = {"conjunto_id": 2, "conjunto_nombre": "URBANIZACIÓN SANTA CLARA MANZANA 2 PH", "pj_nit": "901542561"}
        u13 = {
            "inmueble_id": 13,
            "torre_apto": "23-401",
            "contacto_nombre": "CHRISTIAN DAVID TELLEZ ARIAS",
            "contacto_cedula": "1089386615",
            "propietarios": [{"identificacion": "1089386615"}],
            "obligaciones": [{"obligacion_id": 9, "obligacion_estado": "ACTIVA", "proceso_estado": "Activo", "radicado_interno": "EXP-0009"}],
            "expensas": [
                {"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": 9, "valor_capital": 35000},
                {"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": 10, "valor_capital": 35000},
            ],
        }
        u15 = {
            "inmueble_id": 15,
            "torre_apto": "25-103",
            "contacto_nombre": "PAOLA ANDREA MOLINA MONTES",
            "contacto_cedula": "1088316771",
            "propietarios": [{"identificacion": "1088316771"}],
            "obligaciones": [{"obligacion_id": 11, "obligacion_estado": "ACTIVA", "proceso_estado": "Activo", "radicado_interno": "EXP-0011"}],
            "expensas": (
                [{"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": m, "valor_capital": 35000} for m in range(8, 10)]
                + [{"concepto": "Cuota Extraordinaria", "periodo_anio": 2026, "periodo_mes": 4, "valor_capital": 108837}]
            ),
        }
        # capital 25-103: 2*35000 + 108837 = 178837 (sintético corto); forzar desfase con saldo 0
        invoices = [
            self._inv(),
            self._inv(
                page=22, half="top", factura_no="7156",
                unidad_dir="Bloque 25   APTO 103", ref_pago="25103",
                propietario_nombre="MOLINA MONTES PAOLA ANDREA",
                propietario_identificacion="1088316771",
                saldo_anterior_pie=0, abono_ultimo_mes=70800,
            ),
        ]
        plan = cruzar_lote(invoices, conjunto, [u13, u15], periodo_anio=2026, periodo_mes=10)
        self.assertEqual(plan["resumen"]["nuestras"], 2)
        by_id = {u["inmueble_id"]: u for u in plan["unidades"]}
        self.assertEqual(by_id[13]["abono"]["accion"], "INSERTAR_ABONO_MES_ANTERIOR")
        self.assertEqual(by_id[13]["abono"]["monto"], 250000)
        self.assertEqual(by_id[15]["abono"]["accion"], "INSERTAR_ABONO_CIERRE")
        self.assertGreater(by_id[15]["abono"]["monto"], 70800)
        self.assertTrue(by_id[15]["abono"]["alerta_fuerte"])
        # Cuota oct de 25-103 se propone INSERT; no se bloquea la unidad
        self.assertNotEqual(by_id[15]["estado"], "BLOQUEADO")
        ops15 = {c["operacion"] for c in by_id[15]["cargos"] if c["concepto_erp"] == "Expensa Ordinaria"}
        self.assertEqual(ops15, {"INSERT"})

    def test_titular_discrepante_no_se_sobrescribe(self):
        conjunto = {"conjunto_id": 2, "conjunto_nombre": "MZ 2", "pj_nit": "901542561"}
        u = {
            "inmueble_id": 17,
            "torre_apto": "34-103",
            "contacto_nombre": "BANCO DAVIVIENDA",
            "contacto_cedula": "8600343137",
            "propietarios": [{"identificacion": "8600343137"}],
            "obligaciones": [{"obligacion_id": 12, "obligacion_estado": "ACTIVA", "proceso_estado": "Activo"}],
            "expensas": [{"concepto": "Expensa Ordinaria", "periodo_anio": 2026, "periodo_mes": 10, "valor_capital": 35000}],
        }
        inv = self._inv(
            unidad_dir="Bloque 34   APTO 103", ref_pago="34103",
            propietario_nombre="RESTREPO WILFER ALBERTO",
            propietario_identificacion="1087994460",
            abono_ultimo_mes=0, saldo_anterior_pie=1000,
        )
        plan = cruzar_lote([inv], conjunto, [u], periodo_anio=2026, periodo_mes=10)
        u0 = plan["unidades"][0]
        self.assertFalse(u0["cedula_coincide"])
        self.assertTrue(any("DISCREPANCIA_TITULAR_PDF" in a for a in u0["alertas"]))
        self.assertEqual(u0["titular_neon"], "BANCO DAVIVIENDA")
        self.assertNotEqual(u0["estado"], "BLOQUEADO")


class PermisosCargadorTests(unittest.TestCase):
    def test_consulta_ve_y_no_escribe(self):
        consulta = permisos.permisos_de_perfil(permisos.PERFIL_CONSULTA)
        self.assertFalse(permisos.denegar_acceso(consulta, "GET", "/herramientas/facturacion-mensual"))
        self.assertTrue(permisos.denegar_acceso(consulta, "POST", "/api/facturacion-mensual/preview"))
        self.assertTrue(permisos.denegar_acceso(consulta, "POST", "/api/facturacion-mensual/aplicar"))

    def test_abogado_puede_confirmar(self):
        abogado = permisos.permisos_de_perfil(permisos.PERFIL_ABOGADO)
        self.assertFalse(permisos.denegar_acceso(abogado, "GET", "/herramientas/facturacion-mensual"))
        self.assertFalse(permisos.denegar_acceso(abogado, "POST", "/api/facturacion-mensual/preview"))
        self.assertFalse(permisos.denegar_acceso(abogado, "POST", "/api/facturacion-mensual/aplicar"))
        self.assertFalse(permisos.denegar_acceso(abogado, "GET", "/api/facturacion-mensual/factura/x/7/top"))


@unittest.skipUnless(PDF_OCT.exists(), "PDF de octubre 2026 no montado")
class ParserPdfRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf = PDF_OCT.read_bytes()
        cls.invoices = parse_facturacion_pdf(cls.pdf)

    def test_120_facturas_dos_por_pagina(self):
        self.assertEqual(len(self.invoices), 120)
        self.assertEqual(sum(1 for i in self.invoices if i["half"] == "top"), 60)
        self.assertEqual(sum(1 for i in self.invoices if i["half"] == "bottom"), 60)

    def test_periodo_octubre_2026(self):
        self.assertEqual(periodo_desde_facturas(self.invoices), (2026, 10))

    def test_23_401_y_25_103_en_el_pdf(self):
        by_ref = {i["ref_pago"]: i for i in self.invoices}
        a = by_ref["23401"]
        self.assertEqual(a["abono_ultimo_mes"], 250000)
        self.assertEqual(a["page"], 7)
        self.assertEqual(a["half"], "top")
        b = by_ref["25103"]
        self.assertEqual(b["abono_ultimo_mes"], 70800)
        self.assertEqual(b["saldo_anterior_pie"], 0)
        self.assertEqual(b["page"], 22)
        self.assertEqual(b["half"], "top")
        cuota = next(c for c in b["conceptos"] if "ADMINISTRACION" in (c["concepto"] or ""))
        self.assertEqual(cuota["valor_mes"], 35000)
        self.assertGreater(cuota["saldo_ant"] + cuota["valor_mes"] + cuota["intereses_mes"] - 0.01, -1)

    def test_visor_recorta_media_pagina_no_entera(self):
        png_top = recortar_media_pagina_png(self.pdf, 7, "top")
        png_bot = recortar_media_pagina_png(self.pdf, 7, "bottom")
        self.assertGreater(len(png_top), 1000)
        self.assertGreater(len(png_bot), 1000)
        self.assertNotEqual(png_top, png_bot)
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(png_top))
        # Mitad de Letter a scale=2: ancho ~1224, alto ~ (792-395)*2 ≈ 794
        self.assertGreater(im.width, 800)
        self.assertLess(im.height, im.width)  # media página, más ancha que alta
        self.assertEqual(Y_MITAD, 395.0)


class MigracionCuotasDistorsionadasTests(unittest.TestCase):
    def test_sql_idempotente_solo_valores_exactos(self):
        sql = (ROOT / "migrations" / "20261028_0237_corregir_cuotas_distorsionadas_mz2.sql").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(sql, r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;", sql)
        self.assertEqual(sql.upper().count("UPDATE EXPENSAS_PH"), 5)
        casos = [
            (10, 8, "45000"),
            (10, 9, "45000"),
            (14, 8, "35001"),
            (14, 9, "35002"),
            (19, 9, "35001"),
        ]
        for inm, mes, distorsionado in casos:
            self.assertIn(f"inmueble_id = {inm}", sql)
            self.assertIn(f"periodo_mes = {mes}", sql)
            self.assertIn(f"valor_capital = {distorsionado}", sql)
        self.assertIn("SET valor_capital = 35000.00", sql)
        self.assertNotIn("DELETE", sql.upper())
        self.assertIn("concepto = 'Expensa Ordinaria'", sql)

    def test_lotes_tabla_idempotente(self):
        sql = (ROOT / "migrations" / "20261028_0238_lotes_facturacion_ph.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS lotes_facturacion_ph", sql)
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS uq_lotes_facturacion_ph_hash_periodo", sql)


class PreviewNoEscribeTests(unittest.TestCase):
    def test_preview_pdf_no_hace_insert(self):
        from facturacion_mensual_service import preview_pdf

        src = Path(ROOT / "facturacion_mensual_service.py").read_text(encoding="utf-8")
        cuerpo = src.split("def preview_pdf", 1)[1].split("def _upsert_expensa", 1)[0]
        self.assertNotIn("INSERT INTO expensas_ph", cuerpo)
        self.assertNotIn("UPDATE expensas_ph", cuerpo)
        self.assertIn("cruzar_lote", cuerpo)
        self.assertTrue(callable(preview_pdf))


if __name__ == "__main__":
    unittest.main()
