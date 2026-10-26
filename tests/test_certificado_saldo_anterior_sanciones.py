"""
Regla 2026-10 (usuario): el saldo anterior forma parte de la deuda certificada y
las sanciones/multas se cobran como capital extraordinario (no son interés de mora).

Cubre: clasificación (E6), Bolsa, fila SALDO ANTERIOR del certificado, alertas y
extractor del saldo anterior (layout compacto y layout partido por pypdf).
"""
from __future__ import annotations

import unittest
from pathlib import Path

from bolsa_global_mora import (
    clasificar_concepto_bolsa,
    concepto_mora_ambiguo,
    determinar_mora_y_capital_limpio,
    _es_cuota_ordinaria as bolsa_es_ordinaria,
)
from certificados_deuda_flujo_service import (
    _advertencias_con_saldo_anterior,
    _meta_cuenta_bolsa,
)
from certificados_deuda_service import (
    _es_cuota_ordinaria as cert_es_ordinaria,
    agrupar_capital_limpio,
    construir_contexto_plantilla,
)
from estado_cuenta_pdf_service import analizar_estado_cuenta_pdf, extraer_saldo_anterior


def _fila(fecha, concepto, valor=0, abono=0):
    return {"Fecha": fecha, "Concepto": concepto, "Valor": valor, "Abono": abono}


def _certificar(rows, **kw):
    r = determinar_mora_y_capital_limpio(rows, **kw)
    filas = agrupar_capital_limpio(r["capital_limpio_a_demandar"])
    return r, filas


class ClasificacionSancionesYMoraTests(unittest.TestCase):
    def test_sanciones_y_multas_son_clase_propia(self):
        for c in (
            "SANCION",
            "SANCIONES",
            "SANCION MUDANZA",
            "MULTA",
            "MULTAS POR CONVIVENCIA",
            "PENALIDAD",
            "SANCION ADMINISTRACION",
            "SANCION POR MORA",
        ):
            self.assertEqual(clasificar_concepto_bolsa(c), "sancion", c)

    def test_interes_real_sigue_siendo_interes(self):
        for c in (
            "INTERES",
            "INTERESES",
            "INTERES MORATORIO",
            "INTERESES ADMINISTRACION",
            "INTERESES DE MORA",
            "ADMINISTRACION MORATORIA",
            "MORA",
            "INT",
            "INT MORA",
            "MORAS",
        ):
            self.assertEqual(clasificar_concepto_bolsa(c), "interes", c)

    def test_e6_cuota_con_palabra_mora_no_es_interes(self):
        self.assertEqual(clasificar_concepto_bolsa("CUOTA ADMINISTRACION MORA"), "capital")
        self.assertEqual(clasificar_concepto_bolsa("CUOTA DE ADMINISTRACION EN MORA"), "capital")
        self.assertEqual(clasificar_concepto_bolsa("ADMINISTRACION MORA"), "capital")
        self.assertTrue(concepto_mora_ambiguo("CUOTA ADMINISTRACION MORA"))
        self.assertFalse(concepto_mora_ambiguo("INTERES MORATORIO"))
        self.assertFalse(concepto_mora_ambiguo("CUOTA ADMINISTRACION"))

    def test_morador_o_memorando_no_son_mora(self):
        self.assertNotEqual(clasificar_concepto_bolsa("MORADORES"), "interes")
        self.assertNotEqual(clasificar_concepto_bolsa("MEMORANDO"), "interes")

    def test_sancion_nunca_es_cuota_ordinaria(self):
        for c in ("SANCION", "SANCION ADMINISTRACION", "CUOTA ADMINISTRACION SANCION", "MULTAS"):
            self.assertFalse(bolsa_es_ordinaria(c), c)
            self.assertFalse(cert_es_ordinaria(c), c)
        self.assertTrue(bolsa_es_ordinaria("CUOTA ADMINISTRACION MORA"))

    def test_cuota_admin_mora_se_certifica_como_ordinaria(self):
        r, filas = _certificar([_fila("2024.01.01", "CUOTA ADMINISTRACION MORA", 100)])
        self.assertEqual(r["total_capital_demandado"], 100.0)
        self.assertEqual(filas[0].cuotas_ordinarias, 100.0)
        self.assertTrue(any("MORA" in e and "cuota" in e for e in r["errores_procesamiento"]))


class SancionesCertificadasTests(unittest.TestCase):
    def test_sancion_sin_abonos_va_a_extraordinarias_con_alerta(self):
        rows = [
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 100_000),
            _fila("2024.01.01", "SANCION", 50_000),
            _fila("2024.01.01", "INTERES MORATORIO", 9_000),
        ]
        r, filas = _certificar(rows)
        self.assertEqual(r["total_capital_demandado"], 150_000.0)  # sin interés
        self.assertEqual(r["sanciones_certificadas"], 50_000.0)
        self.assertTrue(r["alerta_sanciones"])
        self.assertIn("NO", "".join(r["errores_procesamiento"]).upper())
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].cuotas_ordinarias, 100_000.0)
        self.assertEqual(filas[0].cuotas_extraordinarias, 50_000.0)

    def test_sancion_posterior_al_quiebre_ya_no_se_descarta(self):
        rows = [
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 100),
            _fila("2024.02.01", "CUOTA ADMINISTRACION", 100),
            _fila("2024.02.01", "SANCION", 44),
            _fila("2024.03.01", "CUOTA ADMINISTRACION", 100),
            _fila("2024.03.01", "MULTA", 40),
        ]
        r, _ = _certificar(rows)
        self.assertEqual(r["total_capital_demandado"], 384.0)
        self.assertEqual(r["sanciones_certificadas"], 84.0)

    def test_sancion_consume_abono_antes_que_la_cuota_ordinaria(self):
        rows = [
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 100),
            _fila("2024.01.01", "SANCION", 50),
            _fila("2024.01.20", "ABONO", 0, 50),
        ]
        r, _ = _certificar(rows)
        self.assertEqual(r["sanciones_certificadas"], 0.0)
        self.assertEqual(r["total_capital_demandado"], 100.0)
        self.assertFalse(r["alerta_sanciones"])

    def test_abono_total_cubre_sancion_y_cuota(self):
        rows = [
            _fila("2024.01.01", "CUOTA ADMINISTRACION", 100),
            _fila("2024.01.01", "SANCION", 50),
            _fila("2024.01.20", "ABONO", 0, 150),
        ]
        r, filas = _certificar(rows)
        self.assertEqual(r["total_capital_demandado"], 0.0)
        self.assertEqual(filas, [])

    def test_sancion_con_fecha_invalida_no_se_pierde(self):
        r, _ = _certificar([_fila("sin fecha", "SANCION", 70)])
        self.assertEqual(r["total_capital_demandado"], 70.0)
        self.assertEqual(r["sanciones_certificadas"], 70.0)


class SaldoAnteriorCertificadoTests(unittest.TestCase):
    def test_sancion_50000_mas_saldo_300000_sin_abonos_certifica_ambos(self):
        rows = [
            _fila("2024.03.01", "CUOTA ADMINISTRACION", 100_000),
            _fila("2024.03.01", "SANCION", 50_000),
        ]
        r, filas = _certificar(rows, saldo_anterior=300_000)
        self.assertEqual(r["total_capital_demandado"], 450_000.0)
        self.assertEqual([f.mes for f in filas], ["SALDO ANTERIOR", "MARZO"])
        sa, marzo = filas
        self.assertEqual(sa.anio, 2024)
        self.assertEqual(sa.cuotas_ordinarias, 0.0)
        self.assertEqual(sa.cuotas_extraordinarias, 300_000.0)
        self.assertEqual(sa.vencimiento, "29-feb-24")
        self.assertEqual(marzo.cuotas_ordinarias, 100_000.0)
        self.assertEqual(marzo.cuotas_extraordinarias, 50_000.0)
        self.assertEqual(filas[-1].saldo, 450_000.0)
        self.assertTrue(r["alerta_saldo_anterior"])
        self.assertTrue(r["alerta_sanciones"])

    def test_abonos_parciales_consumen_saldo_anterior_primero(self):
        rows = [
            _fila("2024.03.01", "CUOTA ADMINISTRACION", 100_000),
            _fila("2024.03.01", "SANCION", 50_000),
            _fila("2024.03.20", "ABONO", 0, 120_000),
        ]
        r, filas = _certificar(rows, saldo_anterior=300_000)
        sa = filas[0]
        self.assertEqual(sa.mes, "SALDO ANTERIOR")
        self.assertEqual(sa.cuotas_extraordinarias, 180_000.0)
        self.assertEqual(filas[-1].saldo, 330_000.0)
        self.assertEqual(r["total_capital_demandado"], 330_000.0)

    def test_abonos_que_cubren_el_saldo_anterior_siguen_con_intereses_primero(self):
        rows = [
            _fila("2024.03.01", "CUOTA ADMINISTRACION", 100_000),
            _fila("2024.03.01", "INTERES MORATORIO", 10_000),
            _fila("2024.03.20", "ABONO", 0, 350_000),
        ]
        r, filas = _certificar(rows, saldo_anterior=300_000)
        # 300.000 saldo + 10.000 interés + 40.000 de cuota → quedan 60.000
        self.assertEqual(r["total_capital_demandado"], 60_000.0)
        self.assertNotIn("SALDO ANTERIOR", [f.mes for f in filas])

    def test_saldo_a_favor_reduce_la_deuda_certificada(self):
        rows = [_fila("2024.03.01", "CUOTA ADMINISTRACION", 100_000)]
        r, filas = _certificar(rows, saldo_anterior=-30_000)
        self.assertEqual(r["total_capital_demandado"], 70_000.0)
        self.assertEqual(filas[-1].saldo, 70_000.0)
        self.assertIn("A FAVOR", r["alerta_saldo_anterior_mensaje"])

    def test_contexto_plantilla_muestra_fila_saldo_anterior(self):
        rows = [_fila("2024.03.01", "CUOTA ADMINISTRACION", 100_000)]
        _, filas = _certificar(rows, saldo_anterior=300_000)
        ctx = construir_contexto_plantilla(datos_neon={}, filas=filas)
        self.assertEqual(ctx["filas"][0]["mes"], "SALDO ANTERIOR")
        self.assertEqual(ctx["filas"][0]["cuotas_extraordinarias"], "$ 300.000")
        self.assertEqual(ctx["total_saldo"], "$ 400.000")

    def test_flujo_antepone_alertas_de_saldo_y_sanciones(self):
        rows = [
            _fila("2024.03.01", "CUOTA ADMINISTRACION", 100_000),
            _fila("2024.03.01", "SANCION", 50_000),
        ]
        r = determinar_mora_y_capital_limpio(rows, saldo_anterior=300_000)
        meta = _meta_cuenta_bolsa({**r, "archivo": "x.pdf"})
        adv = _advertencias_con_saldo_anterior(meta)
        self.assertIn("saldo anterior", adv[0].lower())
        self.assertIn("sanciones", adv[1].lower())
        self.assertTrue(meta["alerta_saldo_anterior"] and meta["alerta_sanciones"])

    def test_cuotas_del_certificado_no_se_inventan(self):
        """El saldo anterior es UNA fila; no hay cuotas mensuales sintéticas."""
        rows = [_fila("2024.03.01", "CUOTA ADMINISTRACION", 100)]
        r, filas = _certificar(rows, saldo_anterior=5_000)
        self.assertEqual(len(r["capital_limpio_a_demandar"]), 2)
        self.assertEqual(len(filas), 2)


class ExtractorSaldoAnteriorTests(unittest.TestCase):
    def test_layout_compacto_misma_linea(self):
        lineas = ["Saldo anterior     .  .              0              0        997,872"]
        self.assertEqual(extraer_saldo_anterior(lineas), 997872.0)

    def test_layout_partido_columnas_en_lineas_siguientes(self):
        lineas = [
            "Señor (a):",
            "Identificacion",
            "Saldo anterior",
            "0",
            "0",
            "250,000",
            "0",
            "0",
            "CUOTA DE ADMINISTRACION",
        ]
        self.assertEqual(extraer_saldo_anterior(lineas), 250000.0)

    def test_layout_partido_cero(self):
        lineas = ["Saldo anterior", "0", "0", "0", "0", "0"]
        self.assertEqual(extraer_saldo_anterior(lineas), 0.0)

    def test_pie_saldo_ant_monto_en_linea_previa(self):
        lineas = [
            "5,471,741",
            "Sub_total",
            "250,000",
            "Saldo Ant.",
            "5,721,741",
            "Totales",
            "7,011,443",
            "1,539,702",
        ]
        self.assertEqual(extraer_saldo_anterior(lineas), 250000.0)

    def test_pie_saldo_ant_pegado_a_la_etiqueta(self):
        lineas = ["4,616,783Sub_total", "997,872Saldo Ant.", "5,614,655Totales"]
        self.assertEqual(extraer_saldo_anterior(lineas), 997872.0)

    def test_portada_saldo_anterior_saldo_total_no_es_un_saldo(self):
        """La portada lista saldos por concepto bajo 'Saldo anterior / Saldo Total'."""
        lineas = ["Saldo anterior", "Saldo Total", "3,452,000", "5,471,741", "294,451"]
        self.assertIsNone(extraer_saldo_anterior(lineas))

    def test_sin_datos_devuelve_none(self):
        self.assertIsNone(extraer_saldo_anterior(["Periodo 1", "Total 100"]))
        self.assertIsNone(extraer_saldo_anterior(None))
        self.assertIsNone(extraer_saldo_anterior(["Saldo anterior", "texto"]))


_STORE = Path("/cursor/stores/bc-2b92a817-5490-4c88-a556-8ad39d11cb5d")
_PDFS_REALES = {
    "124": _STORE / "docs/estados-cuenta-1-24-1-31/124.pdf",
    "131": _STORE / "docs/estados-cuenta-1-24-1-31/131.pdf",
    "931": _STORE / "docs/estados-cuenta-muestra-demanda/931.pdf",
    "951": _STORE / "docs/estados-cuenta-muestra-demanda/951.pdf",
    "924": _STORE / "docs/estados-cuenta-muestra-demanda/924.pdf",
    "1031": _STORE / "docs/estados-cuenta-muestra-demanda/1031.pdf",
    "1032": _STORE / "docs/estados-cuenta-muestra-demanda/1032.pdf",
    "1502": _STORE / "docs/samples/1502.pdf",
}
_PDF_1405 = _STORE / "media/mirador-1405-mora/1405_mirador_f881.pdf"


@unittest.skipUnless(
    all(p.is_file() for p in _PDFS_REALES.values()),
    "PDFs reales del store no disponibles en este entorno",
)
class PdfsRealesSaldoAnteriorTests(unittest.TestCase):
    def test_los_8_pdfs_reales_leen_saldo_anterior_cero_no_none(self):
        """Antes: 7 daban None. El saldo anterior real de los 8 es 0 (la tabla
        de movimientos empieza en 0; los montos de la portada son saldos por concepto)."""
        for nombre, ruta in _PDFS_REALES.items():
            out = analizar_estado_cuenta_pdf(ruta.read_bytes())
            self.assertEqual(out["saldo_anterior"], 0.0, nombre)
            self.assertEqual(out["cabecera"]["saldo_anterior"], 0.0, nombre)

    def test_pdf_124_certifica_la_sancion_de_44000(self):
        out = analizar_estado_cuenta_pdf(_PDFS_REALES["124"].read_bytes())
        r = determinar_mora_y_capital_limpio(out["rows"], saldo_anterior=out["saldo_anterior"])
        sanciones = [i for i in r["capital_limpio_a_demandar"] if i["clase_concepto"] == "sancion"]
        self.assertEqual(sum(i["valor_a_demandar"] for i in sanciones), 44_000.0)
        self.assertFalse(r["alerta_saldo_anterior"])
        self.assertTrue(r["alerta_sanciones"])


@unittest.skipUnless(_PDF_1405.is_file(), "PDF Mirador 1-405 no disponible")
class Pdf1405SaldoAnteriorTests(unittest.TestCase):
    def test_1405_conserva_997872_y_certifica_la_multa(self):
        out = analizar_estado_cuenta_pdf(_PDF_1405.read_bytes())
        self.assertEqual(out["saldo_anterior"], 997872.0)
        r = determinar_mora_y_capital_limpio(out["rows"], saldo_anterior=out["saldo_anterior"])
        self.assertEqual(r["saldo_anterior_modo"], "fila_saldo_anterior")
        self.assertEqual(r["saldo_anterior_pendiente"], 0.0)  # cubierto por 2.050.000 de abonos
        self.assertEqual(r["fecha_inicio_mora"], "2024.07")
        self.assertEqual(r["sanciones_certificadas"], 45_000.0)
        self.assertEqual(r["total_capital_demandado"], 4_177_118.0)


if __name__ == "__main__":
    unittest.main()
