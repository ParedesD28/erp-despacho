"""Cargador de facturación mensual: barreras, solo VALOR MES, visor y autocausación apagada."""
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
    clasificar_cargo_mes_recibo,
    cargos_desde_factura,
    cruzar_conjunto_estricto,
    cruzar_lote,
    cruzar_unidad_en_conjunto,
    detectar_abono_ultimo_mes,
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

    def test_abonos_y_anticipos_se_ignoran(self):
        for nombre in (
            "ABONO ULTIMO MES",
            "ANTICIPOS",
            "MENOS ANTICIPOS",
            "ADELANTO",
        ):
            r = clasificar_cargo_mes_recibo(nombre)
            self.assertIsNone(r["concepto_erp"], nombre)
            self.assertEqual(r["accion"], "IGNORAR_SIEMPRE", nombre)

    def test_cargos_solo_valor_mes_causable(self):
        inv = {
            "conceptos": [
                {"concepto": "APORTE CUOTA DE ADMINISTRACION", "valor_mes": 35000, "intereses_mes": 1200},
                {"concepto": "INTERESES DE MORA", "valor_mes": 1200, "intereses_mes": 0},
                {"concepto": "ABONO ULTIMO MES", "valor_mes": 250000},
                {"concepto": "SANCION REGLAMENTO", "valor_mes": 50000},
                {"concepto": "CUOTA EXTRAORDINARIA SEGURO", "valor_mes": 0},
            ]
        }
        cargos = cargos_desde_factura(inv, [], 2026, 10)
        conceptos = [c["concepto_erp"] for c in cargos]
        self.assertEqual(conceptos, ["Expensa Ordinaria", "Cuota Extraordinaria"])
        self.assertEqual(cargos[0]["valor_mes"], 35000)
        self.assertEqual(cargos[1]["valor_mes"], 50000)
        self.assertTrue(cargos[1]["requiere_confirmacion"])
        self.assertNotIn("Abono", conceptos)


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

    def test_mes_anterior_octubre_es_septiembre(self):
        self.assertEqual(mes_anterior(2026, 10), (2026, 9))
        self.assertEqual(mes_anterior(2026, 1), (2025, 12))


class AbonoOpcionalTests(unittest.TestCase):
    """Lee Abono último Mes, lo muestra desmarcado y solo inserta M-1 si se marca."""

    def test_23_401_propone_mes_anterior_desmarcado(self):
        r = detectar_abono_ultimo_mes(
            {"abono_ultimo_mes": 250000},
            [],
            2026,
            10,
        )
        self.assertEqual(r["accion"], "PROPONER_ABONO_MES_ANTERIOR")
        self.assertEqual(r["monto"], 250000)
        self.assertEqual(r["periodo"], "2026-09")
        self.assertEqual(r["periodo_anio"], 2026)
        self.assertEqual(r["periodo_mes"], 9)
        self.assertTrue(r["requiere_confirmacion"])
        self.assertFalse(r["marcado_por_defecto"])

    def test_no_duplica_mismo_monto(self):
        r = detectar_abono_ultimo_mes(
            {"abono_ultimo_mes": 250000},
            [{"concepto": "Abono", "periodo_anio": 2026, "periodo_mes": 9, "valor_capital": 250000}],
            2026,
            10,
        )
        self.assertEqual(r["accion"], "NO_OP_YA_EXISTE")
        self.assertFalse(r["requiere_confirmacion"])

    def test_no_pisa_otro_abono_del_periodo(self):
        r = detectar_abono_ultimo_mes(
            {"abono_ultimo_mes": 250000},
            [{"concepto": "Abono", "periodo_anio": 2026, "periodo_mes": 9, "valor_capital": 100000}],
            2026,
            10,
        )
        self.assertEqual(r["accion"], "NO_PISAR")

    def test_25_103_no_cierra_desfase(self):
        r = detectar_abono_ultimo_mes(
            {"abono_ultimo_mes": 70800},
            [],
            2026,
            10,
        )
        self.assertEqual(r["accion"], "PROPONER_ABONO_MES_ANTERIOR")
        self.assertEqual(r["monto"], 70800)
        self.assertNotEqual(r["monto"], 580237)
        self.assertFalse(r["marcado_por_defecto"])

    def test_sin_abono_ultimo_mes_no_propone(self):
        self.assertIsNone(detectar_abono_ultimo_mes({"abono_ultimo_mes": 0}, [], 2026, 10))

    def test_upsert_de_cuotas_sigue_sin_abono_ni_mora(self):
        from facturacion_mensual_service import _CONCEPTOS_CAUSABLES, _upsert_expensa

        self.assertEqual(_CONCEPTOS_CAUSABLES, frozenset({"Expensa Ordinaria", "Cuota Extraordinaria"}))
        self.assertNotIn("Abono", _CONCEPTOS_CAUSABLES)
        self.assertEqual(
            _upsert_expensa(None, inmueble_id=1, obligation_id=1, concepto="Abono", anio=2026, mes=9, valor=250000),
            "OMITIDO_NO_CAUSABLE",
        )
        self.assertEqual(
            _upsert_expensa(
                None, inmueble_id=1, obligation_id=1, concepto="INTERESES DE MORA", anio=2026, mes=10, valor=1200
            ),
            "OMITIDO_NO_CAUSABLE",
        )

    def test_aplicar_solo_inserta_abono_marcado(self):
        src = (ROOT / "facturacion_mensual_service.py").read_text(encoding="utf-8")
        self.assertNotIn("INSERTAR_ABONO_CIERRE", src)
        self.assertNotIn("580.237", src)
        self.assertNotIn("def capital_neto_antes", src)
        cuerpo = src.split("def aplicar_lote", 1)[1]
        self.assertIn("confirmar_abonos", cuerpo)
        self.assertIn("OMITIDO_SIN_CONFIRMACION", cuerpo)
        self.assertIn("_insertar_abono", cuerpo)

    def test_ui_checkbox_desmarcado_junto_a_cuotas(self):
        html = (ROOT / "templates" / "facturacion_mensual.html").read_text(encoding="utf-8")
        self.assertIn("abono-ok", html)
        self.assertIn("Último pago (PDF)", html)
        self.assertIn("confirmar_abonos", html)
        self.assertNotIn("tabla-abonos", html)
        self.assertNotRegex(html, r'class="abono-ok"[^>]*\schecked')
        self.assertIn("VALOR MES", html)


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
                "concepto": "INTERESES DE MORA",
                "saldo_ant": 0,
                "valor_mes": 1847,
                "intereses_mes": 0,
                "total_concepto": 1847,
            }, {
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
        self.assertEqual(by_id[13]["abono"]["accion"], "PROPONER_ABONO_MES_ANTERIOR")
        self.assertEqual(by_id[13]["abono"]["monto"], 250000)
        self.assertEqual(by_id[13]["abono"]["periodo"], "2026-09")
        self.assertFalse(by_id[13]["abono"]["marcado_por_defecto"])
        self.assertEqual(by_id[15]["abono"]["accion"], "PROPONER_ABONO_MES_ANTERIOR")
        self.assertEqual(by_id[15]["abono"]["monto"], 70800)
        self.assertNotEqual(by_id[15]["abono"]["monto"], 580237)
        self.assertFalse(by_id[15]["abono"]["marcado_por_defecto"])
        for u in plan["unidades"]:
            self.assertNotIn("Abono", {c["concepto_erp"] for c in u["cargos"]})
            self.assertNotIn("INTERESES DE MORA", {c.get("concepto_pdf") for c in u["cargos"]})
        ops13 = {c["operacion"] for c in by_id[13]["cargos"] if c["concepto_erp"] == "Expensa Ordinaria"}
        self.assertEqual(ops13, {"NO_OP"})
        self.assertNotEqual(by_id[15]["estado"], "BLOQUEADO")
        ops15 = {c["operacion"] for c in by_id[15]["cargos"] if c["concepto_erp"] == "Expensa Ordinaria"}
        self.assertEqual(ops15, {"INSERT"})
        self.assertEqual(
            [c["valor_mes"] for c in by_id[15]["cargos"] if c["concepto_erp"] == "Expensa Ordinaria"],
            [35000],
        )

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
            saldo_anterior_pie=1000,
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
        self.assertNotIn("anticipos_pie", a)
        self.assertEqual(a["page"], 7)
        self.assertEqual(a["half"], "top")
        cuota_a = next(c for c in a["conceptos"] if "ADMINISTRACION" in (c["concepto"] or ""))
        self.assertEqual(cuota_a["valor_mes"], 35000)
        b = by_ref["25103"]
        self.assertEqual(b["abono_ultimo_mes"], 70800)
        self.assertEqual(b["saldo_anterior_pie"], 0)
        self.assertEqual(b["page"], 22)
        self.assertEqual(b["half"], "top")
        cuota = next(c for c in b["conceptos"] if "ADMINISTRACION" in (c["concepto"] or ""))
        self.assertEqual(cuota["valor_mes"], 35000)
        self.assertGreater(cuota["saldo_ant"] + cuota["valor_mes"] + cuota["intereses_mes"] - 0.01, -1)
        cargos_a = cargos_desde_factura(a, [], 2026, 10)
        cargos_b = cargos_desde_factura(b, [], 2026, 10)
        for cargos in (cargos_a, cargos_b):
            self.assertTrue(all(c["concepto_erp"] in {"Expensa Ordinaria", "Cuota Extraordinaria"} for c in cargos))
            self.assertNotIn("Abono", {c["concepto_erp"] for c in cargos})
        self.assertIn("Expensa Ordinaria", {c["concepto_erp"] for c in cargos_a})
        self.assertEqual({c["concepto_erp"] for c in cargos_b}, {"Expensa Ordinaria"})
        self.assertTrue(all(c["valor_mes"] == 35000 for c in cargos_b if c["concepto_erp"] == "Expensa Ordinaria"))
        prop_a = detectar_abono_ultimo_mes(a, [], 2026, 10)
        prop_b = detectar_abono_ultimo_mes(b, [], 2026, 10)
        self.assertEqual(prop_a["monto"], 250000)
        self.assertEqual(prop_b["monto"], 70800)
        self.assertFalse(prop_a["marcado_por_defecto"])
        self.assertFalse(prop_b["marcado_por_defecto"])
        for inv in self.invoices:
            self.assertNotIn("anticipos_pie", inv)

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
        self.assertNotIn("_insertar_abono", cuerpo)


if __name__ == "__main__":
    unittest.main()
