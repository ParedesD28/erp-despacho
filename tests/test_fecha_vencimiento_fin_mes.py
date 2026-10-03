"""Regla: fecha_vencimiento de cuotas = último día calendario del mes del concepto."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from liquidador import fecha_vencimiento_fin_mes


class FechaVencimientoFinMesTests(unittest.TestCase):
    def test_enero_31(self):
        self.assertEqual(fecha_vencimiento_fin_mes(2024, 1), date(2024, 1, 31))

    def test_febrero_bisiesto_29(self):
        self.assertEqual(fecha_vencimiento_fin_mes(2024, 2), date(2024, 2, 29))

    def test_febrero_no_bisiesto_28(self):
        self.assertEqual(fecha_vencimiento_fin_mes(2023, 2), date(2023, 2, 28))

    def test_abril_30(self):
        self.assertEqual(fecha_vencimiento_fin_mes(2024, 4), date(2024, 4, 30))

    def test_diciembre_31(self):
        self.assertEqual(fecha_vencimiento_fin_mes(2025, 12), date(2025, 12, 31))

    def test_isoformat_para_insert(self):
        self.assertEqual(fecha_vencimiento_fin_mes(2024, 1).isoformat(), "2024-01-31")
        self.assertEqual(fecha_vencimiento_fin_mes(2024, 2).isoformat(), "2024-02-29")

    def test_mes_invalido(self):
        with self.assertRaises(ValueError):
            fecha_vencimiento_fin_mes(2024, 0)
        with self.assertRaises(ValueError):
            fecha_vencimiento_fin_mes(2024, 13)


if __name__ == "__main__":
    unittest.main()
