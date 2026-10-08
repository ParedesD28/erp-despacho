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

    def test_psid_meta_no_es_telefono_legible(self):
        import agent_supervision as sup

        self.assertFalse(sup.es_telefono_whatsapp_legible("1442782103907655"))
        self.assertTrue(sup.es_telefono_whatsapp_legible("573126487636"))
        self.assertTrue(sup.es_telefono_whatsapp_legible("3126487636"))
        self.assertEqual(sup.formatear_telefono_display("573126487636"), "+57 312 648 7636")
        self.assertEqual(sup.formatear_telefono_display("1442782103907655"), "")


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

    def test_placeholder_texto_sin_tipo_image(self):
        import agent_supervision as sup

        m = sup.enriquecer_mensaje(
            {
                "tipo_mensaje": "text",
                "contenido": "[Imagen recibida; presumiblemente comprobante de pago]",
                "autor": "DEUDOR",
                "direccion": "ENTRANTE",
            }
        )
        self.assertEqual(m["tipo_mensaje"], "image")
        self.assertTrue(m["tiene_imagen"])
        self.assertIn("image_placeholder", [a["tipo"] for a in m["adjuntos"]])

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


class MensajesNormalizacionTests(unittest.TestCase):
    def test_quita_usuario_humano_de_mensajes_agente(self):
        import agent_supervision as sup

        data = {
            "mensajes": [
                {
                    "autor": "AGENTE",
                    "direccion": "SALIENTE",
                    "contenido": "Hola",
                    "tipo_mensaje": "text",
                    "usuario_humano": "abogado@erp",
                },
                {
                    "autor": "HUMANO",
                    "direccion": "SALIENTE",
                    "contenido": "Manual",
                    "tipo_mensaje": "text",
                    "usuario_humano": "abogado@erp",
                },
                {
                    "autor": "DEUDOR",
                    "direccion": "ENTRANTE",
                    "contenido": "[Imagen recibida; presumiblemente comprobante de pago]",
                    "tipo_mensaje": "image",
                    "usuario_humano": "abogado@erp",
                },
            ]
        }
        out = sup._normalizar_mensajes(data)
        self.assertNotIn("usuario_humano", out["mensajes"][0])
        self.assertEqual(out["mensajes"][1].get("usuario_humano"), "abogado@erp")
        self.assertNotIn("usuario_humano", out["mensajes"][2])
        self.assertEqual(out.get("usuario_humano"), "abogado@erp")
        self.assertTrue(out["mensajes"][2]["tiene_imagen"])


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
                    "total_mensajes": 4,
                },
                {
                    "telefono": "3001112233",
                    "modo_actual": "HUMANO",
                    "fecha_ultima_actividad": "2026-10-03T11:00:00Z",
                    "total_mensajes": 2,
                },
            ],
        }
        with patch.object(sup, "_cargar_nombres_agenda", return_value=fake_nombres):
            with patch.object(sup, "_cargar_contactos_por_identificacion", return_value={}):
                out = sup._normalizar_conversaciones(data, buscar="")
        filas = out["conversaciones"]
        self.assertEqual(filas[0]["nombre"], "Juan Pérez")
        self.assertEqual(filas[0]["display_name"], "Juan Pérez")
        self.assertTrue(filas[0]["ia_atiende"])
        self.assertFalse(filas[1]["ia_atiende"])
        self.assertEqual(out["aviso_ia"]["total_ia"], 1)
        self.assertTrue(out["aviso_ia"]["activo"])
        self.assertIn("mensaje", filas[0]["last_message"].lower())

    def test_preview_desde_total_mensajes_y_psid(self):
        import agent_supervision as sup

        data = {
            "conversaciones": [
                {
                    "telefono": "1442782103907655",
                    "identificacion": "9872330",
                    "modo_actual": "HUMANO",
                    "total_mensajes": 12,
                    "fecha_ultima_actividad": "2026-10-08T18:00:00Z",
                }
            ]
        }
        por_cedula = {
            "9872330": {
                "nombre": "Deudor Prueba",
                "telefono": "573001112233",
                "contacto_id": 9,
                "fuente": "contactos_cedula",
            }
        }
        with patch.object(sup, "_cargar_nombres_agenda", return_value={}):
            with patch.object(sup, "_cargar_contactos_por_identificacion", return_value=por_cedula):
                out = sup._normalizar_conversaciones(data, buscar="")
        fila = out["conversaciones"][0]
        self.assertEqual(fila["conversation_key"], "1442782103907655")
        self.assertTrue(fila["telefono_es_id_meta"])
        self.assertEqual(fila["telefono_resuelto"], "573001112233")
        self.assertIn("573", fila["telefono_display"].replace(" ", "").replace("+", ""))
        self.assertEqual(fila["nombre"], "Deudor Prueba")
        self.assertEqual(fila["last_message"], "12 mensaje(s) en el hilo")

    def test_preview_ultimo_mensaje_del_bot(self):
        import agent_supervision as sup

        data = {
            "conversaciones": [
                {
                    "telefono": "573106927812",
                    "modo_actual": "AGENTE",
                    "ultimo_mensaje": "[Imagen recibida; presumiblemente comprobante de pago]",
                    "total_mensajes": 3,
                }
            ]
        }
        with patch.object(sup, "_cargar_nombres_agenda", return_value={}):
            with patch.object(sup, "_cargar_contactos_por_identificacion", return_value={}):
                out = sup._normalizar_conversaciones(data, buscar="")
        self.assertIn("Imagen", out["conversaciones"][0]["last_message"])

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
            with patch.object(sup, "_cargar_contactos_por_identificacion", return_value={}):
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

    def test_ui_telefono_visible_y_preview(self):
        self.assertIn("telefonoVisible", self.tpl)
        self.assertIn("conversationKey", self.tpl)
        self.assertIn("image_placeholder", self.tpl)
        self.assertIn("Autor explícito gana sobre usuario_humano", self.tpl)
        self.assertIn("Sin teléfono WhatsApp", self.tpl)
        self.assertIn('usuario_humano', self.src)
        self.assertIn('autor != "HUMANO"', self.src)

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
