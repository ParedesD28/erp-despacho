"""
Regresión mínima: paralelismo acotado del lote PDF → Excel.

No requiere PDF COLON reales: usa blobs inválidos (error aislado por archivo)
y un mock de parseo para verificar orden + workers.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from estado_cuenta_pdf_service import (
    PARSE_WORKERS,
    UI_BATCH_SIZE,
    _parsear_lote_acotado,
    procesar_lote_estados_cuenta,
)


class ThroughputConfigTests(unittest.TestCase):
    def test_defaults_seguros_para_render(self):
        self.assertGreaterEqual(PARSE_WORKERS, 1)
        self.assertLessEqual(PARSE_WORKERS, 8)
        self.assertGreaterEqual(UI_BATCH_SIZE, 1)
        self.assertLessEqual(UI_BATCH_SIZE, 25)


class ParseoParaleloTests(unittest.TestCase):
    def test_lote_invalido_preserva_orden_y_aisla_errores(self):
        archivos = [
            ("a.pdf", b"no-pdf-a"),
            ("b.pdf", b"no-pdf-b"),
            ("c.pdf", b"no-pdf-c"),
        ]
        resultado = procesar_lote_estados_cuenta(archivos, incluir_excel=False)
        self.assertEqual(resultado["archivos_recibidos"], 3)
        self.assertEqual(resultado["archivos_fallidos"], 3)
        nombres = [c["archivo"] for c in resultado["cuentas"]]
        self.assertEqual(nombres, ["a.pdf", "b.pdf", "c.pdf"])
        for c in resultado["cuentas"]:
            self.assertTrue(c.get("error"))
            self.assertEqual(c.get("movimientos_extraidos"), 0)
        # Bytes liberados tras parseo (anti-OOM).
        self.assertTrue(all(blob == b"" for _, blob in archivos))

    def test_workers_1_y_4_mismo_orden(self):
        archivos_base = [(f"f{i}.pdf", f"bad-{i}".encode()) for i in range(6)]

        with mock.patch(
            "estado_cuenta_pdf_service._procesar_un_pdf_en_lote",
            side_effect=lambda filename, _contenido: {
                "archivo": filename,
                "titular": filename,
                "bloque": None,
                "apartamento": None,
                "codigo_cuenta": None,
                "conjunto": None,
                "fechas_detectadas": 0,
                "movimientos_extraidos": 1,
                "bloques_omitidos": 0,
                "inconsistencias_saldo": 0,
                "advertencia_encoding": False,
                "saldo_final": 0,
                "alerta_calidad": False,
                "rows": [],
                "error": None,
            },
        ):
            seq = _parsear_lote_acotado(list(archivos_base), max_workers=1)
            par = _parsear_lote_acotado(list(archivos_base), max_workers=4)

        self.assertEqual([c["archivo"] for c in seq], [f"f{i}.pdf" for i in range(6)])
        self.assertEqual([c["archivo"] for c in par], [c["archivo"] for c in seq])

    def test_paralelismo_real_termina_mas_rapido_que_serie(self):
        """Smoke: 4 workers con sleep artificial < 4× sleep serie (margen holgado)."""

        def lento(filename, _contenido):
            time.sleep(0.05)
            return {
                "archivo": filename,
                "titular": None,
                "bloque": None,
                "apartamento": None,
                "codigo_cuenta": None,
                "conjunto": None,
                "fechas_detectadas": 0,
                "movimientos_extraidos": 0,
                "bloques_omitidos": 0,
                "inconsistencias_saldo": 0,
                "advertencia_encoding": False,
                "saldo_final": None,
                "alerta_calidad": True,
                "rows": [],
                "error": "mock",
            }

        items = [(f"x{i}.pdf", b"x") for i in range(4)]
        with mock.patch(
            "estado_cuenta_pdf_service._procesar_un_pdf_en_lote",
            side_effect=lento,
        ):
            t0 = time.perf_counter()
            _parsear_lote_acotado(list(items), max_workers=1)
            serie = time.perf_counter() - t0

            t1 = time.perf_counter()
            _parsear_lote_acotado(list(items), max_workers=4)
            paralelo = time.perf_counter() - t1

        # Serie ≈ 0.20s; paralelo ≈ 0.05–0.10s. Margen amplio por CI lento.
        self.assertLess(paralelo, serie * 0.85)


if __name__ == "__main__":
    unittest.main()
