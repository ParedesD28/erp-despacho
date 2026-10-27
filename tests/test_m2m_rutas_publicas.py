"""Rutas M2M públicas (sin sesión): cada una debe autenticarse por sí misma."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api_recaudos
import bot_api
import grok_api
import main as main_mod
import permisos
import rate_limit

OLD = "old-shared-key-AAAAAAAAAAAAAAAAAAAA"
NEW_REC = "new-recaudos-key-CCCCCCCCCCCCCCCCC"
NEW_BOT = "new-bot-key-BBBBBBBBBBBBBBBBBBBBBBB"

M2M_VARS = (
    "LIQUIDADOR_API_KEY",
    "BOT_API_KEY",
    "RECAUDOS_API_KEY",
    "BOT_PDF_PUBLIC_SECRET",
    "AGENT_SUPERVISION_KEY",
    "M2M_LEGACY_KEY_ENABLED",
)

# Rutas bajo /api/ públicas en el middleware que NO usan llave X-API-Key y por qué.
AUTH_PROPIA = {
    "/api/bot/pdf/{filename}": "URL firmada HMAC con expiración",
    "/api/v1/cron/sync-rama": "X-Cron-Secret",
}


def _env(**values):
    base = {k: "" for k in M2M_VARS}
    base.update(values)
    return mock.patch.dict(os.environ, base)


def _dependencias(route: APIRoute):
    return {d.dependency for d in route.dependencies}


class AuditoriaRutasPublicasTests(unittest.TestCase):
    def _rutas_publicas_api(self):
        """Rutas /api/ públicas: las del app y las de cada APIRouter de módulos del proyecto.

        Se recorren los routers directamente (no ``app.routes``) porque FastAPI recientes
        agrupan los routers incluidos en objetos internos no estables.
        """
        from fastapi import APIRouter

        rutas = [r for r in main_mod.app.routes if isinstance(r, APIRoute)]
        vistos = set()
        for nombre, modulo in list(sys.modules.items()):
            archivo = getattr(modulo, "__file__", "") or ""
            if not archivo.startswith(str(ROOT)) or "/tests/" in archivo:
                continue
            router = getattr(modulo, "router", None)
            if isinstance(router, APIRouter) and id(router) not in vistos:
                vistos.add(id(router))
                rutas += [r for r in router.routes if isinstance(r, APIRoute)]
        return [
            r
            for r in rutas
            if r.path.startswith("/api/") and main_mod._is_public_path(r.path)
        ]

    def test_toda_ruta_api_publica_tiene_autenticacion_propia(self):
        rutas = self._rutas_publicas_api()
        paths = {r.path for r in rutas}
        for esperado in (
            "/api/bot/liquidar",
            "/api/bot/acuerdo",
            "/api/bot/recordatorios/pendientes",
            "/api/recaudos/bot/abono",
        ):
            self.assertIn(esperado, paths)
        for ruta in rutas:
            if ruta.path in AUTH_PROPIA:
                continue
            deps = _dependencias(ruta)
            if ruta.path.startswith("/api/grok/"):
                esperado = grok_api._require_grok_key
                self.assertEqual(ruta.methods, {"GET"}, f"{ruta.path} debe ser solo GET")
            elif ruta.path.startswith("/api/recaudos/bot/"):
                esperado = api_recaudos._require_bot_auth
            elif ruta.path.startswith("/api/bot/"):
                esperado = bot_api._require_api_key
            else:
                self.fail(f"Ruta pública inesperada sin autenticación declarada: {ruta.path}")
            self.assertIn(
                esperado, deps, f"{ruta.methods} {ruta.path} pública sin llave M2M"
            )

    def test_prefijos_publicos_solo_los_esperados(self):
        api_publicos = tuple(p for p in permisos.PUBLIC_PREFIXES if p.startswith("/api/"))
        self.assertEqual(
            api_publicos,
            ("/api/bot/", "/api/recaudos/bot/", "/api/grok/", "/api/v1/cron/"),
        )

    def test_rutas_recaudos_no_bot_siguen_con_sesion(self):
        for path in (
            "/api/recaudos/solicitudes/pendientes",
            "/api/recaudos/solicitudes/1/aprobar",
            "/api/recaudos/informes/exportar-excel",
            "/api/recaudos/botx/abono",
            "/api/recaudos/bot",
            "/api/recaudos",
        ):
            self.assertFalse(main_mod._is_public_path(path), path)
        self.assertTrue(main_mod._is_public_path("/api/recaudos/bot/abono"))

    def test_rbac_cubre_resto_de_api_recaudos(self):
        self.assertEqual(
            permisos.permiso_requerido_para_ruta("GET", "/api/recaudos/solicitudes/pendientes"),
            permisos.NAV_INFORMES,
        )
        self.assertTrue(
            permisos.denegar_acceso(
                permisos.permisos_de_perfil(permisos.PERFIL_CLIENTE_PH),
                "GET",
                "/api/recaudos/informes/exportar-excel",
            )
        )
        self.assertFalse(
            permisos.denegar_acceso(
                permisos.permisos_de_perfil(permisos.PERFIL_ADMIN),
                "GET",
                "/api/recaudos/informes/exportar-excel",
            )
        )


class _LimitadorLimpio(unittest.TestCase):
    """El limitador de main es global al proceso: se reinicia entre pruebas."""

    def setUp(self):
        rate_limit.limiter.reset()
        self.addCleanup(rate_limit.limiter.reset)


class AbonoBotHttpTests(_LimitadorLimpio):
    URL = "/api/recaudos/bot/abono"

    def setUp(self):
        super().setUp()
        self.client = TestClient(main_mod.app)

    def _post(self, key=None, cookies=None):
        headers = {} if key is None else {"X-API-Key": key}
        return self.client.post(self.URL, json={}, headers=headers, cookies=cookies or {})

    def test_sin_llave_401(self):
        with _env(RECAUDOS_API_KEY=NEW_REC, LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(self._post().status_code, 401)
            self.assertEqual(self._post("").status_code, 401)

    def test_llave_mala_401(self):
        with _env(RECAUDOS_API_KEY=NEW_REC, LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(self._post("mala").status_code, 401)

    def test_llave_nueva_pasa_autenticacion(self):
        with _env(RECAUDOS_API_KEY=NEW_REC, LIQUIDADOR_API_KEY=OLD):
            resp = self._post(NEW_REC)
        self.assertEqual(resp.status_code, 422)

    def test_llave_vieja_pasa_mientras_este_habilitada(self):
        with _env(RECAUDOS_API_KEY=NEW_REC, LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(self._post(OLD).status_code, 422)
        with _env(RECAUDOS_API_KEY=NEW_REC, LIQUIDADOR_API_KEY=OLD, M2M_LEGACY_KEY_ENABLED="false"):
            self.assertEqual(self._post(OLD).status_code, 401)
            self.assertEqual(self._post(NEW_REC).status_code, 422)

    def test_llave_de_bot_no_sirve_en_recaudos(self):
        with _env(RECAUDOS_API_KEY=NEW_REC, BOT_API_KEY=NEW_BOT):
            self.assertEqual(self._post(NEW_BOT).status_code, 401)

    def test_sin_llaves_configuradas_503(self):
        with _env():
            self.assertEqual(self._post("x").status_code, 503)
            self.assertEqual(self._post("").status_code, 503)

    def test_sesion_de_usuario_sin_llave_no_sirve(self):
        def _ctx(request, uid):
            request.state.user_id = "1"
            request.state.perfil_codigo = permisos.PERFIL_ADMIN
            request.state.permisos = permisos.permisos_de_perfil(permisos.PERFIL_ADMIN)
            return None

        with _env(RECAUDOS_API_KEY=NEW_REC, LIQUIDADOR_API_KEY=OLD), mock.patch(
            "main.verify_session", return_value="1"
        ), mock.patch("main._aplicar_contexto_permisos", side_effect=_ctx):
            resp = self._post(cookies={"token_erp": "v1.cualquiera"})
        self.assertEqual(resp.status_code, 401)

    def test_otras_rutas_recaudos_exigen_sesion(self):
        resp = self.client.get(
            "/api/recaudos/solicitudes/pendientes",
            headers={"X-API-Key": NEW_REC},
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertIn("/login", resp.headers.get("location", ""))

    def test_cliente_ph_no_accede_a_recaudos_internos(self):
        def _ctx(request, uid):
            request.state.user_id = "9"
            request.state.perfil_codigo = permisos.PERFIL_CLIENTE_PH
            request.state.permisos = permisos.permisos_de_perfil(permisos.PERFIL_CLIENTE_PH)
            return None

        with mock.patch("main.verify_session", return_value="9"), mock.patch(
            "main._aplicar_contexto_permisos", side_effect=_ctx
        ):
            resp = self.client.get(
                "/api/recaudos/solicitudes/pendientes", follow_redirects=False
            )
        self.assertEqual(resp.status_code, 403)


class RutasBotHttpTests(_LimitadorLimpio):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main_mod.app)

    def test_endpoints_bot_exigen_llave_antes_de_validar_cuerpo(self):
        casos = (
            ("post", "/api/bot/liquidar"),
            ("post", "/api/bot/acuerdo"),
            ("get", "/api/bot/recordatorios/pendientes"),
        )
        with _env(BOT_API_KEY=NEW_BOT, LIQUIDADOR_API_KEY=OLD):
            for metodo, path in casos:
                kwargs = {"json": {}} if metodo == "post" else {}
                sin = getattr(self.client, metodo)(path, **kwargs)
                mala = getattr(self.client, metodo)(path, headers={"X-API-Key": "mala"}, **kwargs)
                self.assertEqual(sin.status_code, 401, path)
                self.assertEqual(mala.status_code, 401, path)

    def test_liquidar_con_llave_nueva_y_vieja(self):
        with _env(BOT_API_KEY=NEW_BOT, LIQUIDADOR_API_KEY=OLD):
            for key in (NEW_BOT, OLD):
                resp = self.client.post(
                    "/api/bot/liquidar", json={}, headers={"X-API-Key": key}
                )
                self.assertEqual(resp.status_code, 422)


class M2MSinCookieNiOriginTests(_LimitadorLimpio):
    """El bot no envía cookie ni Origin: autentica solo por llave y no cae en CSRF/login."""

    CASOS = (
        ("/api/bot/liquidar", "BOT_API_KEY", NEW_BOT),
        ("/api/recaudos/bot/abono", "RECAUDOS_API_KEY", NEW_REC),
    )

    def setUp(self):
        super().setUp()
        self.client = TestClient(main_mod.app)

    def test_llave_valida_sin_cookie_ni_origin(self):
        for path, var, valor in self.CASOS:
            with _env(**{var: valor}):
                self.assertFalse(self.client.cookies)
                resp = self.client.post(path, json={}, headers={"X-API-Key": valor})
                self.assertEqual(resp.status_code, 422, path)
                self.assertNotIn("origen no permitido", resp.text)

    def test_sin_llave_401_sin_redirect_a_login(self):
        for path, var, valor in self.CASOS:
            with _env(**{var: valor}):
                resp = self.client.post(path, json={}, follow_redirects=False)
                self.assertEqual(resp.status_code, 401, path)
                self.assertNotIn("location", resp.headers)

    def test_llave_legacy_sin_cookie_ni_origin(self):
        for path, var, valor in self.CASOS:
            with _env(LIQUIDADOR_API_KEY=OLD, **{var: valor}):
                resp = self.client.post(path, json={}, headers={"X-API-Key": OLD})
                self.assertEqual(resp.status_code, 422, path)


class LimiteDeIntentosM2MTests(_LimitadorLimpio):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main_mod.app)

    def test_fallos_repetidos_bloquean_con_429(self):
        for path, var, valor in M2MSinCookieNiOriginTests.CASOS:
            rate_limit.limiter.reset()
            with _env(**{var: valor}):
                codigos = [
                    self.client.post(path, json={}, headers={"X-API-Key": "mala"}).status_code
                    for _ in range(rate_limit.API_FAIL_LIMIT + 2)
                ]
                self.assertEqual(codigos[0], 401)
                self.assertIn(429, codigos, path)
                bloqueada = self.client.post(path, json={}, headers={"X-API-Key": valor})
                self.assertEqual(bloqueada.status_code, 429)
                self.assertIn("retry-after", bloqueada.headers)

    def test_llave_valida_no_cuenta_como_fallo(self):
        for path, var, valor in M2MSinCookieNiOriginTests.CASOS:
            rate_limit.limiter.reset()
            with _env(**{var: valor}):
                for _ in range(rate_limit.API_FAIL_LIMIT + 5):
                    resp = self.client.post(path, json={}, headers={"X-API-Key": valor})
                    self.assertEqual(resp.status_code, 422, path)

    def test_prefijos_m2m_estan_bajo_el_limite_estricto(self):
        for path in ("/api/bot/liquidar", "/api/recaudos/bot/abono"):
            self.assertTrue(path.startswith(rate_limit.API_KEY_PREFIXES), path)


if __name__ == "__main__":
    unittest.main()
