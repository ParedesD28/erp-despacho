"""Llave de Grok Bot: acceso M2M con escritura en /api/bot y /api/recaudos/bot, lectura en /api/grok.

Corren sin base de datos: los servicios y el cursor se simulan.
"""
from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import grok_api
import m2m_keys
import main as main_mod
import permisos
import rate_limit
from test_auth_hardening import _todas_las_rutas

GROK = "grok-bot-key-GGGGGGGGGGGGGGGGGGGGGGG"
OLD = "old-shared-key-AAAAAAAAAAAAAAAAAAAA"
BOT = "new-bot-key-BBBBBBBBBBBBBBBBBBBBBBB"
REC = "new-recaudos-key-CCCCCCCCCCCCCCCCC"
PDF = "new-pdf-secret-DDDDDDDDDDDDDDDDDDD"
SUP = "new-supervision-key-EEEEEEEEEEEEEEE"

M2M_VARS = (
    "LIQUIDADOR_API_KEY",
    "BOT_API_KEY",
    "RECAUDOS_API_KEY",
    "BOT_PDF_PUBLIC_SECRET",
    "AGENT_SUPERVISION_KEY",
    "GROK_BOT_API_KEY",
    "M2M_LEGACY_KEY_ENABLED",
)


def _env(**values):
    base = {k: "" for k in M2M_VARS}
    base.update(values)
    return mock.patch.dict(os.environ, base)


def _rutas_grok():
    rutas = [r for r in grok_api.router.routes if isinstance(r, APIRoute)]
    assert rutas, "no se encontraron rutas /api/grok"
    return rutas


def _ejemplo(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "1", path)


class _Base(unittest.TestCase):
    def setUp(self):
        rate_limit.limiter.reset()
        self.addCleanup(rate_limit.limiter.reset)
        self.client = TestClient(main_mod.app)

    reiniciar_fallos = True

    def _sin_bloqueo(self):
        if self.reiniciar_fallos:
            rate_limit.limiter.api_failures.reset()

    def get(self, path, key=None, **kw):
        self._sin_bloqueo()
        headers = {} if key is None else {"X-API-Key": key}
        return self.client.get(path, headers=headers, follow_redirects=False, **kw)

    def post(self, path, key=None, **kw):
        self._sin_bloqueo()
        headers = {} if key is None else {"X-API-Key": key}
        kw.setdefault("json", {})
        return self.client.post(path, headers=headers, follow_redirects=False, **kw)


class LlaveGrokEnGrokApiTests(_Base):
    def test_sin_llave_401(self):
        with _env(GROK_BOT_API_KEY=GROK):
            for ruta in _rutas_grok():
                path = _ejemplo(ruta.path)
                self.assertEqual(self.get(path).status_code, 401, path)
                self.assertEqual(self.get(path, "").status_code, 401, path)

    def test_llave_de_otro_proposito_401(self):
        with _env(
            GROK_BOT_API_KEY=GROK,
            LIQUIDADOR_API_KEY=OLD,
            BOT_API_KEY=BOT,
            RECAUDOS_API_KEY=REC,
            BOT_PDF_PUBLIC_SECRET=PDF,
            AGENT_SUPERVISION_KEY=SUP,
        ):
            for otra in (OLD, BOT, REC, PDF, SUP, "mala"):
                for ruta in _rutas_grok():
                    path = _ejemplo(ruta.path)
                    self.assertEqual(self.get(path, otra).status_code, 401, (path, otra[:8]))

    def test_llave_heredada_no_abre_grok_ni_con_legacy_habilitada(self):
        with _env(LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(self.get("/api/grok/ping", OLD).status_code, 503)
        with _env(LIQUIDADOR_API_KEY=OLD, GROK_BOT_API_KEY=GROK):
            self.assertEqual(self.get("/api/grok/ping", OLD).status_code, 401)

    def test_sin_configurar_falla_cerrado_503(self):
        with _env(BOT_API_KEY=BOT, LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(self.get("/api/grok/ping", BOT).status_code, 503)
            self.assertEqual(self.get("/api/grok/ping", "").status_code, 503)

    def test_llave_correcta_200(self):
        with _env(GROK_BOT_API_KEY=GROK):
            resp = self.get("/api/grok/ping", GROK)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["consumidor"], "grok")

    def test_sin_cookie_no_redirige_a_login(self):
        with _env(GROK_BOT_API_KEY=GROK):
            resp = self.get("/api/grok/ping")
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("location", resp.headers)

    def test_solo_get_en_todas_las_rutas_grok(self):
        for ruta in _rutas_grok():
            self.assertEqual(ruta.methods, {"GET"}, ruta.path)
        with _env(GROK_BOT_API_KEY=GROK):
            for ruta in _rutas_grok():
                path = _ejemplo(ruta.path)
                for metodo in ("post", "put", "patch", "delete"):
                    self._sin_bloqueo()
                    resp = getattr(self.client, metodo)(
                        path, headers={"X-API-Key": GROK}, follow_redirects=False
                    )
                    self.assertEqual(resp.status_code, 405, (metodo, path))

    def test_codigo_de_grok_api_no_escribe(self):
        fuente = (ROOT / "grok_api.py").read_text(encoding="utf-8")
        codigo = "\n".join(
            linea for linea in fuente.splitlines()
            if not linea.lstrip().startswith(("#", '"""'))
        ).upper()
        for prohibido in ("INSERT INTO", "UPDATE ", "DELETE FROM", "ALTER TABLE", "DROP ", "TRUNCATE"):
            self.assertNotIn(prohibido, codigo.replace("UPDATED", ""), prohibido)
        for decorador in ("@router.post", "@router.put", "@router.patch", "@router.delete"):
            self.assertNotIn(decorador, fuente)


class LlaveGrokEnRoutersDeMaquinaTests(_Base):
    """La llave Grok se suma a las llaves actuales; el bot de cobranza no cambia."""

    CASOS = (
        ("post", "/api/bot/liquidar", "BOT_API_KEY", BOT),
        ("post", "/api/bot/acuerdo", "BOT_API_KEY", BOT),
        ("get", "/api/bot/recordatorios/pendientes", "BOT_API_KEY", BOT),
        ("post", "/api/recaudos/bot/abono", "RECAUDOS_API_KEY", REC),
    )

    def _llamar(self, metodo, path, key):
        self._sin_bloqueo()
        headers = {} if key is None else {"X-API-Key": key}
        kwargs = {"json": {}} if metodo == "post" else {}
        return getattr(self.client, metodo)(path, headers=headers, **kwargs)

    def test_llave_grok_autentica_en_bot_y_recaudos(self):
        with _env(GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT, RECAUDOS_API_KEY=REC):
            for metodo, path, _var, _val in self.CASOS:
                resp = self._llamar(metodo, path, GROK)
                self.assertNotIn(resp.status_code, (401, 403, 503), path)

    def test_llave_grok_sola_alcanza_si_no_hay_otras(self):
        with _env(GROK_BOT_API_KEY=GROK):
            for metodo, path, _var, valor in self.CASOS:
                self.assertNotIn(self._llamar(metodo, path, GROK).status_code, (401, 503), path)
                self.assertEqual(self._llamar(metodo, path, valor).status_code, 401, path)

    def test_llaves_actuales_siguen_funcionando(self):
        with _env(GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT, RECAUDOS_API_KEY=REC, LIQUIDADOR_API_KEY=OLD):
            for metodo, path, _var, valor in self.CASOS:
                for key in (valor, OLD):
                    self.assertNotIn(self._llamar(metodo, path, key).status_code, (401, 503), (path, key[:6]))

    def test_llave_heredada_retirada_no_se_acepta_aunque_haya_grok(self):
        with _env(
            GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT, RECAUDOS_API_KEY=REC,
            LIQUIDADOR_API_KEY=OLD, M2M_LEGACY_KEY_ENABLED="false",
        ):
            for metodo, path, _var, valor in self.CASOS:
                self.assertEqual(self._llamar(metodo, path, OLD).status_code, 401, path)
                self.assertNotIn(self._llamar(metodo, path, valor).status_code, (401, 503), path)
                self.assertNotIn(self._llamar(metodo, path, GROK).status_code, (401, 503), path)

    def test_sin_llave_y_llave_cruzada_401(self):
        with _env(GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT, RECAUDOS_API_KEY=REC):
            for metodo, path, _var, _val in self.CASOS:
                self.assertEqual(self._llamar(metodo, path, None).status_code, 401, path)
                self.assertEqual(self._llamar(metodo, path, "mala").status_code, 401, path)
            self.assertEqual(self._llamar("post", "/api/recaudos/bot/abono", BOT).status_code, 401)
            self.assertEqual(self._llamar("post", "/api/bot/liquidar", REC).status_code, 401)

    def test_sin_ninguna_llave_configurada_503(self):
        with _env():
            for metodo, path, _var, _val in self.CASOS:
                self.assertEqual(self._llamar(metodo, path, GROK).status_code, 503, path)

    def test_misma_cadena_vacia_nunca_autentica(self):
        with _env(GROK_BOT_API_KEY="   ", BOT_API_KEY=BOT):
            self.assertEqual(self._llamar("post", "/api/bot/liquidar", "").status_code, 401)
            self.assertEqual(self._llamar("post", "/api/bot/liquidar", "   ").status_code, 401)

    def test_describe_config_grok_no_usa_heredada(self):
        with _env(LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(m2m_keys.describe_config()["grok"], "faltante")
        with _env(GROK_BOT_API_KEY=GROK):
            self.assertEqual(m2m_keys.describe_config()["grok"], "nueva")


class LlaveGrokNoAbreAdministracionTests(_Base):
    """Con la llave Grok solo se abren los tres prefijos de máquina; todo lo demás sigue exigiendo sesión."""

    EXPLICITAS = (
        ("GET", "/admin/usuarios"),
        ("POST", "/admin/usuarios/crear"),
        ("POST", "/admin/usuarios/actualizar"),
        ("POST", "/admin/usuarios/desactivar"),
        ("POST", "/acuerdos/purgar-todos"),
        ("POST", "/acuerdos/eliminar"),
        ("POST", "/sms/cola/limpiar"),
        ("GET", "/acuerdos"),
        ("GET", "/vencimientos"),
        ("GET", "/crm"),
        ("GET", "/herramientas/google-drive"),
        ("GET", "/portal-ph"),
        ("GET", "/supervision-agente"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/logout"),
        ("GET", "/api/recaudos/solicitudes/pendientes"),
        ("POST", "/api/recaudos/solicitudes/1/aprobar"),
        ("GET", "/api/recaudos/informes/exportar-excel"),
        ("GET", "/__interno/x"),
    )
    PROPIAS = (
        ("GET", "/sms/api/pendientes"),
        ("POST", "/api/v1/cron/sync-rama"),
    )

    def setUp(self):
        super().setUp()
        self.client = TestClient(main_mod.app, raise_server_exceptions=False)
        patcher = mock.patch.dict(
            os.environ,
            {"SMS_API_TOKEN": "sms-token-dummy-0000", "CRON_SECRET_KEY": "cron-dummy-0000"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pedir(self, metodo, path):
        self._sin_bloqueo()
        return self.client.request(
            metodo, path, headers={"X-API-Key": GROK}, follow_redirects=False
        )

    def test_rutas_de_administracion_rechazan_llave_grok(self):
        with _env(GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT, RECAUDOS_API_KEY=REC):
            for metodo, path in self.EXPLICITAS:
                resp = self._pedir(metodo, path)
                self.assertIn(resp.status_code, (302, 303, 401, 403), (metodo, path, resp.status_code))
                if resp.status_code in (302, 303):
                    self.assertIn("/login", resp.headers.get("location", ""), path)

    def test_rutas_publicas_con_secreto_propio_rechazan_llave_grok(self):
        with _env(GROK_BOT_API_KEY=GROK):
            for metodo, path in self.PROPIAS:
                resp = self._pedir(metodo, path)
                self.assertIn(resp.status_code, (401, 403, 503), (metodo, path, resp.status_code))

    def test_ninguna_ruta_no_maquina_responde_2xx_con_llave_grok(self):
        publicas_exactas = set(permisos.PUBLIC_EXACT_PATHS)
        propios = ("/api/bot/", "/api/recaudos/bot/", "/api/grok/", "/static/")
        revisadas = 0
        with _env(GROK_BOT_API_KEY=GROK):
            for metodo, path in _todas_las_rutas():
                if path in publicas_exactas or path.startswith(propios):
                    continue
                resp = self._pedir(metodo, path)
                revisadas += 1
                self.assertGreaterEqual(resp.status_code, 300, (metodo, path, resp.status_code))
                self.assertNotIn(resp.status_code, (200, 201, 202, 204), (metodo, path))
        self.assertGreater(revisadas, 60)

    def test_prefijos_de_maquina_son_exactamente_los_esperados(self):
        api_publicos = tuple(p for p in permisos.PUBLIC_PREFIXES if p.startswith("/api/"))
        self.assertEqual(
            api_publicos,
            ("/api/bot/", "/api/recaudos/bot/", "/api/grok/", "/api/v1/cron/"),
        )
        for path in ("/api/grok", "/api/grokx/a", "/api/recaudos/solicitudes/pendientes", "/admin/usuarios"):
            self.assertFalse(permisos.es_ruta_publica(path), path)

    def test_grok_no_exento_del_rbac_para_sesiones(self):
        admin = permisos.permisos_de_perfil(permisos.PERFIL_ADMIN)
        self.assertTrue(permisos.denegar_acceso(admin, "GET", "/api/grokx/ping"))


class AuditoriaYLimiteDeGrokTests(_Base):
    reiniciar_fallos = False

    def test_log_de_auditoria_sin_cuerpo_ni_llaves(self):
        with _env(GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT), mock.patch(
            "main._json_log"
        ) as log:
            resp = self.client.post(
                "/api/bot/liquidar?cedula=123",
                headers={"X-API-Key": GROK},
                json={"secreto_en_cuerpo": "CUERPO-NO-LOGUEAR"},
            )
        self.assertEqual(resp.status_code, 422)
        llamadas = [c for c in log.call_args_list if c.args[1:2] == ("m2m_audit",)]
        self.assertEqual(len(llamadas), 1)
        campos = llamadas[0].kwargs
        self.assertEqual(campos["consumidor"], "grok")
        self.assertEqual(campos["metodo"], "POST")
        self.assertEqual(campos["ruta"], "/api/bot/liquidar")
        self.assertEqual(campos["estado"], 422)
        todo = repr(llamadas[0])
        for no_debe in (GROK, BOT, "CUERPO-NO-LOGUEAR", "cedula", "123"):
            self.assertNotIn(no_debe, todo)

    def test_auditoria_tambien_en_api_grok_y_recaudos(self):
        with _env(GROK_BOT_API_KEY=GROK), mock.patch("main._json_log") as log:
            self.get("/api/grok/ping", GROK)
            self.post("/api/recaudos/bot/abono", GROK)
        rutas = [c.kwargs["ruta"] for c in log.call_args_list if c.args[1:2] == ("m2m_audit",)]
        self.assertEqual(rutas, ["/api/grok/ping", "/api/recaudos/bot/abono"])

    def test_llamadas_del_bot_de_cobranza_no_se_registran_como_grok(self):
        with _env(GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT), mock.patch("main._json_log") as log:
            self.post("/api/bot/liquidar", BOT)
        self.assertFalse([c for c in log.call_args_list if c.args[1:2] == ("m2m_audit",)])

    def test_rate_limit_propio_de_grok(self):
        ventana = rate_limit.SlidingWindow(3, 60)
        with _env(GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT), mock.patch.object(
            rate_limit.limiter, "grok_window", ventana
        ):
            codigos = [self.get("/api/grok/ping", GROK).status_code for _ in range(5)]
            self.assertEqual(codigos[:3], [200, 200, 200])
            self.assertEqual(codigos[3:], [429, 429])
            bloqueada = self.get("/api/grok/ping", GROK)
            self.assertIn("retry-after", bloqueada.headers)
            self.assertEqual(self.post("/api/bot/liquidar", GROK).status_code, 429)
            self.assertEqual(self.post("/api/bot/liquidar", BOT).status_code, 422)

    def test_rate_limit_grok_configurado_por_defecto(self):
        self.assertGreaterEqual(rate_limit.GROK_LIMIT, 1)
        self.assertTrue("/api/grok/".startswith(rate_limit.API_KEY_PREFIXES))

    def test_llaves_malas_de_grok_siguen_bajo_el_bloqueo_por_ip(self):
        with _env(GROK_BOT_API_KEY=GROK):
            codigos = [
                self.get("/api/grok/ping", "mala").status_code
                for _ in range(rate_limit.API_FAIL_LIMIT + 2)
            ]
        self.assertEqual(codigos[0], 401)
        self.assertIn(429, codigos)


class _FakeCursor:
    def __init__(self, filas=None):
        self.sql: list[str] = []
        self._filas = filas or []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))

    def fetchall(self):
        return list(self._filas)

    def fetchone(self):
        return self._filas[0] if self._filas else None


def _fake_lectura(cursor):
    @contextmanager
    def _ctx():
        yield cursor

    return _ctx


class DatosDeLecturaTests(_Base):
    def _con_grok(self):
        return _env(GROK_BOT_API_KEY=GROK)

    def test_lectura_abre_transaccion_solo_lectura_y_hace_rollback(self):
        eventos = []

        class Cur(_FakeCursor):
            def execute(self, sql, params=None):
                eventos.append(sql)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Conn:
            def cursor(self, **kw):
                return Cur()

            def rollback(self):
                eventos.append("ROLLBACK")

            def release(self):
                eventos.append("RELEASE")

        import db

        with mock.patch.object(db, "get_connection", return_value=Conn()):
            with grok_api._lectura() as cur:
                cur.execute("SELECT 1")
        self.assertEqual(eventos, ["SET TRANSACTION READ ONLY", "SELECT 1", "ROLLBACK", "RELEASE"])

    def test_procesos_no_expone_abogado_ni_usuarios(self):
        fila = {
            "radicado_interno": "R-1", "radicado_rama": "", "tipo_cartera": "JURIDICO",
            "naturaleza": "EJECUTIVO", "juzgado": "J1", "etapa_actual": "x", "estado": "ACTIVO",
            "pretensiones": "100", "medidas_cautelares": "", "demandante_nombre": "CONJ",
            "demandante_identificacion": "9", "demandado_nombre": "DEUDOR",
            "demandado_identificacion": "123", "abogado_asignado": "ABOGADO SECRETO",
            "password": "x", "token": "y",
        }
        with self._con_grok(), mock.patch(
            "expedientes_service.cargar_procesos_general_sin_duplicados", return_value=[fila]
        ) as cargar:
            resp = self.get("/api/grok/procesos?estado=activos&q=deudor", GROK)
        self.assertEqual(resp.status_code, 200)
        cargar.assert_called_once_with("ACTIVOS")
        cuerpo = resp.text
        self.assertIn("DEUDOR", cuerpo)
        for prohibido in ("ABOGADO SECRETO", "abogado", "password", "token"):
            self.assertNotIn(prohibido, cuerpo)

    def test_procesos_estado_invalido_422(self):
        with self._con_grok():
            self.assertEqual(self.get("/api/grok/procesos?estado=BORRAR", GROK).status_code, 422)

    def test_detalle_proceso_filtra_campos_de_usuarios_del_erp(self):
        proc = {
            "radicado_interno": "R-1", "estado": "ACTIVO", "abogado_id": 7,
            "abogado_asignado": "ABOGADO SECRETO",
            "inmueble": {"id": 5, "conjunto_residencial": "C", "torre_apto": "T1-101", "contacto_id": 3, "conjunto_id": 2},
            "propietarios_inmueble": [{"identificacion": "1", "nombre": "P", "telefono": "3", "email": "p@x.co", "es_principal": True}],
            "inactivacion": {"motivo": "pago", "fecha": date(2026, 1, 1), "usuario": "USUARIO ERP", "accion": "INACTIVAR"},
        }
        cursor = _FakeCursor()
        with self._con_grok(), mock.patch.object(grok_api, "_lectura", _fake_lectura(cursor)), mock.patch(
            "expedientes_service._get_process", return_value=proc
        ), mock.patch(
            "expedientes_service._get_demandantes", return_value=[{"nombre": "A", "identificacion": "1"}]
        ), mock.patch(
            "expedientes_service._get_demandados", return_value=[{"nombre": "B", "identificacion": "2", "telefono": "3"}]
        ), mock.patch(
            "expedientes_service._get_actuaciones",
            return_value=[{"id": 1, "fecha": date(2026, 1, 2), "etapa": "E", "descripcion": "D", "usuario": "USUARIO ERP", "archivo": "a.pdf"}],
        ), mock.patch(
            "obligaciones_service.obtener_obligaciones_proceso",
            return_value=[{"id": 9, "identificacion_deudor": "2", "capital_inicial": 10, "acreedor_contacto_id": 4}],
        ):
            resp = self.get("/api/grok/procesos/R-1", GROK)
        self.assertEqual(resp.status_code, 200, resp.text)
        for prohibido in ("ABOGADO SECRETO", "USUARIO ERP", "abogado", "usuario", "a.pdf", "contacto_id"):
            self.assertNotIn(prohibido, resp.text)
        datos = resp.json()["proceso"]
        self.assertEqual(datos["demandados"][0]["nombre"], "B")
        self.assertEqual(datos["obligaciones"][0]["id"], 9)

    def test_detalle_proceso_inexistente_404(self):
        cursor = _FakeCursor()
        with self._con_grok(), mock.patch.object(grok_api, "_lectura", _fake_lectura(cursor)), mock.patch(
            "expedientes_service._get_process", return_value=None
        ):
            self.assertEqual(self.get("/api/grok/procesos/NOPE", GROK).status_code, 404)

    def test_vencimientos_y_acuerdos_son_select(self):
        cursor = _FakeCursor([{"id": 1, "titulo": "T", "fecha_vencimiento": date(2026, 2, 1)}])
        with self._con_grok(), mock.patch.object(grok_api, "_lectura", _fake_lectura(cursor)):
            v = self.get("/api/grok/vencimientos?desde=2026-01-01&hasta=2026-12-31", GROK)
            a = self.get("/api/grok/acuerdos?estado=pendiente", GROK)
        self.assertEqual((v.status_code, a.status_code), (200, 200))
        self.assertEqual(v.json()["total"], 1)
        self.assertEqual(len(cursor.sql), 2)
        for sql in cursor.sql:
            self.assertTrue(sql.upper().startswith("SELECT"), sql)

    def test_fechas_invalidas_422(self):
        with self._con_grok():
            self.assertEqual(self.get("/api/grok/vencimientos?desde=ayer", GROK).status_code, 422)
            self.assertEqual(
                self.get("/api/grok/liquidacion?inmueble_id=1&fecha_corte=2999-01-01", GROK).status_code, 422
            )

    def test_conjuntos_solo_campos_publicos(self):
        cursor = _FakeCursor()
        fila = {"id": 1, "nombre": "C", "nit": "9", "persona_juridica": "PJ", "telefono": "3",
                "email": "c@x.co", "direccion": "d", "ciudad": "B", "unidades_count": 4, "contacto_id": 8}
        with self._con_grok(), mock.patch.object(grok_api, "_lectura", _fake_lectura(cursor)), mock.patch(
            "catalogos_service.listar_conjuntos", return_value=[fila]
        ):
            resp = self.get("/api/grok/conjuntos", GROK)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("contacto_id", resp.text)
        self.assertEqual(resp.json()["conjuntos"][0]["unidades_count"], 4)

    def test_liquidacion_se_calcula_sin_escribir(self):
        resumen = {"capital": 100, "intereses": 5, "honorarios_pct": 0, "honorarios": 0, "gastos": 0, "gran_total": 105}
        resultados = [{"periodo": "2026-01", "desde": "2026-01-01", "tasa_ea": 20, "tasa_mes": 1.5}]
        inm_info = (1, "C", "DEUDOR", "123")
        with self._con_grok(), mock.patch(
            "liquidador.motor_calculo_judicial", return_value=(resultados, resumen, inm_info)
        ) as motor:
            resp = self.get("/api/grok/liquidacion?inmueble_id=1&fecha_corte=2026-01-31", GROK)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(motor.call_args.kwargs["autocausar"], False)
        self.assertEqual(resp.json()["datos"]["gran_total"], 105.0)
        self.assertNotIn("detalle_mensual", resp.json()["datos"])

    def test_saldo_usa_servicio_existente(self):
        with self._con_grok(), mock.patch(
            "obligacion_saldo_service.calcular_saldo_obligacion",
            return_value={"saldo_total": 10.0, "saldo_verificado": True},
        ) as calc:
            resp = self.get("/api/grok/obligaciones/9/saldo?fecha_corte=2026-01-31", GROK)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calc.call_args.args[0], 9)

    def test_limpiar_quita_claves_sensibles(self):
        limpio = grok_api._limpiar(
            {"a": 1, "password": "x", "api_key": "y", "x": [{"token_acceso": 1, "ok": 2}], "hash_pw": 3}
        )
        self.assertEqual(limpio, {"a": 1, "x": [{"ok": 2}]})


if __name__ == "__main__":
    unittest.main()
