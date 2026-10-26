"""La página pública de gestionlegal.co conserva los datos reales y el tono acordado."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

PLANTILLA = ROOT / "templates" / "landing_gestionlegal.html"
ESTATICA = ROOT / "static" / "landing.html"


def _contraste(frente: str, fondo: str) -> float:
    def canal(hex_color: str, indice: int) -> float:
        valor = int(hex_color[indice : indice + 2], 16) / 255
        return valor / 12.92 if valor <= 0.04045 else ((valor + 0.055) / 1.055) ** 2.4

    def luminancia(hex_color: str) -> float:
        rojo, verde, azul = canal(hex_color, 1), canal(hex_color, 3), canal(hex_color, 5)
        return 0.2126 * rojo + 0.7152 * verde + 0.0722 * azul

    clara, oscura = sorted((luminancia(frente), luminancia(fondo)))
    return (oscura + 0.05) / (clara + 0.05)


class TestLandingPublica(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = PLANTILLA.read_text(encoding="utf-8")
        entorno = Environment(
            loader=FileSystemLoader(str(ROOT / "templates")),
            undefined=StrictUndefined,
            autoescape=True,
        )
        cls.render = entorno.get_template("landing_gestionlegal.html").render()

    def test_la_plantilla_renderiza_sin_variables(self) -> None:
        self.assertIn("Diego Alejandro Paredes García", self.render)
        self.assertNotIn("{{", self.render)

    def test_conserva_datos_reales_y_servicios(self) -> None:
        for texto in (
            "contacto@gestionlegal.co",
            "Pereira, Risaralda",
            "Distrito Judicial de Pereira",
            "Derecho civil y comercial",
            "Cobro de cartera",
            "Propiedad horizontal",
            "Incumplimiento de contratos",
            "Resolución de negocios",
            "Medidas cautelares",
            "Títulos valores",
            "Cuotas de administración en mora",
            "Actas de asamblea",
            "Comités de convivencia",
            "Ley 675 de 2001",
            "Ley 1581 de 2012",
            "art. 34 de la Ley 1123 de 2007",
            "Circular 002 de la SIC",
            "Teléfono o WhatsApp",
            "L.T. No. 44.872",
            "Cobro prejudicial",
            "Restitución del inmueble",
            "Certificación de deuda",
            "artículo 30 de la Ley 675 de 2001",
            "inteligencia artificial",
            "https://wa.me/573202937064",
            'data-whatsapp="573202937064"',
            'href="/politica-de-datos"',
            'href="/login"',
        ):
            self.assertIn(texto, self.html, texto)

    def test_no_inventa_credenciales_ni_recupera_el_tono_anterior(self) -> None:
        prohibido = (
            "T.P.",
            "Ficha de control",
            "Ficha de Control",
            "forense",
            "Forense",
            "precalificación",
            "Régimen disciplinario",
            "[TELEFONO]",
            "[DIRECCION",
            "testimonio",
            "Testimonio",
            "premio",
            "Escribir al despacho",
            "Aquí va la fotografía",
            "soluciones integrales",
        )
        for texto in prohibido:
            self.assertNotIn(texto, self.html, texto)
        self.assertEqual(self.html.count("position: fixed"), 1)
        self.assertNotIn("position:fixed", self.html)
        self.assertNotRegex(self.html, r'id="tasa-ibc"[^>]*\svalue=')

    def test_whatsapp_usa_el_numero_del_despacho(self) -> None:
        self.assertIn('data-whatsapp="573202937064"', self.html)
        self.assertIn("mailto:contacto@gestionlegal.co", self.html)
        self.assertEqual(self.html.count("https://wa.me/573202937064"), 5)
        self.assertIn('class="wa-float"', self.html)

    def test_fuentes_autoalojadas_existen(self) -> None:
        for nombre in (
            "fraunces-latin.woff2",
            "fraunces-latin-italic.woff2",
            "source-sans-3-latin.woff2",
        ):
            self.assertIn(f"/static/fonts/{nombre}", self.html)
            self.assertTrue((ROOT / "static" / "fonts" / nombre).is_file(), nombre)

    def test_contraste_de_los_pares_visibles(self) -> None:
        pares = {
            "texto sobre papel": ("#1c2430", "#f6f1e7"),
            "marino sobre papel": ("#1c3348", "#f6f1e7"),
            "texto de botón": ("#f7f3ea", "#1c3348"),
            "texto sobre cartera": ("#f7f3ea", "#1c3348"),
            "placeholder": ("#3e5163", "#fffdf8"),
            "texto sobre pie": ("#1c2430", "#e3d9c8"),
            "texto sobre formulario": ("#1c2430", "#fffdf8"),
            "aviso del formulario": ("#143221", "#e7f0e6"),
            "texto sobre contacto": ("#1c2430", "#ebe3d4"),
        }
        for nombre, (frente, fondo) in pares.items():
            relacion = _contraste(frente, fondo)
            self.assertGreaterEqual(relacion, 7, f"{nombre}: {relacion:.2f}")

    def test_politica_de_datos_sin_inventar_direccion(self) -> None:
        politica = (ROOT / "templates" / "politica_datos.html").read_text(encoding="utf-8")
        for texto in (
            "Diego Alejandro Paredes García",
            "contacto@gestionlegal.co",
            "Ley 1581 de 2012",
            "Pereira",
        ):
            self.assertIn(texto, politica, texto)
        self.assertNotIn("T.P.", politica)
        self.assertNotIn("Calle", politica)
        self.assertNotIn("[DIRECCION", politica)

    def test_copia_estatica_no_sirve_el_diseno_viejo(self) -> None:
        estatica = ESTATICA.read_text(encoding="utf-8")
        self.assertIn('url=/landing', estatica)
        self.assertNotIn("Ficha de control", estatica)
        self.assertNotIn("position: fixed", estatica)


class TestLandingPublicaHttp(unittest.TestCase):
    """Con default-deny RBAC, las páginas públicas deben abrir sin sesión."""

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        import main

        cls.client = TestClient(main.app, follow_redirects=False)

    def test_paginas_publicas_abren_sin_sesion(self) -> None:
        for ruta, texto in (
            ("/", "Diego Alejandro Paredes García"),
            ("/landing", "Diego Alejandro Paredes García"),
            ("/politica-de-datos", "Ley 1581 de 2012"),
        ):
            with self.subTest(ruta=ruta):
                respuesta = self.client.get(ruta)
                self.assertEqual(respuesta.status_code, 200)
                self.assertIn(texto, respuesta.text)

    def test_politica_de_datos_es_ruta_publica_explicita(self) -> None:
        import permisos

        self.assertTrue(permisos.es_ruta_publica("/politica-de-datos"))
        self.assertFalse(permisos.es_ruta_publica("/politica-de-datos/x"))

    def test_fuentes_se_sirven_sin_sesion(self) -> None:
        for nombre in (
            "fraunces-latin.woff2",
            "fraunces-latin-italic.woff2",
            "source-sans-3-latin.woff2",
        ):
            respuesta = self.client.get(f"/static/fonts/{nombre}")
            self.assertEqual(respuesta.status_code, 200, nombre)

    def test_no_hay_endpoints_de_escritura_en_la_pagina_publica(self) -> None:
        for ruta in ("/landing", "/politica-de-datos"):
            self.assertEqual(self.client.post(ruta).status_code, 405, ruta)


if __name__ == "__main__":
    unittest.main()
