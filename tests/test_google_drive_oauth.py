"""OAuth connect helpers + admin routes (mocks; sin Google real)."""
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


class OAuthHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["ERP_SESSION_SECRET"] = (
            "ci-dummy-secret-not-used-in-production-000000"
        )
        os.environ["PUBLIC_BASE_URL"] = "https://gestionlegal.co"
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid.apps.googleusercontent.com"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "csecret"
        os.environ.pop("GOOGLE_OAUTH_REDIRECT_URI", None)

    def tearDown(self) -> None:
        for k in (
            "PUBLIC_BASE_URL",
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_OAUTH_REDIRECT_URI",
        ):
            os.environ.pop(k, None)

    def test_redirect_uri_default(self) -> None:
        from google_drive_oauth import redirect_uri

        self.assertEqual(
            redirect_uri(),
            "https://gestionlegal.co/herramientas/google-drive/oauth/callback",
        )

    def test_state_roundtrip(self) -> None:
        from google_drive_oauth import mint_oauth_state, verify_oauth_state

        state = mint_oauth_state(user_id="42")
        self.assertTrue(verify_oauth_state(state, user_id="42"))
        self.assertFalse(verify_oauth_state(state + "x", user_id="42"))
        self.assertFalse(verify_oauth_state("garbage", user_id="42"))

    def test_authorize_url_contains_scope_and_offline(self) -> None:
        from google_drive_oauth import build_authorize_url, mint_oauth_state

        url = build_authorize_url(state=mint_oauth_state(user_id="1"))
        self.assertIn("accounts.google.com", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("drive.file", url)
        self.assertIn("gestionlegal.co", url)

    def test_fernet_roundtrip(self) -> None:
        from app_settings import decrypt_value, encrypt_value

        enc = encrypt_value("refresh-token-xyz")
        self.assertNotEqual(enc, "refresh-token-xyz")
        self.assertEqual(decrypt_value(enc), "refresh-token-xyz")


class OAuthRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main as main_mod

        cls.app = main_mod.app

    def test_google_drive_page_exige_admin(self) -> None:
        from fastapi.testclient import TestClient
        import permisos

        client = TestClient(self.app)
        # Sin sesión → redirect login
        resp = client.get("/herramientas/google-drive", follow_redirects=False)
        self.assertIn(resp.status_code, (303, 302))

        def _ctx(request, uid):
            request.state.user_id = uid
            request.state.perfil_codigo = permisos.PERFIL_ABOGADO
            request.state.permisos = permisos.permisos_de_perfil(permisos.PERFIL_ABOGADO)
            request.state.user_nombre = "X"
            return None

        with patch("main.verify_session", return_value="u1"), patch(
            "main._aplicar_contexto_permisos", side_effect=_ctx
        ):
            resp = client.get("/herramientas/google-drive", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/informes", resp.headers.get("location", ""))

    def test_callback_guarda_token(self) -> None:
        from fastapi.testclient import TestClient
        import google_drive_oauth as gdo
        import permisos

        client = TestClient(self.app)
        state = gdo.mint_oauth_state(user_id="7")

        def _ctx(request, uid):
            request.state.user_id = "7"
            request.state.perfil_codigo = permisos.PERFIL_ADMIN
            request.state.permisos = permisos.permisos_de_perfil(permisos.PERFIL_ADMIN)
            return None

        with patch("main.verify_session", return_value="7"), patch(
            "main._aplicar_contexto_permisos", side_effect=_ctx
        ), patch(
            "google_drive_oauth.exchange_code_for_tokens",
            return_value={"refresh_token": "rt-test-abc", "access_token": "at"},
        ), patch(
            "app_settings.save_google_oauth_refresh_token", return_value=True
        ) as mock_save:
            client.cookies.set(gdo.STATE_COOKIE, state)
            resp = client.get(
                "/herramientas/google-drive/oauth/callback",
                params={"code": "authcode", "state": state},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"rt-test-abc", resp.content)
        self.assertNotIn(b"<textarea", resp.content)
        self.assertIn(b"guardado", resp.content.lower())
        mock_save.assert_called_once_with("rt-test-abc")

    def test_callback_muestra_token_solo_si_falla_guardado(self) -> None:
        from fastapi.testclient import TestClient
        import google_drive_oauth as gdo
        import permisos

        client = TestClient(self.app)
        state = gdo.mint_oauth_state(user_id="7")

        def _ctx(request, uid):
            request.state.user_id = "7"
            request.state.perfil_codigo = permisos.PERFIL_ADMIN
            request.state.permisos = permisos.permisos_de_perfil(permisos.PERFIL_ADMIN)
            return None

        with patch("main.verify_session", return_value="7"), patch(
            "main._aplicar_contexto_permisos", side_effect=_ctx
        ), patch(
            "google_drive_oauth.exchange_code_for_tokens",
            return_value={"refresh_token": "rt-test-abc", "access_token": "at"},
        ), patch(
            "app_settings.save_google_oauth_refresh_token", return_value=False
        ):
            client.cookies.set(gdo.STATE_COOKIE, state)
            resp = client.get(
                "/herramientas/google-drive/oauth/callback",
                params={"code": "authcode", "state": state},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"rt-test-abc", resp.content)
        self.assertEqual(resp.headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
