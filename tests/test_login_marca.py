"""La página de login muestra la marca existente (balanza) sin depender de un CDN."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

PLANTILLA = ROOT / "templates" / "login.html"
MARCA = ROOT / "static" / "img" / "marca-despacho.svg"


class TestLoginMarca(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = PLANTILLA.read_text(encoding="utf-8")
        cls.marca = MARCA.read_text(encoding="utf-8")

    def test_marca_estatica_es_la_balanza_del_menu(self) -> None:
        self.assertTrue(MARCA.is_file())
        # Trazos de Lucide "scale", el mismo ícono del menú lateral.
        self.assertIn("m16 16 3-8 3 8", self.marca)
        self.assertIn("M12 3v18", self.marca)
        self.assertIn("Despacho Digital", self.marca)

    def test_plantilla_pone_la_marca_arriba_del_formulario(self) -> None:
        self.assertIn('href="/static/img/marca-despacho.svg"', self.html)
        self.assertIn('src="/static/img/marca-despacho.svg"', self.html)
        self.assertIn('alt="Despacho Digital"', self.html)
        self.assertLess(
            self.html.index('src="/static/img/marca-despacho.svg"'),
            self.html.index('<form action="/login"'),
        )

    def test_conserva_el_texto_en_espanol(self) -> None:
        for texto in (
            "Acceso Restringido - Despacho Digital",
            "Acceso exclusivo para equipo jurídico",
            "Correo Corporativo",
            "Contraseña",
            "Ingresar al Sistema",
            "Gestión Legal",
        ):
            self.assertIn(texto, self.html, texto)


class TestLoginMarcaHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        import main

        cls.client = TestClient(main.app, follow_redirects=False)

    def test_login_abre_con_la_marca(self) -> None:
        respuesta = self.client.get("/login")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('src="/static/img/marca-despacho.svg"', respuesta.text)
        self.assertIn("Acceso Restringido", respuesta.text)

    def test_marca_se_sirve_sin_sesion(self) -> None:
        respuesta = self.client.get("/static/img/marca-despacho.svg")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("m16 16 3-8 3 8", respuesta.text)


if __name__ == "__main__":
    unittest.main()
