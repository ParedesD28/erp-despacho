"""Tests del fallback IA (pool gratis + Claude last) para PDF sin texto nativo."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from estado_cuenta_ia_vision import (
    EstadoCuentaIaError,
    _construir_kwargs_mensaje,
    _es_error_tool_choice_no_soportado,
    _modelo_default,
    _parse_json_respuesta,
    _use_tools_habilitado,
    extraer_estado_cuenta_via_ia,
    ia_fallback_habilitado,
    proveedor_ia,
    proveedores_ia,
    reparar_json_ligero,
    validar_y_mapear_respuesta_ia,
)

_CLEAR_IA_ENV = {
    "ANTHROPIC_API_KEY": "",
    "GEMINI_API_KEY": "",
    "GROQ_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "PDF_IA_PROVIDER": "",
    "PDF_IA_PROVIDERS": "",
    "ESTADO_CUENTA_IA_PROVIDER": "",
    "ESTADO_CUENTA_IA_PROVIDERS": "",
    "ESTADO_CUENTA_IA_FALLBACK": "",
    "ESTADO_CUENTA_IA_MODEL": "",
}


def _pop_ia_keys() -> None:
    for k in (
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "PDF_IA_PROVIDER",
        "PDF_IA_PROVIDERS",
        "ESTADO_CUENTA_IA_PROVIDER",
        "ESTADO_CUENTA_IA_PROVIDERS",
        "ESTADO_CUENTA_IA_MODEL",
    ):
        os.environ.pop(k, None)
from estado_cuenta_pdf_service import EstadoCuentaPdfError, analizar_estado_cuenta_pdf

_SAMPLES = Path(
    "/cursor/stores/bc-2b92a817-5490-4c88-a556-8ad39d11cb5d/docs/samples"
)


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


def _mov_minimo(concepto: str = "CUOTA ADMIN") -> dict:
    return {
        "concepto": concepto,
        "tipo_documento": "FAC",
        "numero": "0001234",
        "fecha": "2026.01.01",
        "valor": 100,
        "abono": 0,
        "saldo": 100,
    }


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
            "movimientos": [_mov_minimo()],
        }
        data["movimientos"][0]["fecha"] = "2026-01-01"
        parcial = validar_y_mapear_respuesta_ia(data)
        self.assertEqual(parcial["rows"][0]["Fecha"], "2026.01.01")


class ParseJsonRobustoTests(unittest.TestCase):
    def test_concepto_con_comillas_sin_escapar(self):
        # típico fallo prod: Expecting ',' delimiter en concepto
        roto = (
            '{\n  "cabecera": {"titular": "X", "bloque": "1", "apartamento": "103",'
            ' "codigo_cuenta": "1103", "conjunto": null, "nit": null},\n'
            '  "movimientos": [\n'
            '    {"concepto": "CUOTA "ESPECIAL" ADMINISTRACION", "tipo_documento": "FAC",'
            ' "numero": "0001111", "fecha": "2026.01.01", "valor": 100, "abono": 0, "saldo": 100}\n'
            "  ]\n}"
        )
        with self.assertRaises(json.JSONDecodeError):
            json.loads(roto)
        data = _parse_json_respuesta(roto)
        self.assertEqual(
            data["movimientos"][0]["concepto"],
            'CUOTA "ESPECIAL" ADMINISTRACION',
        )
        parcial = validar_y_mapear_respuesta_ia(data)
        self.assertEqual(parcial["rows"][0]["Valor"], 100.0)

    def test_fenced_markdown_json(self):
        bueno = {
            "cabecera": {"titular": "Y", "bloque": "1", "apartamento": "1", "codigo_cuenta": "1"},
            "movimientos": [_mov_minimo("RETROACTIVO AÑO 2026")],
        }
        fenced = "Aquí va:\n```json\n" + json.dumps(bueno, ensure_ascii=False) + "\n```\n"
        data = _parse_json_respuesta(fenced)
        self.assertEqual(data["movimientos"][0]["concepto"], "RETROACTIVO AÑO 2026")

    def test_trailing_comma(self):
        crudo = (
            '{"cabecera": {"titular": "Z", "bloque": "1", "apartamento": "1",'
            ' "codigo_cuenta": "1", "conjunto": null, "nit": null},'
            ' "movimientos": [{"concepto": "CUOTA ADMIN", "tipo_documento": "FAC",'
            ' "numero": "0001234", "fecha": "2026.01.01", "valor": 50, "abono": 0,'
            ' "saldo": 50,},],}'
        )
        data = _parse_json_respuesta(crudo)
        self.assertEqual(data["movimientos"][0]["valor"], 50)

    def test_reparar_no_cambia_montos(self):
        bueno = json.dumps(
            {
                "cabecera": {},
                "movimientos": [
                    {
                        "concepto": "X",
                        "tipo_documento": "FAC",
                        "numero": "1",
                        "fecha": "2026.01.01",
                        "valor": 1234567,
                        "abono": 0,
                        "saldo": 1234567,
                    }
                ],
            }
        )
        fixed = reparar_json_ligero(bueno)
        self.assertEqual(json.loads(fixed)["movimientos"][0]["valor"], 1234567)

    def test_retry_cuando_json_invalido(self):
        bueno = {
            "cabecera": {"titular": "R", "bloque": "1", "apartamento": "1", "codigo_cuenta": "9"},
            "movimientos": [_mov_minimo()],
        }
        calls = {"n": 0}

        def fake_call(pdf_bytes, *, provider=None, model=None, retry_json=False):
            calls["n"] += 1
            if calls["n"] == 1:
                return '{"cabecera": {, "movimientos": []'  # basura
            return bueno

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gem-test", "PDF_IA_PROVIDER": "gemini"}, clear=False):
            with patch("estado_cuenta_ia_vision._llamar_proveedor_pdf", side_effect=fake_call):
                parcial = extraer_estado_cuenta_via_ia(b"%PDF-fake")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(parcial["rows"][0]["Concepto"], "CUOTA ADMIN")


class FlagIaTests(unittest.TestCase):
    def test_default_off_sin_key(self):
        with patch.dict(os.environ, _CLEAR_IA_ENV, clear=False):
            _pop_ia_keys()
            self.assertFalse(ia_fallback_habilitado())

    def test_on_con_gemini_key(self):
        with patch.dict(
            os.environ,
            {**_CLEAR_IA_ENV, "GEMINI_API_KEY": "gem-test"},
            clear=False,
        ):
            _pop_ia_keys()
            os.environ["GEMINI_API_KEY"] = "gem-test"
            self.assertEqual(proveedor_ia(), "gemini")
            self.assertTrue(ia_fallback_habilitado())

    def test_on_con_anthropic_sin_gemini(self):
        with patch.dict(
            os.environ,
            {**_CLEAR_IA_ENV, "ANTHROPIC_API_KEY": "sk-test"},
            clear=False,
        ):
            _pop_ia_keys()
            os.environ["ANTHROPIC_API_KEY"] = "sk-test"
            self.assertEqual(proveedor_ia(), "anthropic")
            self.assertTrue(ia_fallback_habilitado())

    def test_force_provider_anthropic(self):
        with patch.dict(
            os.environ,
            {
                **_CLEAR_IA_ENV,
                "PDF_IA_PROVIDER": "anthropic",
                "ANTHROPIC_API_KEY": "sk-test",
                "GEMINI_API_KEY": "gem-test",
            },
            clear=False,
        ):
            self.assertEqual(proveedor_ia(), "anthropic")

    def test_pool_claude_ultimo(self):
        with patch.dict(
            os.environ,
            {
                **_CLEAR_IA_ENV,
                "GEMINI_API_KEY": "g",
                "GROQ_API_KEY": "q",
                "ANTHROPIC_API_KEY": "a",
            },
            clear=False,
        ):
            pool = proveedores_ia()
            self.assertEqual(pool[0], "gemini")
            self.assertIn("groq", pool)
            self.assertEqual(pool[-1], "anthropic")

    def test_pdf_ia_providers_orden(self):
        with patch.dict(
            os.environ,
            {
                **_CLEAR_IA_ENV,
                "PDF_IA_PROVIDERS": "groq,gemini,anthropic",
                "GROQ_API_KEY": "q",
                "GEMINI_API_KEY": "g",
                "ANTHROPIC_API_KEY": "a",
            },
            clear=False,
        ):
            self.assertEqual(proveedores_ia(), ["groq", "gemini", "anthropic"])

    def test_cascada_failover_al_siguiente(self):
        bueno = {
            "cabecera": {"titular": "T", "bloque": "1", "apartamento": "1", "codigo_cuenta": "1"},
            "movimientos": [_mov_minimo()],
        }
        calls: list[str] = []

        def fake_call(pdf_bytes, *, provider=None, model=None, retry_json=False):
            calls.append(provider or "?")
            if provider == "gemini":
                raise EstadoCuentaIaError("Cuota/rate-limit de gemini agotada.")
            return json.dumps(bueno)

        with patch.dict(
            os.environ,
            {
                **_CLEAR_IA_ENV,
                "PDF_IA_PROVIDERS": "gemini,groq,anthropic",
                "GEMINI_API_KEY": "g",
                "GROQ_API_KEY": "q",
                "ANTHROPIC_API_KEY": "a",
            },
            clear=False,
        ):
            with patch("estado_cuenta_ia_vision._llamar_proveedor_pdf", side_effect=fake_call):
                parcial = extraer_estado_cuenta_via_ia(b"%PDF-fake")
        self.assertEqual(calls, ["gemini", "groq"])
        self.assertEqual(parcial.get("proveedor_ia"), "groq")
        self.assertEqual(parcial["cabecera"]["titular"], "T")

    def test_force_off(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gem-test", "ESTADO_CUENTA_IA_FALLBACK": "0"},
            clear=False,
        ):
            self.assertFalse(ia_fallback_habilitado())


class AnalizarFallbackMockTests(unittest.TestCase):
    def test_sin_texto_sin_ia_mensaje_claro(self):
        with patch.dict(
            os.environ,
            {**_CLEAR_IA_ENV, "ESTADO_CUENTA_IA_FALLBACK": "0"},
            clear=False,
        ):
            _pop_ia_keys()
            os.environ["ESTADO_CUENTA_IA_FALLBACK"] = "0"
            with self.assertRaises(EstadoCuentaPdfError) as ctx:
                analizar_estado_cuenta_pdf(_pdf_sin_texto())
            self.assertIn("texto seleccionable", str(ctx.exception).lower())

    def test_sin_texto_con_mock_ia(self):
        parcial = validar_y_mapear_respuesta_ia(_json_ia_1204())
        with patch.dict(
            os.environ,
            {**_CLEAR_IA_ENV, "GEMINI_API_KEY": "gem-test", "ESTADO_CUENTA_IA_FALLBACK": "1"},
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

        with patch.dict(
            os.environ,
            {**_CLEAR_IA_ENV, "PDF_IA_PROVIDER": "gemini"},
            clear=False,
        ):
            _pop_ia_keys()
            os.environ["PDF_IA_PROVIDER"] = "gemini"
            with self.assertRaises(EstadoCuentaIaError) as ctx:
                _require_api_key("gemini")
            self.assertIn("configure GEMINI_API_KEY", str(ctx.exception))

    def test_tool_use_payload_directo(self):
        """Si Claude responde con tool_use, el input dict se usa sin json.loads."""
        message = MagicMock()
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "extraer_estado_cuenta"
        tool_block.input = {
            "cabecera": {"titular": "T", "bloque": "1", "apartamento": "1", "codigo_cuenta": "1"},
            "movimientos": [_mov_minimo()],
        }
        message.content = [tool_block]
        from estado_cuenta_ia_vision import _extraer_payload_mensaje

        payload = _extraer_payload_mensaje(message)
        self.assertIsInstance(payload, dict)
        parcial = validar_y_mapear_respuesta_ia(payload)
        self.assertEqual(parcial["rows"][0]["Concepto"], "CUOTA ADMIN")


class SinToolChoiceTests(unittest.TestCase):
    """Default prod-safe: sin tool_choice tool/any (evita BadRequest 400)."""

    def test_use_tools_default_off(self):
        with patch.dict(os.environ, {"ESTADO_CUENTA_IA_USE_TOOLS": ""}, clear=False):
            os.environ.pop("ESTADO_CUENTA_IA_USE_TOOLS", None)
            self.assertFalse(_use_tools_habilitado())

    def test_kwargs_default_sin_tools_ni_tool_choice(self):
        kwargs = _construir_kwargs_mensaje(
            modelo="claude-sonnet-5-5",
            content=[{"type": "text", "text": "x"}],
            use_tools=False,
            include_output_config=False,
        )
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("tool_choice", kwargs)
        self.assertEqual(kwargs["model"], "claude-sonnet-5-5")

    def test_kwargs_opt_in_tools(self):
        kwargs = _construir_kwargs_mensaje(
            modelo="claude-sonnet-5-5",
            content=[{"type": "text", "text": "x"}],
            use_tools=True,
            include_output_config=False,
        )
        self.assertIn("tools", kwargs)
        self.assertEqual(kwargs["tool_choice"]["type"], "tool")
        self.assertEqual(kwargs["tool_choice"]["name"], "extraer_estado_cuenta")

    def test_detecta_error_tool_choice_400(self):
        class BadRequestError(Exception):
            pass

        exc = BadRequestError(
            'tool_choice: type "tool" and "any" are not supported for this model.'
        )
        self.assertTrue(_es_error_tool_choice_no_soportado(exc))

    def test_llamar_claude_default_no_envia_tool_choice(self):
        """Path sin tools: messages.create no debe recibir tool_choice."""
        bueno = {
            "cabecera": {
                "titular": "T",
                "bloque": "1",
                "apartamento": "103",
                "codigo_cuenta": "1103",
                "conjunto": None,
                "nit": None,
            },
            "movimientos": [_mov_minimo()],
        }
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = json.dumps(bueno)
        message = MagicMock()
        message.content = [text_block]

        captured: dict = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured["kwargs"] = kwargs
                if "tool_choice" in kwargs:
                    raise AssertionError(
                        "tool_choice no debe enviarse en el path default sin tools"
                    )
                return message

        class FakeClient:
            def __init__(self, *a, **k):
                self.messages = FakeMessages()

        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-test",
                "ESTADO_CUENTA_IA_USE_TOOLS": "",
            },
            clear=False,
        ):
            os.environ.pop("ESTADO_CUENTA_IA_USE_TOOLS", None)
            with patch("anthropic.Anthropic", FakeClient):
                from estado_cuenta_ia_vision import _llamar_claude_pdf

                payload = _llamar_claude_pdf(b"%PDF-fake", use_tools=False)

        self.assertNotIn("tool_choice", captured["kwargs"])
        self.assertNotIn("tools", captured["kwargs"])
        self.assertIsInstance(payload, str)
        parcial = validar_y_mapear_respuesta_ia(_parse_json_respuesta(payload))
        self.assertEqual(parcial["cabecera"]["codigo_cuenta"], "1103")

    def test_fallback_texto_si_tool_choice_400(self):
        """Si opt-in tools falla con 400, reintenta sin tools (no propaga 400)."""
        bueno = {
            "cabecera": {
                "titular": "U",
                "bloque": "1",
                "apartamento": "404",
                "codigo_cuenta": "1404",
            },
            "movimientos": [_mov_minimo()],
        }
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = json.dumps(bueno)
        message_ok = MagicMock()
        message_ok.content = [text_block]

        class BadRequestError(Exception):
            pass

        calls: list[dict] = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                if "tool_choice" in kwargs:
                    raise BadRequestError(
                        'Error code: 400 - tool_choice: type "tool" and "any" '
                        "are not supported for this model."
                    )
                return message_ok

        class FakeClient:
            def __init__(self, *a, **k):
                self.messages = FakeMessages()

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            with patch("anthropic.Anthropic", FakeClient):
                from estado_cuenta_ia_vision import _llamar_claude_pdf

                payload = _llamar_claude_pdf(b"%PDF-fake", use_tools=True)

        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("tool_choice", calls[0])
        self.assertNotIn("tool_choice", calls[-1])
        self.assertIsInstance(payload, str)
        data = _parse_json_respuesta(payload)
        self.assertEqual(data["cabecera"]["codigo_cuenta"], "1404")

    def test_modelo_respeta_anthropic_model_env(self):
        with patch.dict(
            os.environ,
            {
                "PDF_IA_PROVIDER": "anthropic",
                "ESTADO_CUENTA_IA_MODEL": "",
                "ANTHROPIC_MODEL": "claude-3-5-haiku-latest",
            },
            clear=False,
        ):
            os.environ.pop("ESTADO_CUENTA_IA_MODEL", None)
            self.assertEqual(_modelo_default("anthropic"), "claude-3-5-haiku-latest")

    def test_modelo_gemini_default(self):
        with patch.dict(
            os.environ,
            {
                "PDF_IA_PROVIDER": "gemini",
                "ESTADO_CUENTA_IA_MODEL": "",
                "GEMINI_MODEL": "",
            },
            clear=False,
        ):
            os.environ.pop("ESTADO_CUENTA_IA_MODEL", None)
            os.environ.pop("GEMINI_MODEL", None)
            self.assertEqual(_modelo_default("gemini"), "gemini-2.5-flash")

    def test_modelo_groq_default(self):
        with patch.dict(
            os.environ,
            {
                **_CLEAR_IA_ENV,
                "PDF_IA_PROVIDER": "groq",
                "GROQ_MODEL": "",
            },
            clear=False,
        ):
            os.environ.pop("GROQ_MODEL", None)
            os.environ.pop("ESTADO_CUENTA_IA_MODEL", None)
            self.assertEqual(_modelo_default("groq"), "qwen/qwen3.8-27b")

    def test_modelo_deepseek_default(self):
        with patch.dict(
            os.environ,
            {
                **_CLEAR_IA_ENV,
                "PDF_IA_PROVIDER": "deepseek",
                "DEEPSEEK_MODEL": "",
            },
            clear=False,
        ):
            os.environ.pop("DEEPSEEK_MODEL", None)
            os.environ.pop("ESTADO_CUENTA_IA_MODEL", None)
            self.assertEqual(_modelo_default("deepseek"), "deepseek-flash")


class GeminiClientMockTests(unittest.TestCase):
    def test_llamar_gemini_pdf_inline(self):
        bueno = {
            "cabecera": {
                "titular": "G",
                "bloque": "1",
                "apartamento": "204",
                "codigo_cuenta": "1204",
                "conjunto": None,
                "nit": None,
            },
            "movimientos": [_mov_minimo()],
        }
        response = MagicMock()
        response.text = json.dumps(bueno)
        response.candidates = []

        class FakeModels:
            def generate_content(self, **kwargs):
                self.last_kwargs = kwargs
                return response

        class FakeClient:
            def __init__(self, *a, **k):
                self.models = FakeModels()

        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gem-test", "PDF_IA_PROVIDER": "gemini"},
            clear=False,
        ):
            with patch("google.genai.Client", FakeClient):
                from estado_cuenta_ia_vision import _llamar_gemini_pdf

                payload = _llamar_gemini_pdf(b"%PDF-fake")

        data = _parse_json_respuesta(payload)
        self.assertEqual(data["cabecera"]["codigo_cuenta"], "1204")
        parcial = validar_y_mapear_respuesta_ia(data)
        self.assertEqual(parcial["rows"][0]["Concepto"], "CUOTA ADMIN")

    def test_extraer_despacha_a_gemini(self):
        bueno = {
            "cabecera": {"titular": "G", "bloque": "1", "apartamento": "1", "codigo_cuenta": "1"},
            "movimientos": [_mov_minimo()],
        }
        with patch.dict(
            os.environ,
            {**_CLEAR_IA_ENV, "PDF_IA_PROVIDER": "gemini", "GEMINI_API_KEY": "gem"},
            clear=False,
        ):
            with patch(
                "estado_cuenta_ia_vision._llamar_gemini_pdf",
                return_value=json.dumps(bueno),
            ) as mock_g:
                with patch("estado_cuenta_ia_vision._llamar_claude_pdf") as mock_c:
                    parcial = extraer_estado_cuenta_via_ia(b"%PDF-fake")
        mock_g.assert_called_once()
        mock_c.assert_not_called()
        self.assertEqual(parcial["cabecera"]["titular"], "G")
        self.assertEqual(parcial.get("proveedor_ia"), "gemini")


class GroqOpenRouterMockTests(unittest.TestCase):
    def test_llamar_groq_usa_imagenes_y_api(self):
        bueno = {
            "cabecera": {"titular": "Q", "bloque": "1", "apartamento": "1", "codigo_cuenta": "1"},
            "movimientos": [_mov_minimo()],
        }

        class FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(bueno)}}]}

        with patch.dict(
            os.environ,
            {**_CLEAR_IA_ENV, "PDF_IA_PROVIDER": "groq", "GROQ_API_KEY": "gsk-test"},
            clear=False,
        ):
            with patch(
                "estado_cuenta_ia_vision._pdf_paginas_jpeg_b64",
                return_value=["aaa", "bbb", "ccc", "ddd"],
            ):
                with patch("estado_cuenta_ia_vision.requests.post", return_value=FakeResp()) as mock_post:
                    from estado_cuenta_ia_vision import _llamar_groq_pdf

                    payload = _llamar_groq_pdf(b"%PDF-fake")
        # 4 páginas / máx 3 por request → 2 llamadas
        self.assertEqual(mock_post.call_count, 2)
        data = _parse_json_respuesta(payload)
        self.assertEqual(data["cabecera"]["titular"], "Q")

    def test_merge_payloads_ia(self):
        from estado_cuenta_ia_vision import _merge_payloads_ia

        merged = _merge_payloads_ia(
            [
                {
                    "cabecera": {"titular": "A", "bloque": None, "apartamento": "1"},
                    "movimientos": [_mov_minimo()],
                },
                {
                    "cabecera": {"titular": None, "bloque": "2", "apartamento": None},
                    "movimientos": [
                        {
                            **_mov_minimo(),
                            "numero": "0000999",
                            "concepto": "INTERES ADMIN",
                        }
                    ],
                },
            ]
        )
        self.assertEqual(merged["cabecera"]["titular"], "A")
        self.assertEqual(merged["cabecera"]["bloque"], "2")
        self.assertEqual(len(merged["movimientos"]), 2)


class ParserInline1502Tests(unittest.TestCase):
    @unittest.skipUnless((_SAMPLES / "1502.pdf").is_file(), "sample 1502.pdf no disponible")
    def test_1502_texto_inline_nativo(self):
        out = analizar_estado_cuenta_pdf((_SAMPLES / "1502.pdf").read_bytes())
        self.assertEqual(out["fuente_parseo"], "colon_texto")
        self.assertGreaterEqual(out["movimientos_extraidos"], 100)
        self.assertEqual(out["titular"], "CORTES DIAZ MICHAEL JOHEL")
        self.assertEqual(out["bloque"], "1")
        self.assertEqual(out["apartamento"], "502")
        self.assertEqual(out["codigo_cuenta"], "1502")
        self.assertEqual(out["inconsistencias_saldo"], 0)
        self.assertAlmostEqual(out["saldo_final"], 8471643.0)


@unittest.skipUnless(
    (
        bool((os.environ.get("GEMINI_API_KEY") or "").strip())
        or bool((os.environ.get("GROQ_API_KEY") or "").strip())
        or bool((os.environ.get("OPENROUTER_API_KEY") or "").strip())
        or bool((os.environ.get("DEEPSEEK_API_KEY") or "").strip())
        or bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    )
    and os.environ.get("ESTADO_CUENTA_IA_SMOKE", "").strip() in ("1", "true", "yes"),
    "Smoke opcional: export GEMINI/GROQ/OPENROUTER/DEEPSEEK/ANTHROPIC + ESTADO_CUENTA_IA_SMOKE=1",
)
class SmokeIa1204Tests(unittest.TestCase):
    def test_1204_pdf_real(self):
        from bolsa_global_estado_cuenta_service import calcular_bolsa_por_cuenta

        sample = _SAMPLES / "1204.pdf"
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
        print(
            json.dumps(
                {
                    "proveedor": proveedor_ia(),
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
