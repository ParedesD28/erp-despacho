"""UI /herramientas/consulta-proceso — auth y mensaje sin LEGAL_API_BASE_URL."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("ERP_SESSION_SECRET", "x" * 32)

import permisos


ROOT = Path(__file__).resolve().parents[1]

# URL ficticia solo para tests (nunca un host real de gobierno).
_TEST_BASE = "https://ejemplo.invalid/api/v1"


class TestConsultaProcesoUi(unittest.TestCase):
    def test_ruta_y_plantilla_existen(self) -> None:
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/herramientas/consulta-proceso")', main_src)
        self.assertIn(
            '@app.get("/herramientas/consulta-proceso/debug-env")',
            main_src,
        )
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
        self.assertFalse(
            permisos.denegar_acceso(
                consulta, "GET", "/herramientas/consulta-proceso/debug-env"
            )
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

    def test_con_env_no_muestra_falta_configurar_por_error_html(self) -> None:
        """Regresión: detail con 'LEGAL_API_BASE_URL' (HTML/WAF) ≠ env ausente."""
        import asyncio
        from contextlib import asynccontextmanager

        from fastapi import HTTPException

        import main as erp_main

        html_detail = (
            "La API devolvió HTML (revisa LEGAL_API_BASE_URL: debe ser la base "
            "REST, no el portal web). HTTP 200, Content-Type: text/html"
        )

        @asynccontextmanager
        async def fake_client():
            client = MagicMock()

            async def boom(_radicado: str):
                raise HTTPException(status_code=502, detail=html_detail)

            client.consultar_id_por_radicado = boom
            yield client

        captured: dict = {}

        def fake_render(name, context, status_code=200):
            captured["name"] = name
            captured["context"] = context
            return MagicMock(status_code=status_code)

        with patch.dict(os.environ, {"LEGAL_API_BASE_URL": _TEST_BASE}, clear=False):
            with patch(
                "external_legal_client.LegalAPIClient",
                side_effect=lambda *a, **k: fake_client(),
            ):
                with patch.object(erp_main, "render_template", side_effect=fake_render):
                    asyncio.run(
                        erp_main.vista_consulta_proceso(
                            MagicMock(), radicado="66001418900120260028400"
                        )
                    )

        self.assertTrue(captured["context"]["api_configurada"])
        self.assertEqual(captured["context"]["error"], html_detail)
        self.assertNotEqual(
            captured["context"]["error"],
            "Falta configurar LEGAL_API_BASE_URL",
        )
        self.assertNotIn(
            "Falta configurar LEGAL_API_BASE_URL",
            captured["context"]["error"],
        )

    def test_con_env_mapea_solo_detail_exacto_no_configurada(self) -> None:
        import asyncio
        from contextlib import asynccontextmanager

        from fastapi import HTTPException

        import main as erp_main

        @asynccontextmanager
        async def fake_client():
            client = MagicMock()

            async def boom(_radicado: str):
                raise HTTPException(
                    status_code=503, detail="LEGAL_API_BASE_URL no configurada"
                )

            client.consultar_id_por_radicado = boom
            yield client

        captured: dict = {}

        def fake_render(name, context, status_code=200):
            captured["context"] = context
            return MagicMock(status_code=status_code)

        with patch.dict(os.environ, {"LEGAL_API_BASE_URL": _TEST_BASE}, clear=False):
            with patch(
                "external_legal_client.LegalAPIClient",
                side_effect=lambda *a, **k: fake_client(),
            ):
                with patch.object(erp_main, "render_template", side_effect=fake_render):
                    asyncio.run(
                        erp_main.vista_consulta_proceso(MagicMock(), radicado="123")
                    )

        self.assertEqual(
            captured["context"]["error"],
            "Falta configurar LEGAL_API_BASE_URL",
        )

    def test_debug_env_payload_sin_url(self) -> None:
        import main as erp_main

        with patch.dict(os.environ, {"LEGAL_API_BASE_URL": _TEST_BASE}, clear=False):
            payload = erp_main._legal_api_env_debug_payload()
        self.assertEqual(
            set(payload),
            {
                "legal_api_base_url_present",
                "legal_api_base_url_len",
                "startswith_https",
            },
        )
        self.assertTrue(payload["legal_api_base_url_present"])
        self.assertEqual(payload["legal_api_base_url_len"], len(_TEST_BASE))
        self.assertTrue(payload["startswith_https"])
        dumped = json.dumps(payload)
        self.assertNotIn("ejemplo.invalid", dumped)
        self.assertNotIn(_TEST_BASE, dumped)
        self.assertNotIn("://", dumped)

    def test_debug_env_payload_ausente(self) -> None:
        import main as erp_main

        env = {
            k: v
            for k, v in os.environ.items()
            if k != "LEGAL_API_BASE_URL"
        }
        with patch.dict(os.environ, env, clear=True):
            payload = erp_main._legal_api_env_debug_payload()
        self.assertFalse(payload["legal_api_base_url_present"])
        self.assertEqual(payload["legal_api_base_url_len"], 0)
        self.assertFalse(payload["startswith_https"])

    def test_debug_env_solo_admin(self) -> None:
        import main as erp_main

        req_admin = MagicMock()
        req_admin.state.perfil_codigo = permisos.PERFIL_ADMIN
        with patch.dict(os.environ, {"LEGAL_API_BASE_URL": _TEST_BASE}, clear=False):
            resp = erp_main.debug_legal_api_env(req_admin)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body.decode("utf-8"))
        self.assertTrue(body["legal_api_base_url_present"])
        self.assertNotIn("ejemplo.invalid", json.dumps(body))

        req_abogado = MagicMock()
        req_abogado.state.perfil_codigo = permisos.PERFIL_ABOGADO
        resp_deny = erp_main.debug_legal_api_env(req_abogado)
        self.assertEqual(resp_deny.status_code, 403)


if __name__ == "__main__":
    unittest.main()
