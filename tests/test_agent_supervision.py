"""Regresiones del chat de supervisión WhatsApp / agente IA."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)


class NormalizacionTelefonoTests(unittest.TestCase):
    def test_solo_digitos(self):
        import agent_supervision as sup

        self.assertEqual(sup.normalizar_telefono_digits("+57 310 692 7812"), "573106927812")
        self.assertEqual(sup.normalizar_telefono_digits("310-692-7812"), "3106927812")
        self.assertEqual(sup.normalizar_telefono_digits(""), "")

    def test_claves_match_incluye_variantes_57(self):
        import agent_supervision as sup

        claves = set(sup.telefono_claves_match("573106927812"))
        self.assertIn("573106927812", claves)
        self.assertIn("3106927812", claves)


class EnriquecerMensajeTests(unittest.TestCase):
    def test_imagen_sin_url_genera_placeholder(self):
        import agent_supervision as sup

        m = sup.enriquecer_mensaje(
            {
                "tipo_mensaje": "image",
                "contenido": "[Imagen recibida; presumiblemente comprobante de pago]",
                "fecha": "2026-10-03T15:30:00+00:00",
                "autor": "DEUDOR",
                "direccion": "ENTRANTE",
            }
        )
        self.assertTrue(m["tiene_imagen"])
        tipos = [a["tipo"] for a in m["adjuntos"]]
        self.assertIn("image_placeholder", tipos)

    def test_pdf_en_metadata(self):
        import agent_supervision as sup

        m = sup.enriquecer_mensaje(
            {
                "tipo_mensaje": "document",
                "contenido": "[Documento PDF enviado]",
                "metadata": {"url_pdf": "https://example.com/liq.pdf"},
                "autor": "AGENTE",
                "direccion": "SALIENTE",
            }
        )
        docs = [a for a in m["adjuntos"] if a["tipo"] == "document"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["url"], "https://example.com/liq.pdf")

    def test_url_imagen_en_contenido(self):
        import agent_supervision as sup

        m = sup.enriquecer_mensaje(
            {
                "tipo_mensaje": "text",
                "contenido": "Comprobante https://cdn.example.com/abono.png listo",
                "autor": "DEUDOR",
                "direccion": "ENTRANTE",
            }
        )
        self.assertTrue(m["tiene_imagen"])
        imgs = [a for a in m["adjuntos"] if a["tipo"] == "image"]
        self.assertEqual(imgs[0]["url"], "https://cdn.example.com/abono.png")

    def test_metadata_json_string(self):
        import agent_supervision as sup

        m = sup.enriquecer_mensaje(
            {
                "contenido": "x",
                "metadata": '{"media_url":"https://x.test/a.jpg","mime_type":"image/jpeg"}',
            }
        )
        self.assertTrue(m["tiene_imagen"])
        self.assertEqual(m["adjuntos"][0]["url"], "https://x.test/a.jpg")


class ConversacionesEnrichmentTests(unittest.TestCase):
    def test_enriquece_nombre_y_aviso_ia(self):
        import agent_supervision as sup

        fake_nombres = {
            "3106927812": {
                "nombre": "Juan Pérez",
                "fuente": "agenda",
                "telefono_display": "3106927812",
                "notas": "",
                "contacto_id": None,
            }
        }
        data = {
            "status": "success",
            "conversaciones": [
                {
                    "telefono": "573106927812",
                    "identificacion": "123",
                    "modo_actual": "AGENTE",
                    "fecha_inicio": "2026-10-01T10:00:00Z",
                    "fecha_ultima_actividad": "2026-10-03T12:00:00Z",
                },
                {
                    "telefono": "3001112233",
                    "modo_actual": "HUMANO",
                    "fecha_ultima_actividad": "2026-10-03T11:00:00Z",
                },
            ],
        }
        with patch.object(sup, "_cargar_nombres_agenda", return_value=fake_nombres):
            out = sup._normalizar_conversaciones(data, buscar="")
        filas = out["conversaciones"]
        self.assertEqual(filas[0]["nombre"], "Juan Pérez")
        self.assertEqual(filas[0]["display_name"], "Juan Pérez")
        self.assertTrue(filas[0]["ia_atiende"])
        self.assertFalse(filas[1]["ia_atiende"])
        self.assertEqual(out["aviso_ia"]["total_ia"], 1)
        self.assertTrue(out["aviso_ia"]["activo"])

    def test_filtro_buscar_por_nombre(self):
        import agent_supervision as sup

        fake_nombres = {
            "3001112233": {
                "nombre": "María López",
                "fuente": "contactos",
                "telefono_display": "3001112233",
                "notas": "",
                "contacto_id": 1,
            }
        }
        data = {
            "conversaciones": [
                {"telefono": "3001112233", "modo_actual": "AGENTE"},
                {"telefono": "3009998877", "modo_actual": "AGENTE"},
            ]
        }
        with patch.object(sup, "_cargar_nombres_agenda", return_value=fake_nombres):
            out = sup._normalizar_conversaciones(data, buscar="maría")
        self.assertEqual(len(out["conversaciones"]), 1)
        self.assertEqual(out["conversaciones"][0]["nombre"], "María López")


class WebhookIaTests(unittest.TestCase):
    def test_webhook_no_op_sin_env(self):
        import agent_supervision as sup

        with patch.dict(os.environ, {"SUPERVISION_IA_WEBHOOK_URL": ""}, clear=False):
            with patch("agent_supervision.requests.post") as post:
                sup._disparar_webhook_ia("ia_atiende", {"telefono": "1"})
                post.assert_not_called()

    def test_webhook_dispara_si_env(self):
        import agent_supervision as sup

        with patch.dict(
            os.environ, {"SUPERVISION_IA_WEBHOOK_URL": "https://hooks.example/test"}, clear=False
        ):
            with patch("agent_supervision.requests.post") as post:
                post.return_value = MagicMock()
                sup._disparar_webhook_ia("ia_atiende", {"telefono": "57"})
                post.assert_called_once()
                args, kwargs = post.call_args
                self.assertEqual(args[0], "https://hooks.example/test")
                self.assertEqual(kwargs["json"]["evento"], "ia_atiende")


class TemplateSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tpl = (ROOT / "templates" / "supervision_agente.html").read_text(encoding="utf-8")
        self.src = (ROOT / "agent_supervision.py").read_text(encoding="utf-8")

    def test_ui_tiene_fechas_whatsapp_y_lightbox(self):
        self.assertIn("etiquetaDia", self.tpl)
        self.assertIn("Hoy", self.tpl)
        self.assertIn("Ayer", self.tpl)
        self.assertIn("lightbox", self.tpl)
        self.assertIn("abrirLightbox", self.tpl)
        self.assertIn("renderAdjuntos", self.tpl)

    def test_ui_agenda_y_aviso_ia(self):
        self.assertIn("editarNombre", self.tpl)
        self.assertIn("/supervision-agente/api/agenda", self.tpl)
        self.assertIn("aviso-ia", self.tpl)
        self.assertIn("toast-ia", self.tpl)
        self.assertIn("Buscar nombre, teléfono o cédula", self.tpl)

    def test_api_agenda_y_aviso_en_router(self):
        self.assertIn('/supervision-agente/api/agenda', self.src)
        self.assertIn('/supervision-agente/api/aviso-ia', self.src)
        self.assertIn("whatsapp_agenda", self.src)
        self.assertIn("SUPERVISION_IA_WEBHOOK_URL", self.src)

    def test_migracion_agenda_existe(self):
        mig = ROOT / "migrations" / "20261003_whatsapp_agenda.sql"
        self.assertTrue(mig.is_file())
        sql = mig.read_text(encoding="utf-8")
        self.assertIn("whatsapp_agenda", sql)
        self.assertIn("telefono_digits", sql)


if __name__ == "__main__":
    unittest.main()
