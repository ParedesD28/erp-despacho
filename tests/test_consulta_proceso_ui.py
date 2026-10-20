"""UI /herramientas/consulta-proceso — auth y mensaje sin LEGAL_API_BASE_URL."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import permisos


ROOT = Path(__file__).resolve().parents[1]


class TestConsultaProcesoUi(unittest.TestCase):
    def test_ruta_y_plantilla_existen(self) -> None:
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/herramientas/consulta-proceso")', main_src)
        self.assertIn("LegalAPIClient", main_src)
        self.assertTrue((ROOT / "templates" / "consulta_proceso.html").is_file())
        informes = (ROOT / "templates" / "informes.html").read_text(encoding="utf-8")
        self.assertIn("/herramientas/consulta-proceso", informes)

    def test_plantilla_sin_host_hardcodeado(self) -> None:
        html = (ROOT / "templates" / "consulta_proceso.html").read_text(encoding="utf-8")
        self.assertIn("Falta configurar LEGAL_API_BASE_URL", html)
        self.assertNotIn("http://", html.lower())
        self.assertNotIn("https://", html.lower())

    def test_get_exige_nav_informes(self) -> None:
        consulta = permisos.permisos_de_perfil(permisos.PERFIL_CONSULTA)
        abogado = permisos.permisos_de_perfil(permisos.PERFIL_ABOGADO)
        sin_informes = frozenset(
            p for p in abogado if p != permisos.NAV_INFORMES
        )
        self.assertFalse(
            permisos.denegar_acceso(consulta, "GET", "/herramientas/consulta-proceso")
        )
        self.assertTrue(
            permisos.denegar_acceso(sin_informes, "GET", "/herramientas/consulta-proceso")
        )

    def test_sin_env_muestra_mensaje(self) -> None:
        import asyncio

        secret = "x" * 32
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "LEGAL_API_BASE_URL"
        }
        env["ERP_SESSION_SECRET"] = secret
        with patch.dict(os.environ, env, clear=True):
            import main as erp_main

            captured: dict = {}

            def fake_render(name, context, status_code=200):
                captured["name"] = name
                captured["context"] = context
                return MagicMock(status_code=status_code)

            with patch.object(erp_main, "render_template", side_effect=fake_render):
                asyncio.run(erp_main.vista_consulta_proceso(MagicMock(), radicado="123"))

        self.assertEqual(captured["name"], "consulta_proceso.html")
        self.assertFalse(captured["context"]["api_configurada"])
        self.assertEqual(
            captured["context"]["error"],
            "Falta configurar LEGAL_API_BASE_URL",
        )


if __name__ == "__main__":
    unittest.main()
