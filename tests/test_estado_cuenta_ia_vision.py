"""Tests del fallback Claude para PDF sin texto nativo."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from estado_cuenta_ia_vision import (
    EstadoCuentaIaError,
    ia_fallback_habilitado,
    validar_y_mapear_respuesta_ia,
)
from estado_cuenta_pdf_service import EstadoCuentaPdfError, analizar_estado_cuenta_pdf


def _json_ia_1204() -> dict:
    """Respuesta Claude simulada alineada al ground truth de 1204.pdf."""
    return {
        "cabecera": {
            "conjunto": "CONJUNTO RESIDENCIAL MIRADOR DEL PORTAL",
            "titular": "SANTA LUNA MARIO GERMAN",
            "bloque": "1",
            "apartamento": "204",
            "codigo_cuenta": "1204",
            "nit": "901501807-5",
        },
        "movimientos": [
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003479",
                "fecha": "2026.01.01",
                "valor": 129350,
                "abono": 0,
                "saldo": 129350,
            },
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "RDC",
                "numero": "0002340",
                "fecha": "2026.01.09",
                "valor": 0,
                "abono": 129350,
                "saldo": 0,
            },
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003550",
                "fecha": "2026.02.01",
                "valor": 129350,
                "abono": 0,
                "saldo": 129350,
            },
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003621",
                "fecha": "2026.03.01",
                "valor": 129350,
                "abono": 0,
                "saldo": 258700,
            },
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003692",
                "fecha": "2026.04.01",
                "valor": 148100,
                "abono": 0,
                "saldo": 406800,
            },
            {
                "concepto": "RETROACTIVO AÑO 2026",
                "tipo_documento": "FAC",
                "numero": "0003692",
                "fecha": "2026.04.01",
                "valor": 28125,
                "abono": 0,
                "saldo": 434925,
            },
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003763",
                "fecha": "2026.05.01",
                "valor": 148100,
                "abono": 0,
                "saldo": 583025,
            },
            {
                "concepto": "INTERESES ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003763",
                "fecha": "2026.05.01",
                "valor": 8502,
                "abono": 0,
                "saldo": 591527,
            },
            {
                "concepto": "RETROACTIVO AÑO 2026",
                "tipo_documento": "FAC",
                "numero": "0003763",
                "fecha": "2026.05.01",
                "valor": 28125,
                "abono": 0,
                "saldo": 619652,
            },
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003836",
                "fecha": "2026.06.01",
                "valor": 148100,
                "abono": 0,
                "saldo": 767752,
            },
            {
                "concepto": "INTERESES ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003836",
                "fecha": "2026.06.01",
                "valor": 11825,
                "abono": 0,
                "saldo": 779577,
            },
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003907",
                "fecha": "2026.07.01",
                "valor": 148100,
                "abono": 0,
                "saldo": 927677,
            },
            {
                "concepto": "INTERESES ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003907",
                "fecha": "2026.07.01",
                "valor": 14981,
                "abono": 0,
                "saldo": 942658,
            },
            {
                "concepto": "CUOTAS DE ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003978",
                "fecha": "2026.08.01",
                "valor": 148100,
                "abono": 0,
                "saldo": 1090758,
            },
            {
                "concepto": "INTERESES ADMINISTRACION",
                "tipo_documento": "FAC",
                "numero": "0003978",
                "fecha": "2026.08.01",
                "valor": 18622,
                "abono": 0,
                "saldo": 1109380,
            },
            {
                "concepto": "CUOTA DE SEGURO AREAS COMUNES",
                "tipo_documento": "FAC",
                "numero": "0003978",
                "fecha": "2026.08.01",
                "valor": 74383,
                "abono": 0,
                "saldo": 1183763,
            },
        ],
    }


def _pdf_sin_texto() -> bytes:
    """PDF mínimo de 2 páginas sin texto seleccionable (pypdf-friendly)."""
    # Construye un PDF trivial con 2 páginas vacías vía reportlab si está, si no bytes fijos.
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
        import io

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        c.showPage()
        c.showPage()
        c.save()
        return buf.getvalue()
    except Exception:
        # Fallback: PDF 1.4 con 2 páginas vacías (sin operadores de texto).
        return (
            b"%PDF-1.4\n"
            b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
            b"2 0 obj<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>endobj\n"
            b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
            b"/Resources << >> >>endobj\n"
            b"4 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R "
            b"/Resources << >> >>endobj\n"
            b"5 0 obj<< /Length 0 >>stream\nendstream\nendobj\n"
            b"6 0 obj<< /Length 0 >>stream\nendstream\nendobj\n"
            b"xref\n0 7\n0000000000 65535 f \n"
            b"trailer<< /Size 7 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
        )


class ValidacionJsonIaTests(unittest.TestCase):
    def test_mapea_cabecera_y_movimientos(self):
        parcial = validar_y_mapear_respuesta_ia(_json_ia_1204())
        self.assertEqual(parcial["cabecera"]["titular"], "SANTA LUNA MARIO GERMAN")
        self.assertEqual(parcial["cabecera"]["bloque"], "1")
        self.assertEqual(parcial["cabecera"]["apartamento"], "204")
        self.assertEqual(parcial["cabecera"]["codigo_cuenta"], "1204")
        self.assertEqual(len(parcial["rows"]), 16)
        self.assertEqual(parcial["rows"][-1]["Saldo"], 1183763.0)
        self.assertEqual(parcial["rows"][0]["Tipo Documento"], "FAC")

    def test_rechaza_sin_movimientos(self):
        with self.assertRaises(EstadoCuentaIaError):
            validar_y_mapear_respuesta_ia({"cabecera": {}, "movimientos": []})

    def test_normaliza_fecha_con_guiones(self):
        data = {
            "cabecera": {"titular": "X", "bloque": "1", "apartamento": "1", "codigo_cuenta": "1"},
            "movimientos": [
                {
                    "concepto": "CUOTA ADMIN",
                    "tipo_documento": "FAC",
                    "numero": "0001234",
                    "fecha": "2026-01-01",
                    "valor": 100,
                    "abono": 0,
                    "saldo": 100,
                }
            ],
        }
        parcial = validar_y_mapear_respuesta_ia(data)
        self.assertEqual(parcial["rows"][0]["Fecha"], "2026.01.01")


class FlagIaTests(unittest.TestCase):
    def test_default_off_sin_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "ESTADO_CUENTA_IA_FALLBACK": ""}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.assertFalse(ia_fallback_habilitado())

    def test_on_con_key(self):
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "sk-test", "ESTADO_CUENTA_IA_FALLBACK": ""},
            clear=False,
        ):
            self.assertTrue(ia_fallback_habilitado())

    def test_force_off(self):
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "sk-test", "ESTADO_CUENTA_IA_FALLBACK": "0"},
            clear=False,
        ):
            self.assertFalse(ia_fallback_habilitado())


class AnalizarFallbackMockTests(unittest.TestCase):
    def test_sin_texto_sin_ia_mensaje_claro(self):
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "", "ESTADO_CUENTA_IA_FALLBACK": "0"},
            clear=False,
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with self.assertRaises(EstadoCuentaPdfError) as ctx:
                analizar_estado_cuenta_pdf(_pdf_sin_texto())
            self.assertIn("texto seleccionable", str(ctx.exception).lower())

    def test_sin_texto_con_mock_ia(self):
        parcial = validar_y_mapear_respuesta_ia(_json_ia_1204())
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "sk-test", "ESTADO_CUENTA_IA_FALLBACK": "1"},
            clear=False,
        ):
            with patch(
                "estado_cuenta_pdf_service.extraer_estado_cuenta_via_ia",
                return_value=parcial,
            ) as mock_ia:
                out = analizar_estado_cuenta_pdf(_pdf_sin_texto())
        mock_ia.assert_called_once()
        self.assertEqual(out["fuente_parseo"], "ia_vision")
        self.assertEqual(out["movimientos_extraidos"], 16)
        self.assertEqual(out["titular"], "SANTA LUNA MARIO GERMAN")
        self.assertEqual(out["saldo_final"], 1183763.0)
        self.assertTrue(out["alerta_calidad"])
        self.assertTrue(any("IA" in a for a in out["advertencias"]))
        self.assertEqual(out["inconsistencias_saldo"], 0)

    def test_mensaje_sin_api_key_en_modulo(self):
        from estado_cuenta_ia_vision import _require_api_key

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with self.assertRaises(EstadoCuentaIaError) as ctx:
                _require_api_key()
            self.assertIn("configure ANTHROPIC_API_KEY", str(ctx.exception))


@unittest.skipUnless(
    bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    and os.environ.get("ESTADO_CUENTA_IA_SMOKE", "").strip() in ("1", "true", "yes"),
    "Smoke opcional: export ANTHROPIC_API_KEY y ESTADO_CUENTA_IA_SMOKE=1",
)
class SmokeIa1204Tests(unittest.TestCase):
    def test_1204_pdf_real(self):
        from pathlib import Path

        from bolsa_global_estado_cuenta_service import calcular_bolsa_por_cuenta

        sample = Path(
            "/cursor/stores/bc-2b92a817-5490-4c88-a556-8ad39d11cb5d/docs/samples/1204.pdf"
        )
        if not sample.is_file():
            self.skipTest("sample 1204.pdf no disponible")
        out = analizar_estado_cuenta_pdf(sample.read_bytes())
        self.assertEqual(out["fuente_parseo"], "ia_vision")
        self.assertGreaterEqual(out["movimientos_extraidos"], 10)
        self.assertIsNotNone(out.get("titular"))
        cuenta = {
            "archivo": "1204.pdf",
            "titular": out.get("titular"),
            "bloque": out.get("bloque"),
            "apartamento": out.get("apartamento"),
            "codigo_cuenta": out.get("codigo_cuenta"),
            "conjunto": out.get("conjunto"),
            "movimientos_extraidos": out["movimientos_extraidos"],
            "rows": out["rows"],
            "error": None,
        }
        bolsa = calcular_bolsa_por_cuenta(cuenta)
        self.assertFalse(bolsa.get("omitido"))
        # Evidencia mínima para el reporte del agente
        print(
            json.dumps(
                {
                    "titular": out.get("titular"),
                    "bloque": out.get("bloque"),
                    "apartamento": out.get("apartamento"),
                    "movimientos": out["movimientos_extraidos"],
                    "saldo_final": out.get("saldo_final"),
                    "inconsistencias_saldo": out.get("inconsistencias_saldo"),
                    "bolsa_total": bolsa.get("total_capital_demandado"),
                    "bolsa_items": len(bolsa.get("capital_limpio_a_demandar") or []),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
