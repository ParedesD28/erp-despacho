"""Extracción de saldo_anterior (metadata) — no inventar filas FAC."""
from __future__ import annotations

import unittest
from pathlib import Path

from estado_cuenta_pdf_service import extraer_saldo_anterior, analizar_estado_cuenta_pdf


class ExtraerSaldoAnteriorTests(unittest.TestCase):
    def test_linea_colon_tipica(self):
        lineas = [
            "Concepto Documento Fecha Valor Abono Saldo",
            "Saldo anterior     .  .              0              0        997,872",
            "CUOTAS DE ADMINISTRACION FAC 0001783 2024.01.01         98,000              0      1,095,872",
        ]
        self.assertEqual(extraer_saldo_anterior(lineas), 997872.0)

    def test_sin_linea_devuelve_none(self):
        self.assertIsNone(extraer_saldo_anterior(["Periodo 1", "Total 100"]))

    def test_ultima_ocurrencia_gana(self):
        lineas = [
            "Saldo anterior 100",
            "Saldo anterior     .  .              0              0        997,872",
        ]
        self.assertEqual(extraer_saldo_anterior(lineas), 997872.0)

    def test_no_es_concepto_valido_para_movimiento(self):
        from estado_cuenta_pdf_service import _es_concepto_valido

        self.assertFalse(_es_concepto_valido("Saldo anterior"))
        self.assertFalse(_es_concepto_valido("saldo anterior"))


_PDF_MIRADOR = Path(
    "/cursor/stores/bc-2b92a817-5490-4c88-a556-8ad39d11cb5d/media/"
    "mirador-1405-mora/1405_mirador_f881.pdf"
)


@unittest.skipUnless(_PDF_MIRADOR.is_file(), "PDF Mirador 1-405 no disponible en este entorno")
class PdfMiradorSaldoAnteriorTests(unittest.TestCase):
    def test_pdf_real_expone_saldo_anterior(self):
        out = analizar_estado_cuenta_pdf(_PDF_MIRADOR.read_bytes())
        self.assertEqual(out["fuente_parseo"], "colon_texto")
        self.assertEqual(out.get("saldo_anterior"), 997872.0)
        self.assertEqual(out["cabecera"].get("saldo_anterior"), 997872.0)
        # No debe aparecer como fila de movimiento
        conceptos = {(r.get("Concepto") or "").lower() for r in out["rows"]}
        self.assertNotIn("saldo anterior", conceptos)


class IaCabeceraSaldoAnteriorTests(unittest.TestCase):
    def test_mapea_saldo_anterior_sin_inventar_fila(self):
        from estado_cuenta_ia_vision import validar_y_mapear_respuesta_ia

        data = {
            "cabecera": {
                "conjunto": "X",
                "titular": "T",
                "bloque": "1",
                "apartamento": "405",
                "codigo_cuenta": "1405",
                "nit": "1",
                "saldo_anterior": 997872,
            },
            "movimientos": [
                {
                    "concepto": "CUOTAS DE ADMINISTRACION",
                    "tipo_documento": "FAC",
                    "numero": "0001783",
                    "fecha": "2024.01.01",
                    "valor": 98000,
                    "abono": 0,
                    "saldo": 1095872,
                }
            ],
        }
        parcial = validar_y_mapear_respuesta_ia(data)
        self.assertEqual(parcial["cabecera"]["saldo_anterior"], 997872.0)
        self.assertEqual(len(parcial["rows"]), 1)

    def test_saldo_anterior_ausente_queda_none(self):
        from estado_cuenta_ia_vision import validar_y_mapear_respuesta_ia

        data = {
            "cabecera": {
                "conjunto": "X",
                "titular": "T",
                "bloque": "1",
                "apartamento": "1",
                "codigo_cuenta": "1",
                "nit": "1",
            },
            "movimientos": [
                {
                    "concepto": "CUOTAS DE ADMINISTRACION",
                    "tipo_documento": "FAC",
                    "numero": "0001",
                    "fecha": "2024.01.01",
                    "valor": 100,
                    "abono": 0,
                    "saldo": 100,
                }
            ],
        }
        parcial = validar_y_mapear_respuesta_ia(data)
        self.assertIsNone(parcial["cabecera"]["saldo_anterior"])


if __name__ == "__main__":
    unittest.main()
