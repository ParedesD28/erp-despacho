"""
Suite exhaustiva — Método Bolsa Global / Art. 1653-1655 CC.

Compatible con `pytest` y con `unittest discover` (CI del ERP).
"""
from __future__ import annotations

import math
import unittest
from datetime import date, datetime

from bolsa_global_mora import (
    clasificar_concepto_bolsa,
    determinar_mora_y_capital_limpio,
    _a_float_seguro,
    _es_cuota_ordinaria,
    _extraer_mes_corte,
)


def _fila(fecha, concepto, valor=0, abono=0):
    return {
        "Fecha": fecha,
        "Concepto": concepto,
        "Valor": valor,
        "Abono": abono,
    }


class TestClasificacionTokens(unittest.TestCase):
    def test_pintura_es_capital_no_interes(self):
        """PINTURA no debe matchear INT como substring (bug v2 pegada)."""
        self.assertEqual(clasificar_concepto_bolsa("PINTURA FACHADA"), "capital")
        self.assertEqual(clasificar_concepto_bolsa("PINTURA"), "capital")

    def test_sancion_con_tilde_es_interes(self):
        self.assertEqual(clasificar_concepto_bolsa("SANCIÓN"), "interes")
        self.assertEqual(clasificar_concepto_bolsa("SANCION ASAMBLEA"), "interes")

    def test_memorando_no_es_mora(self):
        """MORA ⊂ MEMORANDO no debe disparar interés."""
        self.assertEqual(clasificar_concepto_bolsa("MEMORANDO"), "no_reconocido")

    def test_interes_con_tilde_y_token_int(self):
        self.assertEqual(clasificar_concepto_bolsa("INTERÉS DE MORA"), "interes")
        self.assertEqual(clasificar_concepto_bolsa("INT"), "interes")
        self.assertEqual(clasificar_concepto_bolsa("INT."), "interes")

    def test_administracion_y_cuota_son_capital(self):
        self.assertEqual(clasificar_concepto_bolsa("ADMINISTRACION"), "capital")
        self.assertEqual(clasificar_concepto_bolsa("CUOTA ADMON"), "capital")
        self.assertEqual(clasificar_concepto_bolsa("RETROACTIVO"), "capital")

    def test_honorarios_son_gasto(self):
        self.assertEqual(clasificar_concepto_bolsa("HONORARIOS ABOGADO"), "gasto")
        self.assertEqual(clasificar_concepto_bolsa("COBRO PREJURIDICO"), "gasto")


class TestParseoMontosYFechas(unittest.TestCase):
    def test_monto_latam_punto_miles_coma_decimal(self):
        self.assertEqual(_a_float_seguro("1.234,56"), 1234.56)
        self.assertEqual(_a_float_seguro("1.234.567,89"), 1234567.89)

    def test_nan_inf_devuelven_none(self):
        self.assertIsNone(_a_float_seguro(float("nan")))
        self.assertIsNone(_a_float_seguro(float("inf")))
        self.assertIsNone(_a_float_seguro("Infinity"))
        self.assertIsNone(_a_float_seguro("NaN"))

    def test_fechas_multi_formato(self):
        self.assertEqual(_extraer_mes_corte("2024.01.15"), "2024.01")
        self.assertEqual(_extraer_mes_corte("2024-01-15"), "2024.01")
        self.assertEqual(_extraer_mes_corte("15/01/2024"), "2024.01")
        self.assertEqual(_extraer_mes_corte("2024.1.15"), "2024.01")
        self.assertEqual(_extraer_mes_corte(date(2024, 3, 1)), "2024.03")
        self.assertEqual(_extraer_mes_corte(datetime(2024, 3, 1, 12, 0)), "2024.03")
        self.assertIsNone(_extraer_mes_corte("xxx"))
        self.assertIsNone(_extraer_mes_corte(""))


class TestEscenarioA_B_C(unittest.TestCase):
    def test_escenario_a_luego_b_remanente_a_capital_luego_c(self):
        """
        Caso base auditoría: bolsa 800 → A (ene) → B remanente (feb) → C (mar).
        mora=2024.02; capital = 380 + 500 = 880.
        """
        rows = [
            _fila("2024.01.10", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.01.10", "INTERES", 100, 0),
            _fila("2024.01.15", "ABONO", 0, 800),
            _fila("2024.02.10", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.02.10", "INTERES", 80, 0),
            _fila("2024.03.10", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.03.10", "INTERES", 50, 0),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.02")
        self.assertEqual(r["bolsa_global_inicial"], 800.0)
        self.assertEqual(r["bolsa_remanente_final"], 0.0)
        self.assertEqual(r["total_capital_demandado"], 880.0)
        valores = [i["valor_a_demandar"] for i in r["capital_limpio_a_demandar"]]
        self.assertIn(380.0, valores)
        self.assertIn(500.0, valores)
        # Intereses de mar descartados: solo 2 ítems de capital
        self.assertEqual(len(r["capital_limpio_a_demandar"]), 2)

    def test_b_remanente_prelacion_extra_antes_que_ordinaria(self):
        """
        Hallazgo IA: con FIFO por aparición, ordinaria primero queda en $0.
        Regla: sobrante B−I amortiza extras/gastos primero; ordinaria al final.
        I=80, C=500+200, bolsa=580 → sobrante=500 → extra 0; ordinaria 200.
        """
        rows = [
            _fila("2024.02.10", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.02.10", "CUOTA EXTRAORDINARIA", 200, 0),
            _fila("2024.02.10", "INTERES", 80, 0),
            _fila("2024.02.15", "ABONO", 0, 580),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.02")
        self.assertEqual(r["total_capital_demandado"], 200.0)
        por_concepto = {
            i["concepto"]: i["valor_a_demandar"] for i in r["capital_limpio_a_demandar"]
        }
        self.assertEqual(por_concepto.get("CUOTA ADMINISTRACION"), 200.0)
        self.assertNotIn("CUOTA EXTRAORDINARIA", por_concepto)

    def test_b_remanente_prelacion_independiente_del_orden_extracto(self):
        """Misma bolsa/causaciones: misma asignación aunque EXTRA vaya primero."""
        base_abono_interes = [
            _fila("2024.02.10", "INTERES", 80, 0),
            _fila("2024.02.15", "ABONO", 0, 280),  # sobrante 200
        ]
        ord_primero = [
            _fila("2024.02.10", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.02.10", "CUOTA EXTRAORDINARIA", 200, 0),
            *base_abono_interes,
        ]
        extra_primero = [
            _fila("2024.02.10", "CUOTA EXTRAORDINARIA", 200, 0),
            _fila("2024.02.10", "CUOTA ADMINISTRACION", 500, 0),
            *base_abono_interes,
        ]
        r1 = determinar_mora_y_capital_limpio(ord_primero)
        r2 = determinar_mora_y_capital_limpio(extra_primero)
        self.assertEqual(r1["total_capital_demandado"], 500.0)
        self.assertEqual(r2["total_capital_demandado"], 500.0)
        for r in (r1, r2):
            por = {i["concepto"]: i["valor_a_demandar"] for i in r["capital_limpio_a_demandar"]}
            self.assertEqual(por.get("CUOTA ADMINISTRACION"), 500.0)
            self.assertNotIn("CUOTA EXTRAORDINARIA", por)

    def test_b_remanente_gasto_antes_que_ordinaria(self):
        """Honorarios (gasto) se extinguen antes que la cuota ordinaria."""
        rows = [
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 300, 0),
            _fila("2024.01.01", "HONORARIOS ABOGADO", 100, 0),
            _fila("2024.01.01", "INTERES", 50, 0),
            _fila("2024.01.10", "ABONO", 0, 200),  # sobrante 150
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["total_capital_demandado"], 250.0)
        por = {i["concepto"]: i["valor_a_demandar"] for i in r["capital_limpio_a_demandar"]}
        self.assertEqual(por.get("CUOTA ADMINISTRACION"), 250.0)
        self.assertNotIn("HONORARIOS ABOGADO", por)

    def test_mes_limpio_no_toca_capital_ni_con_extra(self):
        """Mes Limpio: bolsa < I_m → ordinaria y extra intactas (sin prelación)."""
        rows = [
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.01.01", "CUOTA EXTRAORDINARIA", 200, 0),
            _fila("2024.01.01", "INTERES", 100, 0),
            _fila("2024.01.10", "ABONO", 0, 50),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["total_capital_demandado"], 700.0)
        por = {i["concepto"]: i["valor_a_demandar"] for i in r["capital_limpio_a_demandar"]}
        self.assertEqual(por.get("CUOTA ADMINISTRACION"), 500.0)
        self.assertEqual(por.get("CUOTA EXTRAORDINARIA"), 200.0)
        self.assertTrue(all("Mes Limpio" in i["nota"] for i in r["capital_limpio_a_demandar"]))

    def test_post_quiebre_capital_pleno_sin_prelacion_de_montos(self):
        """Tras quiebre, meses posteriores demandan capital pleno (ord + extra)."""
        rows = [
            _fila("2024.01.10", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.01.10", "INTERES", 100, 0),
            _fila("2024.01.15", "ABONO", 0, 50),  # Mes Limpio ene
            _fila("2024.02.10", "CUOTA ADMINISTRACION", 400, 0),
            _fila("2024.02.10", "CUOTA EXTRAORDINARIA", 150, 0),
            _fila("2024.02.10", "INTERES", 80, 0),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["total_capital_demandado"], 1050.0)  # 500+400+150
        feb = [
            i for i in r["capital_limpio_a_demandar"] if i["mes_corte"] == "2024.02"
        ]
        self.assertEqual(sum(i["valor_a_demandar"] for i in feb), 550.0)

    def test_es_cuota_ordinaria_heuristica(self):
        self.assertTrue(_es_cuota_ordinaria("CUOTA ADMINISTRACION"))
        self.assertTrue(_es_cuota_ordinaria("CUOTA ADMON"))
        self.assertTrue(_es_cuota_ordinaria("ADMINISTRACION"))
        self.assertTrue(_es_cuota_ordinaria("ADMINISTRACIÓN"))
        self.assertFalse(_es_cuota_ordinaria("CUOTA EXTRAORDINARIA"))
        self.assertFalse(_es_cuota_ordinaria("HONORARIOS"))
        self.assertFalse(_es_cuota_ordinaria("PINTURA FACHADA"))
        self.assertFalse(_es_cuota_ordinaria("GASTOS DE ADMINISTRACION"))

    def test_mes_limpio_perdon_intereses_capital_intacto(self):
        """Bolsa < intereses → Mes Limpio: capital 100% intacto; mora ese mes."""
        rows = [
            _fila("2024.01.01", "INTERES", 100, 0),
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.01.15", "ABONO", 0, 50),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["total_capital_demandado"], 500.0)
        self.assertEqual(len(r["capital_limpio_a_demandar"]), 1)
        item = r["capital_limpio_a_demandar"][0]
        self.assertEqual(item["valor_a_demandar"], 500.0)
        self.assertIn("Mes Limpio", item["nota"])

    def test_mes_limpio_con_pintura_capital_no_evaporado(self):
        """Trampa PINTURA: con matching seguro queda capital intacto en Mes Limpio."""
        rows = [
            _fila("2024.05.01", "INTERES", 100, 0),
            _fila("2024.05.01", "PINTURA", 500, 0),
            _fila("2024.05.10", "ABONO", 0, 40),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.05")
        self.assertEqual(r["total_capital_demandado"], 500.0)
        self.assertEqual(r["capital_limpio_a_demandar"][0]["clase_concepto"], "capital")

    def test_escenario_c_capital_futuro_sin_abonos(self):
        """Bolsa=0 → primer mes con capital fija mora; intereses descartados."""
        rows = [
            _fila("2024.01.01", "INTERES", 80, 0),
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 400, 0),
            _fila("2024.02.01", "INTERES", 90, 0),
            _fila("2024.02.01", "CUOTA ADMINISTRACION", 400, 0),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["bolsa_global_inicial"], 0.0)
        self.assertEqual(r["total_capital_demandado"], 800.0)
        self.assertEqual(len(r["capital_limpio_a_demandar"]), 2)

    def test_b_extingue_capital_no_marca_mora_ese_mes(self):
        """
        Bolsa cubre intereses + todo el capital del mes → no marcar mora ahí;
        el siguiente mes con capital (C) fija la fecha.
        """
        rows = [
            _fila("2024.01.01", "INTERES", 100, 0),
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 50, 0),
            _fila("2024.01.15", "ABONO", 0, 150),
            _fila("2024.02.01", "CUOTA ADMINISTRACION", 300, 0),
            _fila("2024.02.01", "INTERES", 40, 0),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.02")
        self.assertEqual(r["total_capital_demandado"], 300.0)
        self.assertEqual(r["capital_limpio_a_demandar"][0]["mes_corte"], "2024.02")


class TestRobustezDatosSucios(unittest.TestCase):
    def test_rows_none_no_crash(self):
        r = determinar_mora_y_capital_limpio(None)
        self.assertEqual(r["fecha_inicio_mora"], "Sin deuda")
        self.assertEqual(r["total_capital_demandado"], 0.0)
        self.assertTrue(any("None" in e for e in r["errores_procesamiento"]))

    def test_filas_none_y_sucias_no_crash(self):
        rows = [
            None,
            "basura",
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 100, 0),
            {"Fecha": "2024.02.01", "Concepto": "INTERES", "Valor": float("inf"), "Abono": 0},
            {"Fecha": "2024.02.01", "Concepto": "CUOTA", "Valor": float("nan"), "Abono": 50},
        ]
        r = determinar_mora_y_capital_limpio(rows)
        # Bolsa global 50 reduce capital ene (B remanente) → demandar 50; inf/nan → 0 + error.
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["bolsa_global_inicial"], 50.0)
        self.assertEqual(r["total_capital_demandado"], 50.0)
        self.assertTrue(any("no-dict" in e for e in r["errores_procesamiento"]))
        self.assertTrue(
            any("Valor ilegible" in e for e in r["errores_procesamiento"])
        )

    def test_monto_latam_en_flujo_completo(self):
        rows = [
            _fila("2024.01.15", "INTERÉS MORA", "1.234,56", 0),
            _fila("2024.02.01", "CUOTA ADMINISTRACION", 500, "200"),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        # Interés ene (1234.56) no cubierto por bolsa 200 → Mes Limpio ene;
        # pero ene solo tiene interés (sin capital) → capital_limpio vacío ese mes;
        # feb con bolsa=0 → C capital 500.
        # Espera: bolsa 200 < intereses ene → Mes Limpio, mora=2024.01, capital ene vacío,
        # luego feb en_mora → capital 500.
        self.assertEqual(r["bolsa_global_inicial"], 200.0)
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["total_capital_demandado"], 500.0)

    def test_fecha_rara_capital_no_evapora(self):
        """Fecha ilegible: capital anexado con REQUIERE REVISION, no perdido."""
        rows = [
            _fila("xxx", "PINTURA", 80, 0),
            _fila("2024.03.01", "CUOTA ADMINISTRACION", 200, 0),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["fecha_inicio_mora"], "2024.03")
        self.assertEqual(r["total_capital_demandado"], 280.0)
        notas = [i["nota"] for i in r["capital_limpio_a_demandar"]]
        self.assertTrue(any("REQUIERE REVISION" in n for n in notas))
        mes_cortes = {i["mes_corte"] for i in r["capital_limpio_a_demandar"]}
        self.assertIn("FECHA_INVALIDA", mes_cortes)

    def test_concepto_desconocido_alerta_y_default_capital(self):
        rows = [
            _fila("2024.01.01", "XYZ DESCONOCIDO RARO", 150, 0),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["total_capital_demandado"], 150.0)
        self.assertEqual(r["capital_limpio_a_demandar"][0]["clase_concepto"], "no_reconocido")
        self.assertTrue(any("no reconocido" in e.lower() for e in r["errores_procesamiento"]))

    def test_sancion_es_interes_en_flujo(self):
        rows = [
            _fila("2024.01.01", "SANCIÓN", 100, 0),
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 500, 0),
            _fila("2024.01.10", "ABONO", 0, 50),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        # bolsa 50 < intereses 100 → Mes Limpio; capital 500 intacto
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["total_capital_demandado"], 500.0)

    def test_memorando_no_consume_bolsa_como_interes(self):
        rows = [
            _fila("2024.01.01", "MEMORANDO", 100, 0),
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 200, 0),
            _fila("2024.01.10", "ABONO", 0, 50),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        # MEMORANDO → no_reconocido → capital; total capital mes = 300;
        # bolsa 50 < 300 → B remanente: no hay intereses → sobrante 50 reduce capital
        self.assertEqual(r["bolsa_global_inicial"], 50.0)
        self.assertEqual(r["fecha_inicio_mora"], "2024.01")
        self.assertEqual(r["total_capital_demandado"], 250.0)
        self.assertTrue(any("no reconocido" in e.lower() for e in r["errores_procesamiento"]))

    def test_mes_vacio_sin_quiebre_falso(self):
        """
        Mes con solo ceros / sin causaciones no debe marcar mora si bolsa > 0.
        Simulamos: abono crea bolsa; un mes 'fantasma' no se crea (solo Valor>0 agrupa).
        Con mes que tiene Valor=0 no se agrupa → no quiebre.
        """
        rows = [
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 0, 100),  # solo abono
            _fila("2024.02.01", "CUOTA ADMINISTRACION", 80, 0),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        # bolsa 100 >= 80 → A extingue feb; Sin deuda
        self.assertEqual(r["fecha_inicio_mora"], "Sin deuda")
        self.assertEqual(r["total_capital_demandado"], 0.0)
        self.assertEqual(r["bolsa_remanente_final"], 20.0)

    def test_gastos_honorarios_van_a_capital_con_alerta(self):
        rows = [
            _fila("2024.01.01", "HONORARIOS", 150, 0),
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 100, 0),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["total_capital_demandado"], 250.0)
        clases = {i["clase_concepto"] for i in r["capital_limpio_a_demandar"]}
        self.assertIn("gasto", clases)
        self.assertTrue(any("gasto" in e.lower() or "honorarios" in e.lower()
                            for e in r["errores_procesamiento"]))

    def test_abono_negativo_no_entra_bolsa(self):
        rows = [
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 100, -50),
        ]
        r = determinar_mora_y_capital_limpio(rows)
        self.assertEqual(r["bolsa_global_inicial"], 0.0)
        self.assertEqual(r["total_capital_demandado"], 100.0)
        self.assertTrue(any("negativo" in e.lower() for e in r["errores_procesamiento"]))


class TestContratoSalida(unittest.TestCase):
    def test_claves_contrato_presentes(self):
        r = determinar_mora_y_capital_limpio([])
        for clave in (
            "fecha_inicio_mora",
            "capital_limpio_a_demandar",
            "total_capital_demandado",
            "bolsa_global_inicial",
            "bolsa_remanente_final",
            "errores_procesamiento",
        ):
            self.assertIn(clave, r)

    def test_sin_deuda_lista_vacia(self):
        r = determinar_mora_y_capital_limpio([])
        self.assertEqual(r["fecha_inicio_mora"], "Sin deuda")
        self.assertEqual(r["capital_limpio_a_demandar"], [])


if __name__ == "__main__":
    unittest.main()
