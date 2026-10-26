"""Endurecimiento de acceso: fail-closed, default-deny RBAC, sesión, rate limit y CSRF."""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

import csrf_guard
import permisos
import rate_limit
import security
import session_store
import usuarios_service


def _iter_routes(routes):
    """Aplana rutas de la app (incluye routers incluidos y mounts) entre versiones de FastAPI."""
    for route in routes:
        sub = getattr(route, "original_router", None)
        if sub is not None:
            yield from _iter_routes(sub.routes)
            continue
        yield route


def _ejemplo(path: str) -> str:
    import re

    path = re.sub(r"\{[^}:]+:path\}", "x/y", path)
    return re.sub(r"\{[^}]+\}", "x", path)


def _todas_las_rutas():
    import agenda_service  # noqa: F401  (registra rutas de agenda al importarse)
    import main

    resultado = []
    for route in _iter_routes(main.app.routes):
        path = getattr(route, "path", None)
        if path is None:
            continue
        metodos = getattr(route, "methods", None)
        if not metodos:
            resultado.append(("GET", _ejemplo(path.rstrip("/") + "/x")))
            continue
        for metodo in sorted(metodos):
            if metodo in ("HEAD", "OPTIONS"):
                continue
            resultado.append((metodo, _ejemplo(path)))
    return resultado


class ClasificacionDeRutasTests(unittest.TestCase):
    def test_toda_ruta_de_la_app_esta_clasificada(self):
        rutas = _todas_las_rutas()
        self.assertGreater(len(rutas), 60, "la enumeración de rutas parece incompleta")
        paths = {p for _, p in rutas}
        for esperado in ("/liquidador", "/alertas-seguimiento", "/pdfs/x", "/expediente-docs/x/y/x"):
            self.assertTrue(
                any(p.startswith(esperado.split("/x")[0]) for p in paths), esperado
            )
        sin_clasificar = [
            f"{m} {p}"
            for m, p in rutas
            if permisos.permiso_requerido_para_ruta(m, p) == permisos.PERMISO_DENEGADO
        ]
        self.assertEqual(
            sin_clasificar,
            [],
            "Rutas sin clasificar en permisos._ROUTE_RULES / rutas públicas: "
            + ", ".join(sin_clasificar),
        )

    def test_ruta_nueva_sin_regla_se_deniega_incluso_a_admin(self):
        admin = permisos.permisos_de_perfil(permisos.PERFIL_ADMIN)
        for metodo in ("GET", "POST", "DELETE"):
            self.assertTrue(permisos.denegar_acceso(admin, metodo, "/ruta-nueva-sin-regla"))
        self.assertTrue(permisos.denegar_acceso(admin, "GET", "/liquidadorx"))

    def test_lista_publica_es_pequena_y_explicita(self):
        for path in ("/", "/login", "/health", "/landing", "/static/css/a.css",
                     "/api/bot/liquidar", "/sms/api/pendientes", "/api/v1/cron/sync-rama"):
            self.assertTrue(permisos.es_ruta_publica(path), path)
        for path in ("/static/pdfs/secreto.pdf", "/liquidador", "/pdfs/a.pdf",
                     "/expediente-docs/1/a.pdf", "/alertas-seguimiento", "/api/recaudos/x",
                     "/herramientas/google-drive/oauth/callback", "/docs", "/staticx/a"):
            self.assertFalse(permisos.es_ruta_publica(path), path)


# (método, ruta) que cada perfil debe poder usar / no poder usar.
_LECTURA_OPERATIVA = [
    ("GET", "/dashboard"),
    ("GET", "/expedientes"),
    ("GET", "/expediente/EXP-0001"),
    ("GET", "/informes"),
    ("GET", "/contactos"),
    ("GET", "/conjuntos"),
    ("GET", "/crm"),
    ("GET", "/acuerdos"),
    ("GET", "/vencimientos"),
    ("GET", "/liquidador"),
    ("GET", "/liquidador/plantilla"),
    ("GET", "/alertas-seguimiento"),
    ("GET", "/pdfs/a.pdf"),
    ("GET", "/expediente-docs/EXP-0001/a.pdf"),
    ("GET", "/descargar-excel"),
    ("GET", "/herramientas/estado-cuenta"),
    ("GET", "/api/recaudos/informes/exportar-excel"),
    ("GET", "/phasecob"),
]
_ESCRITURA = [
    ("POST", "/liquidador"),
    ("POST", "/alertas-seguimiento/3/leer"),
    ("POST", "/actuacion/nueva"),
    ("POST", "/crear_expediente_completo"),
    ("POST", "/crm/guardar"),
    ("POST", "/contactos/guardar"),
]
_SOLO_ADMIN = [
    ("GET", "/admin/usuarios"),
    ("POST", "/admin/usuarios/crear"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("GET", "/__interno/agenda-log.xlsx"),
]


class MatrizPorPerfilTests(unittest.TestCase):
    def _permitido(self, perfil, metodo, path):
        return not permisos.denegar_acceso(permisos.permisos_de_perfil(perfil), metodo, path)

    def test_admin_puede_todo_menos_portal_ph(self):
        for m, p in _LECTURA_OPERATIVA + _ESCRITURA + _SOLO_ADMIN:
            self.assertTrue(self._permitido("ADMIN", m, p), (m, p))
        self.assertTrue(self._permitido("ADMIN", "POST", "/acuerdos/eliminar"))
        self.assertTrue(self._permitido("ADMIN", "GET", "/supervision-agente"))
        self.assertTrue(self._permitido("ADMIN", "GET", "/procesos"))

    def test_abogado_conserva_todo_salvo_administracion(self):
        for m, p in _LECTURA_OPERATIVA + _ESCRITURA:
            self.assertTrue(self._permitido("ABOGADO", m, p), (m, p))
        for m, p in _SOLO_ADMIN:
            self.assertFalse(self._permitido("ABOGADO", m, p), (m, p))
        self.assertTrue(self._permitido("ABOGADO", "GET", "/supervision-agente"))
        self.assertTrue(self._permitido("ABOGADO", "POST", "/sms/generar-cola"))

    def test_auxiliar_cobro_conserva_su_flujo_diario(self):
        for m, p in _LECTURA_OPERATIVA + _ESCRITURA:
            self.assertTrue(self._permitido("AUXILIAR_COBRO", m, p), (m, p))
        self.assertTrue(self._permitido("AUXILIAR_COBRO", "GET", "/sms"))
        self.assertTrue(self._permitido("AUXILIAR_COBRO", "POST", "/cartas-cobro/generar"))
        for m, p in _SOLO_ADMIN + [
            ("GET", "/supervision-agente"),
            ("GET", "/procesos"),
            ("POST", "/acuerdos/eliminar"),
        ]:
            self.assertFalse(self._permitido("AUXILIAR_COBRO", m, p), (m, p))

    def test_consulta_solo_lee(self):
        for m, p in _LECTURA_OPERATIVA:
            self.assertTrue(self._permitido("CONSULTA", m, p), (m, p))
        for m, p in _ESCRITURA + _SOLO_ADMIN + [("GET", "/sms"), ("GET", "/cartas-cobro")]:
            self.assertFalse(self._permitido("CONSULTA", m, p), (m, p))

    def test_cliente_ph_solo_portal(self):
        self.assertTrue(self._permitido("CLIENTE_PH", "GET", "/portal-ph"))
        self.assertTrue(self._permitido("CLIENTE_PH", "GET", "/logout"))
        self.assertTrue(self._permitido("CLIENTE_PH", "GET", "/static/css/app.css"))
        for m, p in _LECTURA_OPERATIVA + _ESCRITURA + _SOLO_ADMIN + [
            ("GET", "/sms"), ("GET", "/supervision-agente"), ("POST", "/portal-ph"),
        ]:
            self.assertFalse(self._permitido("CLIENTE_PH", m, p), (m, p))

    def test_perfil_bloqueado_no_accede_a_nada_autenticado(self):
        for perfil in (None, "", "BASURA", permisos.PERFIL_SIN_ACCESO):
            perms = permisos.permisos_de_perfil(perfil)
            self.assertEqual(perms, frozenset())
            for m, p in _LECTURA_OPERATIVA + _ESCRITURA + _SOLO_ADMIN + [("GET", "/portal-ph")]:
                self.assertTrue(permisos.denegar_acceso(perms, m, p), (perfil, m, p))

    def test_cada_perfil_humano_tiene_home_permitido(self):
        for perfil in permisos.PERFILES_HUMANOS:
            home = permisos.home_path_para_perfil(perfil)
            self.assertTrue(self._permitido(perfil, "GET", home), (perfil, home))


class FailClosedPerfilTests(unittest.TestCase):
    def test_normalizar_perfil_nunca_devuelve_admin_por_defecto(self):
        for valor in (None, "", "   ", "xyz", "ADMINISTRADOR", 0):
            self.assertEqual(permisos.normalizar_perfil(valor), permisos.PERFIL_SIN_ACCESO)

    def test_perfiles_validos_se_normalizan(self):
        for perfil in permisos.PERFILES_HUMANOS:
            self.assertEqual(permisos.normalizar_perfil(perfil.lower()), perfil)

    def test_usuario_inexistente_queda_bloqueado(self):
        with patch.object(usuarios_service, "obtener_usuario_por_id", return_value=None):
            ctx = usuarios_service.contexto_auth_usuario("99")
        self.assertFalse(ctx["activo"])
        self.assertTrue(ctx["bloqueado"])
        self.assertEqual(ctx["permisos"], frozenset())
        self.assertNotEqual(ctx["perfil_codigo"], permisos.PERFIL_ADMIN)

    def test_usuario_sin_perfil_o_perfil_basura_queda_bloqueado(self):
        for codigo in (None, "", "BASURA"):
            with patch.object(
                usuarios_service,
                "obtener_usuario_por_id",
                return_value={"id": 1, "nombre": "A", "email": "a@x.com", "activo": True,
                              "perfil_codigo": codigo},
            ):
                ctx = usuarios_service.contexto_auth_usuario("1")
            self.assertTrue(ctx["bloqueado"], codigo)
            self.assertFalse(ctx["activo"])
            self.assertEqual(ctx["permisos"], frozenset())

    def test_error_de_bd_no_concede_acceso(self):
        with patch.object(usuarios_service, "obtener_usuario_por_id", side_effect=OSError("neon")):
            with self.assertRaises(usuarios_service.ContextoAuthNoDisponible):
                usuarios_service.contexto_auth_usuario("1")

    def test_perfil_valido_activo_mantiene_permisos(self):
        with patch.object(
            usuarios_service,
            "obtener_usuario_por_id",
            return_value={"id": 5, "nombre": "G", "email": "g@x.com", "activo": True,
                          "perfil_codigo": "CLIENTE_PH", "perfil_nombre": "Cliente PH"},
        ):
            ctx = usuarios_service.contexto_auth_usuario("5")
        self.assertTrue(ctx["activo"])
        self.assertFalse(ctx["bloqueado"])
        self.assertEqual(ctx["permisos"], permisos.permisos_de_perfil("CLIENTE_PH"))


class SesionTests(unittest.TestCase):
    def test_roundtrip_y_campos(self):
        token = security.sign_session("42")
        data = security.parse_session(token)
        self.assertEqual(data.user_id, "42")
        self.assertEqual(len(data.sid), 32)
        self.assertEqual(security.verify_session(token), "42")
        self.assertLessEqual(data.expires_at - data.issued_at, security.SESSION_TTL_MAX)

    def test_cada_sesion_tiene_sid_distinto(self):
        a = security.parse_session(security.sign_session("1"))
        b = security.parse_session(security.sign_session("1"))
        self.assertNotEqual(a.sid, b.sid)

    def test_firma_alterada_o_basura_se_rechaza(self):
        token = security.sign_session("7")
        version, payload, firma = token.split(".")
        otro = security.sign_session("8").split(".")[1]
        for malo in (
            f"{version}.{otro}.{firma}",
            f"{version}.{payload}.{firma[:-2]}AA",
            f"v1.{payload}.{firma}",
            "", "x", "v2..", "v2.a.b", None, "a" * 2000,
        ):
            self.assertIsNone(security.verify_session(malo), malo)

    def test_token_v1_legado_ya_no_es_valido(self):
        import base64
        import hashlib
        import hmac

        payload = f"7.{int(time.time()) + 600}".encode()
        firma = hmac.new(security.SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
        self.assertIsNone(security.verify_session(f"v1.{b64(payload)}.{b64(firma)}"))

    def test_expirada_y_vigencia_excesiva(self):
        ahora = int(time.time())
        self.assertIsNone(security.verify_session(security.sign_session("1", expires_at=ahora - 5)))
        largo = security.sign_session("1", expires_at=ahora + security.SESSION_TTL_MAX + 3600)
        self.assertIsNone(security.verify_session(largo))
        futuro = security.sign_session("1", issued_at=ahora + 3600, expires_at=ahora + 7200)
        self.assertIsNone(security.verify_session(futuro))

    def test_rotar_secreto_invalida_y_un_nuevo_login_basta(self):
        token = security.sign_session("9")
        with patch.object(security, "SESSION_SECRET", "x" * 40):
            self.assertIsNone(security.verify_session(token))
            nuevo = security.sign_session("9")
            self.assertEqual(security.verify_session(nuevo), "9")

    def test_ttl_acotado_y_comparaciones_en_tiempo_constante(self):
        self.assertGreaterEqual(security.SESSION_TTL, security.SESSION_TTL_MIN)
        self.assertLessEqual(security.SESSION_TTL, security.SESSION_TTL_MAX)
        fuente = (ROOT / "security.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(fuente.count("compare_digest"), 2)

    def test_flags_de_cookie(self):
        from fastapi.responses import RedirectResponse

        resp = RedirectResponse("/x")
        security.set_session_cookie(resp, "3")
        cookie = resp.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age="):
            self.assertIn(flag, cookie)
        max_age = int(cookie.split("max-age=")[1].split(";")[0])
        self.assertLessEqual(max_age, security.SESSION_TTL_MAX)

        resp = RedirectResponse("/x")
        security.clear_session_cookie(resp)
        borrado = resp.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=lax", "max-age=0"):
            self.assertIn(flag, borrado)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        session_store.reset_cache_for_tests()
        self.filas: dict[str, float] = {}

        def fake_ejecutar(sql, params, fetch=False):
            if sql.lstrip().startswith("INSERT"):
                self.filas[params[0]] = params[2]
            if sql.lstrip().startswith("SELECT"):
                return (1,) if params[0] in self.filas else None
            return None

        self.p = patch.object(session_store, "_ejecutar", side_effect=fake_ejecutar)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        session_store.reset_cache_for_tests()

    def test_revocar_invalida_inmediatamente(self):
        sid = security.new_session_id()
        self.assertFalse(session_store.esta_revocada(sid))
        self.assertTrue(session_store.revocar(sid, "1", time.time() + 100))
        self.assertTrue(session_store.esta_revocada(sid))
        self.assertIn(sid, self.filas)

    def test_otra_instancia_ve_la_revocacion_tras_expirar_cache(self):
        sid = security.new_session_id()
        self.assertFalse(session_store.esta_revocada(sid))
        self.filas[sid] = 1.0
        session_store.reset_cache_for_tests()
        self.assertTrue(session_store.esta_revocada(sid))

    def test_bd_caida_no_acepta_la_sesion(self):
        self.p.stop()
        with patch.object(session_store, "_ejecutar", side_effect=OSError("neon")):
            with self.assertRaises(session_store.SesionNoVerificable):
                session_store.esta_revocada(security.new_session_id())
        self.p.start()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class RateLimitTests(unittest.TestCase):
    def test_login_bloquea_por_usuario_y_se_recupera(self):
        reloj = FakeClock()
        lim = rate_limit.AccessLimiter(clock=reloj)
        for _ in range(rate_limit.LOGIN_PAIR_FAILS):
            self.assertEqual(lim.login_blocked_for("1.1.1.1", "a@x.com"), 0)
            lim.login_failure("1.1.1.1", "a@x.com")
        espera = lim.login_blocked_for("1.1.1.1", "a@x.com")
        self.assertGreater(espera, 0)
        self.assertEqual(lim.login_blocked_for("9.9.9.9", "otro@x.com"), 0)
        reloj.advance(espera + 1)
        self.assertEqual(lim.login_blocked_for("1.1.1.1", "a@x.com"), 0)

    def test_retroceso_exponencial_con_tope(self):
        reloj = FakeClock()
        lim = rate_limit.FailureLimiter(2, window=900, base=60, max_lock=300, clock=reloj)
        duraciones = []
        for _ in range(5):
            lim.failure("k")
            d = lim.failure("k")
            duraciones.append(d)
            reloj.advance(d + 1)
        self.assertEqual(duraciones, [60, 120, 240, 300, 300])

    def test_login_por_ip_y_por_usuario_independientes(self):
        reloj = FakeClock()
        lim = rate_limit.AccessLimiter(clock=reloj)
        for i in range(rate_limit.LOGIN_IP_FAILS):
            lim.login_failure("2.2.2.2", f"u{i}@x.com")
        self.assertGreater(lim.login_blocked_for("2.2.2.2", "nuevo@x.com"), 0)
        self.assertEqual(lim.login_blocked_for("3.3.3.3", "nuevo@x.com"), 0)
        lim2 = rate_limit.AccessLimiter(clock=reloj)
        for i in range(rate_limit.LOGIN_USER_FAILS):
            lim2.login_failure(f"10.0.0.{i}", "victima@x.com")
        self.assertGreater(lim2.login_blocked_for("200.1.1.1", "Victima@X.com"), 0)

    def test_exito_reinicia_contadores_de_usuario(self):
        lim = rate_limit.AccessLimiter(clock=FakeClock())
        for _ in range(rate_limit.LOGIN_PAIR_FAILS - 1):
            lim.login_failure("1.1.1.1", "a@x.com")
        lim.login_success("1.1.1.1", "a@x.com")
        lim.login_failure("1.1.1.1", "a@x.com")
        self.assertEqual(lim.login_blocked_for("1.1.1.1", "a@x.com"), 0)

    def test_throttle_global_y_recuperacion(self):
        reloj = FakeClock()
        lim = rate_limit.AccessLimiter(clock=reloj)
        lim.global_window = rate_limit.SlidingWindow(5, 60, reloj)
        for _ in range(5):
            self.assertEqual(lim.check_request("4.4.4.4", "/dashboard"), 0)
        espera = lim.check_request("4.4.4.4", "/dashboard")
        self.assertGreater(espera, 0)
        self.assertEqual(lim.check_request("5.5.5.5", "/dashboard"), 0)
        self.assertEqual(lim.check_request("4.4.4.4", "/health"), 0)
        self.assertEqual(lim.check_request("4.4.4.4", "/static/a.css"), 0)
        reloj.advance(espera + 1)
        self.assertEqual(lim.check_request("4.4.4.4", "/dashboard"), 0)

    def test_endpoints_api_key_tienen_limite_mas_estricto_y_castigan_fallos(self):
        reloj = FakeClock()
        lim = rate_limit.AccessLimiter(clock=reloj)
        lim.api_window = rate_limit.SlidingWindow(3, 60, reloj)
        for _ in range(3):
            self.assertEqual(lim.check_request("6.6.6.6", "/api/bot/liquidar"), 0)
        self.assertGreater(lim.check_request("6.6.6.6", "/api/bot/liquidar"), 0)

        lim = rate_limit.AccessLimiter(clock=reloj)
        for _ in range(rate_limit.API_FAIL_LIMIT):
            lim.record_api_response("7.7.7.7", "/api/bot/liquidar", 401)
        self.assertGreater(lim.check_request("7.7.7.7", "/api/bot/liquidar"), 0)
        lim.record_api_response("8.8.8.8", "/dashboard", 401)
        self.assertEqual(lim.check_request("8.8.8.8", "/api/bot/liquidar"), 0)

    def test_ip_cliente_no_confia_en_xff_falsificado(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ERP_TRUSTED_PROXY_COUNT", None)
            h = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
            self.assertEqual(rate_limit.client_ip(h, "10.0.0.5"), "10.0.0.5")
            self.assertEqual(rate_limit.client_ip(h, "8.8.8.8"), "8.8.8.8")

    def test_ip_cliente_usa_cf_connecting_ip_solo_desde_el_proxy(self):
        h = {"cf-connecting-ip": "198.51.100.7", "x-forwarded-for": "1.1.1.1, 198.51.100.7"}
        self.assertEqual(rate_limit.client_ip(h, "10.1.2.3"), "198.51.100.7")
        self.assertEqual(rate_limit.client_ip(h, "8.8.8.8"), "8.8.8.8")
        self.assertEqual(rate_limit.client_ip({"cf-connecting-ip": "no-es-ip"}, "10.1.2.3"), "10.1.2.3")

    def test_ip_cliente_xff_contando_proxies_desde_la_derecha(self):
        h = {"x-forwarded-for": "6.6.6.6, 198.51.100.7, 10.9.9.9"}
        self.assertEqual(rate_limit.client_ip(h, "10.0.0.1", trusted_proxies=2), "198.51.100.7")
        self.assertEqual(rate_limit.client_ip(h, "10.0.0.1", trusted_proxies=9), "10.0.0.1")


class CsrfGuardTests(unittest.TestCase):
    def test_origen_distinto_se_rechaza(self):
        h = {"host": "erp.example.com", "origin": "https://evil.example"}
        self.assertFalse(csrf_guard.origen_permitido(h))
        self.assertFalse(csrf_guard.origen_permitido({"host": "erp.example.com", "origin": "null"}))

    def test_mismo_origen_se_acepta(self):
        h = {"host": "erp.example.com", "origin": "https://erp.example.com"}
        self.assertTrue(csrf_guard.origen_permitido(h))
        h = {"host": "erp.example.com", "referer": "https://erp.example.com/crm?x=1"}
        self.assertTrue(csrf_guard.origen_permitido(h))

    def test_referer_ajeno_y_fetch_metadata(self):
        self.assertFalse(csrf_guard.origen_permitido(
            {"host": "erp.example.com", "referer": "https://evil.example/p"}))
        self.assertFalse(csrf_guard.origen_permitido(
            {"host": "erp.example.com", "sec-fetch-site": "cross-site"}))
        self.assertFalse(csrf_guard.origen_permitido(
            {"host": "erp.example.com", "sec-fetch-site": "same-site"}))
        self.assertTrue(csrf_guard.origen_permitido(
            {"host": "erp.example.com", "sec-fetch-site": "same-origin"}))

    def test_cliente_sin_cabeceras_de_navegador_se_acepta(self):
        self.assertTrue(csrf_guard.origen_permitido({"host": "erp.example.com"}))

    def test_public_base_url_y_lista_extra(self):
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://gestionlegal.co",
                                     "ERP_ALLOWED_ORIGINS": "https://app.otro.co"}):
            self.assertTrue(csrf_guard.origen_permitido(
                {"host": "interno:10000", "origin": "https://gestionlegal.co"}))
            self.assertTrue(csrf_guard.origen_permitido(
                {"host": "interno:10000", "origin": "https://app.otro.co"}))


class MiddlewareIntegracionTests(unittest.TestCase):
    """Middleware real sobre main.app con BD simulada."""

    def setUp(self):
        import main
        from fastapi.testclient import TestClient

        self.main = main
        rate_limit.limiter.reset()
        session_store.reset_cache_for_tests()
        self.revocadas: set[str] = set()
        self.revocaciones: list[str] = []

        def fake_revocar(sid, user_id, expira):
            self.revocadas.add(sid)
            self.revocaciones.append(sid)
            return True

        patches = [
            patch.object(session_store, "esta_revocada", side_effect=lambda sid: sid in self.revocadas),
            patch.object(session_store, "revocar", side_effect=fake_revocar),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app, follow_redirects=False)
        self.addCleanup(rate_limit.limiter.reset)

    def _usuario(self, perfil, activo=True, uid="1"):
        return {"id": int(uid), "nombre": "Test", "email": "t@x.com", "activo": activo,
                "perfil_codigo": perfil, "perfil_nombre": perfil, "password": "h"}

    def _get(self, path, perfil="ADMIN", usuario="auto", token=None, method="GET", **kw):
        token = token or security.sign_session("1")
        self.client.cookies.set(security.SESSION_COOKIE, token)
        retorno = self._usuario(perfil) if usuario == "auto" else usuario
        with patch.object(usuarios_service, "obtener_usuario_por_id", return_value=retorno):
            return self.client.request(method, path, **kw)

    def test_sin_cookie_redirige_a_login(self):
        self.client.cookies.clear()
        resp = self.client.get("/liquidador")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_cliente_ph_no_accede_a_rutas_antes_sin_regla(self):
        for path in ("/liquidador", "/alertas-seguimiento", "/pdfs/a.pdf",
                     "/expediente-docs/EXP-1/a.pdf", "/descargar-excel", "/docs"):
            resp = self._get(path, perfil="CLIENTE_PH")
            self.assertEqual(resp.status_code, 303, path)
            self.assertIn("/portal-ph", resp.headers["location"], path)

    def test_ruta_sin_regla_se_deniega_a_admin(self):
        resp = self._get("/ruta-que-no-existe", perfil="ADMIN")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/dashboard", resp.headers["location"])

    def test_admin_ve_docs_y_abogado_no(self):
        self.assertEqual(self._get("/docs", perfil="ADMIN").status_code, 200)
        resp = self._get("/docs", perfil="ABOGADO")
        self.assertEqual(resp.status_code, 303)

    def test_usuario_inexistente_cierra_sesion(self):
        resp = self._get("/dashboard", usuario=None)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")
        self.assertIn("max-age=0", resp.headers["set-cookie"].lower())

    def test_perfil_ausente_o_invalido_queda_bloqueado(self):
        for perfil in (None, "", "BASURA"):
            resp = self._get("/dashboard", perfil=perfil)
            self.assertEqual(resp.status_code, 303, perfil)
            self.assertEqual(resp.headers["location"], "/login", perfil)

    def test_usuario_inactivo_cierra_sesion(self):
        resp = self._get("/dashboard", usuario=self._usuario("ADMIN", activo=False))
        self.assertEqual(resp.headers["location"], "/login")

    def test_error_de_bd_responde_503_sin_dar_acceso(self):
        self.client.cookies.set(security.SESSION_COOKIE, security.sign_session("1"))
        with patch.object(usuarios_service, "obtener_usuario_por_id", side_effect=OSError("neon")):
            resp = self.client.get("/docs")
        self.assertEqual(resp.status_code, 503)

    def test_sesion_revocada_se_rechaza(self):
        token = security.sign_session("1")
        self.revocadas.add(security.session_id_from_token(token))
        resp = self._get("/docs", token=token)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_logout_revoca_y_borra_cookie(self):
        token = security.sign_session("1")
        self.client.cookies.set(security.SESSION_COOKIE, token)
        resp = self.client.get("/logout")
        self.assertEqual(resp.status_code, 303)
        self.assertIn(security.session_id_from_token(token), self.revocaciones)
        self.assertIn("max-age=0", resp.headers["set-cookie"].lower())
        self.assertEqual(self._get("/docs", token=token).headers["location"], "/login")

    def _login(self, password_ok=True, perfil="ADMIN", **kw):
        with patch.object(
            usuarios_service, "obtener_usuario_por_email", return_value=self._usuario(perfil)
        ), patch.object(self.main, "verify_password", return_value=password_ok):
            return self.client.post(
                "/login", data={"email": "t@x.com", "password": "x"}, **kw
            )

    def test_login_emite_cookie_endurecida_y_sid_nuevo(self):
        self.client.cookies.clear()
        r1 = self._login()
        self.assertEqual(r1.status_code, 303)
        cookie = r1.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age="):
            self.assertIn(flag, cookie)
        t1 = r1.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        self.client.cookies.clear()
        r2 = self._login()
        t2 = r2.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        self.assertNotEqual(security.session_id_from_token(t1), security.session_id_from_token(t2))

    def test_login_con_cookie_previa_la_revoca(self):
        previa = security.sign_session("1")
        self.client.cookies.set(security.SESSION_COOKIE, previa)
        self._login()
        self.assertIn(security.session_id_from_token(previa), self.revocaciones)

    def test_login_perfil_cliente_va_a_portal(self):
        self.client.cookies.clear()
        resp = self._login(perfil="CLIENTE_PH")
        self.assertEqual(resp.headers["location"], "/portal-ph")

    def test_login_sin_perfil_no_inicia_sesion(self):
        self.client.cookies.clear()
        for perfil in (None, "BASURA"):
            resp = self._login(perfil=perfil)
            self.assertEqual(resp.status_code, 403)
            self.assertNotIn("set-cookie", resp.headers)

    def test_login_bloquea_tras_intentos_fallidos_incluso_con_clave_correcta(self):
        self.client.cookies.clear()
        for _ in range(rate_limit.LOGIN_PAIR_FAILS):
            self.assertEqual(self._login(password_ok=False).status_code, 401)
        bloqueado = self._login(password_ok=True)
        self.assertEqual(bloqueado.status_code, 429)
        self.assertIn("retry-after", bloqueado.headers)
        self.assertNotIn("set-cookie", bloqueado.headers)
        rate_limit.limiter.reset()
        self.assertEqual(self._login(password_ok=True).status_code, 303)

    def test_login_desde_otro_origen_se_rechaza(self):
        resp = self._login(headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)
        resp = self._login(headers={"Origin": "http://testserver"})
        self.assertEqual(resp.status_code, 303)

    def test_post_autenticado_cross_site_se_rechaza(self):
        resp = self._get("/crm/guardar", method="POST",
                         headers={"Origin": "https://evil.example"}, data={})
        self.assertEqual(resp.status_code, 403)
        resp = self._get("/crm/guardar", perfil="CONSULTA", method="POST",
                         headers={"Sec-Fetch-Site": "cross-site"}, data={})
        self.assertEqual(resp.status_code, 403)

    def test_throttle_global_devuelve_429_y_se_recupera(self):
        reloj = FakeClock()
        rate_limit.limiter.global_window = rate_limit.SlidingWindow(3, 60, reloj)
        self.client.cookies.clear()
        for _ in range(3):
            self.assertEqual(self.client.get("/landing").status_code, 200)
        resp = self.client.get("/landing")
        self.assertEqual(resp.status_code, 429)
        self.assertIn("retry-after", resp.headers)
        self.assertEqual(self.client.get("/health").status_code, 200)
        reloj.advance(61)
        self.assertEqual(self.client.get("/landing").status_code, 200)

    def test_cabeceras_de_seguridad_tambien_en_redirects_y_errores(self):
        self.client.cookies.clear()
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.headers.get("x-frame-options"), "DENY")
        self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")


if __name__ == "__main__":
    unittest.main()
