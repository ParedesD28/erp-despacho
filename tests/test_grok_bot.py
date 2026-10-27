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


class _FakeConexion:
    """Conexión vacía: estos tests solo prueban la llave, no deben tocar PostgreSQL."""

    def cursor(self, *args, **kwargs):
        @contextmanager
        def _ctx():
            yield _FakeCursor()

        return _ctx()

    def commit(self):
        pass

    def rollback(self):
        pass

    def release(self):
        pass


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
        with mock.patch("db.get_connection", return_value=_FakeConexion()):
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


class _ConexionGrabadora:
    """Conexión falsa que registra todo el SQL: el motor real corre sin base de datos."""

    def __init__(self, sql: list[str], inm_info):
        self._sql = sql
        self._inm_info = inm_info

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, *a, **kw):
        return _CursorGrabador(self._sql, self._inm_info)

    def release(self):
        pass

    def commit(self):
        self._sql.append("COMMIT")

    def rollback(self):
        pass


class _CursorGrabador:
    def __init__(self, sql, inm_info):
        self._sql = sql
        self._inm_info = inm_info

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sql.append(" ".join(str(sql).split()))

    def fetchone(self):
        return self._inm_info

    def fetchall(self):
        return []


class LiquidacionPdfTests(_Base):
    URL = "/api/grok/liquidacion/pdf"
    INM = ("Conjunto Demo", "T1-101", "DEUDOR DEMO", "123456")

    def setUp(self):
        super().setUp()
        import tempfile

        import pdf_storage

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(pdf_storage, "PDF_STORAGE_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ejecutar(self, query, key=GROK, filas=None, inm=INM):
        """Corre el motor real con una BD falsa; devuelve (respuesta, sql_ejecutado, sql_pandas)."""
        import pandas as pd

        sql: list[str] = []
        sql_pandas: list[str] = []
        base = filas if filas is not None else [
            (2026, 1, "Expensa Ordinaria", 100000.0),
            (2026, 2, "Expensa Ordinaria", 100000.0),
        ]
        df = pd.DataFrame(base, columns=["periodo_anio", "periodo_mes", "concepto", "valor_capital"])

        def _read_sql(consulta, conn, params=None):
            sql_pandas.append(" ".join(str(consulta).split()))
            return df.rename(columns={})[["concepto", "periodo_mes", "periodo_anio", "valor_capital"]]

        import db

        with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
            db, "get_connection", side_effect=lambda: _ConexionGrabadora(sql, inm)
        ), mock.patch("liquidador.pd.read_sql_query", side_effect=_read_sql), mock.patch(
            "tasas.obtener_tasa_bd_o_api", return_value=0.20
        ):
            resp = self.get(f"{self.URL}?{query}", key)
        return resp, sql, sql_pandas

    def test_devuelve_pdf_real_sin_escribir_en_expensas(self):
        resp, sql, sql_pandas = self._ejecutar("inmueble_id=7&fecha_corte=2026-03-31")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertEqual(resp.headers["content-type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF-"))
        self.assertIn("Liquidacion_7_2026-03-31.pdf", resp.headers["content-disposition"])
        todo = [*sql, *sql_pandas]
        self.assertTrue(todo, "el motor debía consultar la base")
        for consulta in todo:
            cabeza = consulta.upper().lstrip()
            self.assertTrue(cabeza.startswith("SELECT"), consulta)
            for escritura in ("INSERT", "UPDATE ", "DELETE", "ALTER ", "DROP "):
                self.assertNotIn(escritura, cabeza, consulta)
        self.assertNotIn("COMMIT", sql)

    def test_archivo_temporal_se_borra_despues_de_enviar(self):
        resp, _sql, _p = self._ejecutar("inmueble_id=7&fecha_corte=2026-03-31")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(Path(self._tmp.name).glob("*.pdf")), [])

    def test_motor_se_llama_sin_autocausar_y_con_obligacion(self):
        resultados = [{"periodo": "2026-01", "desde": "2026-01-01", "tasa_ea": 20, "tasa_mes": 1.5}]
        resumen = {"capital": 1, "intereses": 0, "honorarios_pct": 0, "honorarios": 0, "gastos": 0, "gran_total": 1}
        with _env(GROK_BOT_API_KEY=GROK), mock.patch(
            "liquidador.motor_calculo_judicial", return_value=(resultados, resumen, self.INM)
        ) as motor, mock.patch(
            "exportaciones.generar_pdf_liquidacion"
        ) as gen:
            ruta = Path(self._tmp.name) / "x.pdf"
            ruta.write_bytes(b"%PDF-1.4 prueba")
            gen.return_value = str(ruta)
            resp = self.get(f"{self.URL}?inmueble_id=7&fecha_corte=2026-03-31&obligacion_id=55", GROK)
        self.assertEqual(resp.status_code, 200)
        motor.assert_called_once_with(
            7, "usura", 2.5, 23.8, 0.0, date(2026, 3, 31), autocausar=False, obligacion_id=55
        )
        self.assertEqual(gen.call_args.args[:2], (7, date(2026, 3, 31)))

    def test_sin_llave_o_invalida_401(self):
        with _env(GROK_BOT_API_KEY=GROK), mock.patch("liquidador.motor_calculo_judicial") as motor:
            self.assertEqual(self.get(f"{self.URL}?inmueble_id=7&fecha_corte=2026-03-31").status_code, 401)
            for otra in ("mala", BOT, OLD):
                self.assertEqual(
                    self.get(f"{self.URL}?inmueble_id=7&fecha_corte=2026-03-31", otra).status_code, 401
                )
        motor.assert_not_called()

    def test_parametros_invalidos_422(self):
        with _env(GROK_BOT_API_KEY=GROK), mock.patch("liquidador.motor_calculo_judicial") as motor:
            for query in (
                "fecha_corte=2026-03-31",
                "inmueble_id=7",
                "inmueble_id=abc&fecha_corte=2026-03-31",
                "inmueble_id=7&fecha_corte=31-03-2026",
                "inmueble_id=7&fecha_corte=2999-01-01",
                "inmueble_id=7&fecha_corte=",
                "inmueble_id=7&fecha_corte=2026-03-31&obligacion_id=x",
            ):
                self.assertEqual(self.get(f"{self.URL}?{query}", GROK).status_code, 422, query)
        motor.assert_not_called()

    def test_sin_inmueble_o_sin_movimientos_404(self):
        resp, _s, _p = self._ejecutar("inmueble_id=999&fecha_corte=2026-03-31", filas=[], inm=None)
        self.assertEqual(resp.status_code, 404)
        resp, _s, _p = self._ejecutar("inmueble_id=999&fecha_corte=2026-03-31", filas=[], inm=self.INM)
        self.assertEqual(resp.status_code, 404)

    def test_otros_metodos_405(self):
        with _env(GROK_BOT_API_KEY=GROK):
            for metodo in ("post", "put", "patch", "delete"):
                resp = getattr(self.client, metodo)(
                    f"{self.URL}?inmueble_id=7&fecha_corte=2026-03-31",
                    headers={"X-API-Key": GROK},
                )
                self.assertEqual(resp.status_code, 405, metodo)

    def test_auditoria_registra_la_llamada_sin_query(self):
        with mock.patch("main._json_log") as log:
            resp, _s, _p = self._ejecutar("inmueble_id=7&fecha_corte=2026-03-31")
        self.assertEqual(resp.status_code, 200)
        llamadas = [c for c in log.call_args_list if c.args[1:2] == ("m2m_audit",)]
        self.assertEqual(len(llamadas), 1)
        self.assertEqual(llamadas[0].kwargs["ruta"], self.URL)
        self.assertEqual(llamadas[0].kwargs["metodo"], "GET")
        self.assertEqual(llamadas[0].kwargs["estado"], 200)
        self.assertNotIn("inmueble_id", repr(llamadas[0]))

    def test_respeta_el_rate_limit_de_grok(self):
        ventana = rate_limit.SlidingWindow(1, 60)
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
            rate_limit.limiter, "grok_window", ventana
        ), mock.patch("liquidador.motor_calculo_judicial") as motor:
            motor.return_value = ([], {}, None)
            self.get(f"{self.URL}?inmueble_id=7&fecha_corte=2026-03-31", GROK)
            resp = self.get(f"{self.URL}?inmueble_id=7&fecha_corte=2026-03-31", GROK)
        self.assertEqual(resp.status_code, 429)
        self.assertIn("retry-after", resp.headers)


BASE_SECRETA = "https://rama-secreta.invalid/api/v2"
RAD23 = "11001400300120240012300"
RAD23_B = "05001400300220240045600"


def _act(fecha="2026-03-10T00:00:00", actuacion="Auto", anotacion="Se admite", idx=None, **extra):
    import external_legal_client as elc

    datos = {
        "fechaActuacion": fecha, "actuacion": actuacion, "anotacion": anotacion,
        "fechaInicial": "2026-03-11T00:00:00", "fechaFinal": "2026-03-20T00:00:00",
        "fechaRegistro": "2026-03-10T15:30:00", "conDocumentos": False,
    }
    if idx is not None:
        datos["idRegActuacion"] = idx
    datos.update(extra)
    return elc.ActuacionResponse.model_validate(datos)


class ClienteRamaFalso:
    """Sustituye a LegalAPIClient: sin red. ``respuestas`` = {radicado: lista | Exception}."""

    instancias: list = []

    def __init__(self, respuestas=None, **kw):
        self.respuestas = respuestas or {}
        self.llamadas: list[tuple] = []
        self.en_vuelo = 0
        self.max_en_vuelo = 0
        self.retardo = 0.0
        self.kw = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def consultar_id_por_radicado(self, radicado):
        import asyncio

        self.llamadas.append(("id", radicado))
        self.en_vuelo += 1
        self.max_en_vuelo = max(self.max_en_vuelo, self.en_vuelo)
        try:
            if self.retardo:
                await asyncio.sleep(self.retardo)
            r = self.respuestas.get(radicado, [])
            if isinstance(r, BaseException):
                raise r
            return int(radicado[-6:])
        finally:
            self.en_vuelo -= 1

    async def obtener_detalle(self, id_interno):
        import external_legal_client as elc

        self.llamadas.append(("detalle", id_interno))
        return elc.ProcesoDetalleResponse.model_validate(
            {"despacho": "Juzgado 1 Civil", "estado": "Activo", "idProceso": id_interno}
        )

    async def obtener_actuaciones(self, id_interno):
        self.llamadas.append(("actuaciones", id_interno))
        for rad, r in self.respuestas.items():
            if int(rad[-6:]) == id_interno and not isinstance(r, BaseException):
                return list(r)
        return []

    async def obtener_sujetos(self, id_interno):
        import external_legal_client as elc

        self.llamadas.append(("sujetos", id_interno))
        return [elc.SujetoResponse.model_validate(
            {"tipoSujeto": "Demandado", "nombreRazonSocial": "DEUDOR DEMO", "identificacion": "123"}
        )]


class _RamaBase(_Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"LEGAL_API_BASE_URL": BASE_SECRETA})
        patcher.start()
        self.addCleanup(patcher.stop)

    def con_cliente(self, cliente):
        return mock.patch("external_legal_client.LegalAPIClient", return_value=cliente)

    def logs(self):
        """Captura todo lo registrado (logging + print) para buscar secretos."""
        import logging

        registros: list[str] = []

        class H(logging.Handler):
            def emit(self, record):
                registros.append(record.getMessage())

        h = H(level=logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(h)
        self.addCleanup(root.removeHandler, h)
        return registros


class RamaConsultaTests(_RamaBase):
    URL = "/api/grok/rama/consulta"

    def test_sin_llave_o_invalida_401_sin_llamar_a_la_rama(self):
        cliente = ClienteRamaFalso()
        with _env(GROK_BOT_API_KEY=GROK, BOT_API_KEY=BOT), self.con_cliente(cliente) as ctor:
            for key in (None, "", "mala", BOT):
                self.assertEqual(self.get(f"{self.URL}?radicado={RAD23}", key).status_code, 401)
        ctor.assert_not_called()

    def test_consulta_por_radicado_de_23_digitos(self):
        cliente = ClienteRamaFalso({RAD23: [_act(idx=7), _act("2026-04-01T00:00:00", "Memorial", "Pago", idx=8)]})
        with _env(GROK_BOT_API_KEY=GROK), self.con_cliente(cliente):
            resp = self.get(f"{self.URL}?radicado={RAD23}", GROK)
        self.assertEqual(resp.status_code, 200, resp.text)
        d = resp.json()
        self.assertEqual(d["radicado"], RAD23)
        self.assertEqual(d["id_interno"], int(RAD23[-6:]))
        self.assertEqual(d["detalle"]["juzgado"], "Juzgado 1 Civil")
        self.assertEqual([a["actuacion"] for a in d["actuaciones"]], ["Memorial", "Auto"])
        act = d["actuaciones"][1]
        self.assertEqual(act["fecha_actuacion"], "2026-03-10")
        self.assertEqual(act["inicio_termino"], "2026-03-11")
        self.assertEqual(act["fin_termino"], "2026-03-20")
        self.assertEqual(act["fecha_registro"], "2026-03-10")
        self.assertEqual(d["sujetos"][0]["nombre"], "DEUDOR DEMO")
        self.assertEqual(
            [c[0] for c in cliente.llamadas], ["id", "detalle", "actuaciones", "sujetos"]
        )

    def test_radicado_con_separadores_y_limite_de_actuaciones(self):
        cliente = ClienteRamaFalso({RAD23: [_act(f"2026-03-{d:02d}T00:00:00", idx=d) for d in range(1, 6)]})
        con_puntos = "-".join([RAD23[:5], RAD23[5:9], RAD23[9:]])
        with _env(GROK_BOT_API_KEY=GROK), self.con_cliente(cliente):
            resp = self.get(f"{self.URL}?radicado={con_puntos}&limite_actuaciones=2", GROK)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(cliente.llamadas[0], ("id", RAD23))
        self.assertEqual(resp.json()["total_actuaciones"], 5)
        self.assertEqual(len(resp.json()["actuaciones"]), 2)

    def test_exp_se_resuelve_desde_procesos(self):
        cursor = _FakeCursor([{"radicado_interno": "EXP-0001", "radicado_rama": " 11001-4003-001-2024-00123-00 "}])
        cliente = ClienteRamaFalso({RAD23: [_act(idx=1)]})
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
            grok_api, "_lectura", _fake_lectura(cursor)
        ), self.con_cliente(cliente):
            resp = self.get(f"{self.URL}?radicado=exp-0001", GROK)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["radicado_interno"], "EXP-0001")
        self.assertEqual(cliente.llamadas[0], ("id", RAD23))
        self.assertTrue(cursor.sql[0].upper().startswith("SELECT"))

    def test_exp_inexistente_o_sin_radicado_rama_404(self):
        for fila in ([], [{"radicado_interno": "EXP-9", "radicado_rama": None}],
                     [{"radicado_interno": "EXP-9", "radicado_rama": "123"}]):
            cursor = _FakeCursor(fila)
            cliente = ClienteRamaFalso()
            with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
                grok_api, "_lectura", _fake_lectura(cursor)
            ), self.con_cliente(cliente):
                resp = self.get(f"{self.URL}?radicado=EXP-9", GROK)
            self.assertEqual(resp.status_code, 404, fila)
            self.assertEqual(cliente.llamadas, [])

    def test_parametros_invalidos_422(self):
        with _env(GROK_BOT_API_KEY=GROK), self.con_cliente(ClienteRamaFalso()) as ctor:
            for q in ("", "radicado=", "radicado=%3Cscript%3E", f"radicado={RAD23}&limite_actuaciones=0",
                      f"radicado={RAD23}&limite_actuaciones=5000", "radicado=" + "A" * 81):
                self.assertEqual(self.get(f"{self.URL}?{q}", GROK).status_code, 422, q)
        ctor.assert_not_called()

    def test_la_rama_no_conoce_el_radicado_404(self):
        import external_legal_client as elc  # noqa: F401
        from fastapi import HTTPException

        cliente = ClienteRamaFalso({RAD23: HTTPException(404, "No se encontró proceso para el radicado indicado")})
        with _env(GROK_BOT_API_KEY=GROK), self.con_cliente(cliente):
            resp = self.get(f"{self.URL}?radicado={RAD23}", GROK)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], grok_api._MSG_NO_ENCONTRADO)

    def test_rama_caida_502_con_mensaje_corto_sin_secretos(self):
        import httpx
        from fastapi import HTTPException

        errores = (
            HTTPException(502, f"API legal externa respondió HTTP 500. base_path=/api/v2. Cuerpo: {BASE_SECRETA} token=XYZ"),
            HTTPException(504, "Timeout de lectura con la API legal externa"),
            httpx.ConnectError(f"no se pudo conectar a {BASE_SECRETA}"),
            RuntimeError(f"boom {BASE_SECRETA}"),
        )
        for err in errores:
            cliente = ClienteRamaFalso({RAD23: err})
            registros = self.logs()
            with _env(GROK_BOT_API_KEY=GROK), self.con_cliente(cliente), mock.patch(
                "builtins.print"
            ) as fake_print:
                resp = self.get(f"{self.URL}?radicado={RAD23}", GROK)
            self.assertEqual(resp.status_code, 502, repr(err))
            self.assertEqual(resp.json()["error"], grok_api._MSG_NO_RESPONDE)
            visible = resp.text + " ".join(registros) + repr(fake_print.call_args_list)
            for secreto in (BASE_SECRETA, "rama-secreta", "token=XYZ", "base_path", GROK):
                self.assertNotIn(secreto, visible, repr(err))

    def test_timeout_por_consulta_502(self):
        cliente = ClienteRamaFalso({RAD23: [_act()]})
        cliente.retardo = 1.0
        with _env(GROK_BOT_API_KEY=GROK, GROK_RAMA_TIMEOUT_S="1"), self.con_cliente(cliente), mock.patch.object(
            grok_api, "_timeout_consulta_s", return_value=0.05
        ):
            resp = self.get(f"{self.URL}?radicado={RAD23}", GROK)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["error"], grok_api._MSG_TIEMPO)

    def test_sin_base_url_configurada_503_sin_filtrar_nada(self):
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.dict(os.environ, {"LEGAL_API_BASE_URL": ""}):
            resp = self.get(f"{self.URL}?radicado={RAD23}", GROK)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"], grok_api._MSG_NO_CONFIGURADA)

    def test_solo_get_y_auditoria_sin_radicado(self):
        cliente = ClienteRamaFalso({RAD23: [_act(idx=1)]})
        with _env(GROK_BOT_API_KEY=GROK), self.con_cliente(cliente), mock.patch("main._json_log") as log:
            self.get(f"{self.URL}?radicado={RAD23}", GROK)
            self.assertEqual(self.post(f"{self.URL}?radicado={RAD23}", GROK).status_code, 405)
        llamadas = [c for c in log.call_args_list if c.args[1:2] == ("m2m_audit",)]
        self.assertEqual(llamadas[0].kwargs["ruta"], self.URL)
        self.assertNotIn(RAD23, repr(llamadas))

    def test_respeta_el_rate_limit_de_grok(self):
        ventana = rate_limit.SlidingWindow(1, 60)
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
            rate_limit.limiter, "grok_window", ventana
        ), self.con_cliente(ClienteRamaFalso({RAD23: []})):
            self.get(f"{self.URL}?radicado={RAD23}", GROK)
            resp = self.get(f"{self.URL}?radicado={RAD23}", GROK)
        self.assertEqual(resp.status_code, 429)


def _proceso(n, rama=RAD23):
    return {
        "radicado_interno": f"EXP-{n:04d}", "radicado_rama": rama, "juzgado": "J1",
        "conjunto_residencial": "Conjunto Demo", "torre_apto": f"T1-{n}", "demandado": f"DEUDOR {n}",
    }


def _registro(ids=(), hashes=(), manuales=()):
    return {"ids": set(ids), "hashes": set(hashes), "manuales": list(manuales)}


class RamaNovedadesTests(_RamaBase):
    URL = "/api/grok/rama/novedades"

    def _pedir(self, procesos, registros, cliente, query="", total=None):
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
            grok_api, "_cargar_procesos_y_registros",
            return_value=(total if total is not None else len(procesos), procesos, registros),
        ) as carga, self.con_cliente(cliente):
            resp = self.get(f"{self.URL}?{query}", GROK)
        return resp, carga

    def test_sin_llave_401(self):
        cliente = ClienteRamaFalso()
        with _env(GROK_BOT_API_KEY=GROK), self.con_cliente(cliente) as ctor, mock.patch.object(
            grok_api, "_cargar_procesos_y_registros"
        ) as carga:
            self.assertEqual(self.get(self.URL).status_code, 401)
            self.assertEqual(self.get(self.URL, BOT).status_code, 401)
        ctor.assert_not_called()
        carga.assert_not_called()

    def test_desde_o_no_registrada_con_datos_del_proceso(self):
        p = _proceso(1)
        cliente = ClienteRamaFalso({RAD23: [
            _act("2026-01-05T00:00:00", "Auto", "Antigua y ya registrada", idx=1),
            _act("2026-01-06T00:00:00", "Auto", "Antigua no registrada", idx=2),
            _act("2026-03-10T00:00:00", "Sentencia", "Nueva y ya registrada", idx=3),
            _act("2026-03-12T00:00:00", "Memorial", "Nueva", idx=4),
        ]})
        reg = {p["radicado_interno"]: _registro(ids={"1", "3"})}
        resp, carga = self._pedir([p], reg, cliente, "desde=2026-03-01&estado=activos&limit=10&offset=5")
        self.assertEqual(resp.status_code, 200, resp.text)
        carga.assert_called_once_with("ACTIVOS", 10, 5)
        d = resp.json()
        self.assertEqual((d["total_procesos"], d["consultados"], d["con_novedades"]), (1, 1, 1))
        nov = d["novedades"][0]
        self.assertEqual(
            (nov["radicado_interno"], nov["conjunto"], nov["demandado"], nov["juzgado"], nov["radicado_rama"]),
            ("EXP-0001", "Conjunto Demo", "DEUDOR 1", "J1", RAD23),
        )
        por_id = {a["id_actuacion"]: a for a in nov["actuaciones"]}
        self.assertEqual(set(por_id), {"2", "3", "4"})
        self.assertEqual(por_id["2"]["motivo"], ["no_registrada"])
        self.assertEqual(por_id["3"]["motivo"], ["posterior_a_desde"])
        self.assertEqual(por_id["4"]["motivo"], ["posterior_a_desde", "no_registrada"])
        self.assertTrue(por_id["3"]["registrada_en_erp"])
        self.assertEqual(d["errores"], [])

    def test_solo_no_registradas_exige_ambas_condiciones(self):
        p = _proceso(1)
        cliente = ClienteRamaFalso({RAD23: [
            _act("2026-01-06T00:00:00", anotacion="vieja sin registrar", idx=2),
            _act("2026-03-10T00:00:00", anotacion="nueva registrada", idx=3),
            _act("2026-03-12T00:00:00", anotacion="nueva sin registrar", idx=4),
        ]})
        reg = {p["radicado_interno"]: _registro(ids={"3"})}
        resp, _c = self._pedir([p], reg, cliente, "desde=2026-03-01&solo_no_registradas=true")
        ids = [a["id_actuacion"] for a in resp.json()["novedades"][0]["actuaciones"]]
        self.assertEqual(ids, ["4"])

    def test_sin_desde_solo_trae_lo_no_registrado_y_omite_procesos_sin_novedad(self):
        p1, p2 = _proceso(1, RAD23), _proceso(2, RAD23_B)
        cliente = ClienteRamaFalso({
            RAD23: [_act(idx=1)],
            RAD23_B: [_act(idx=9), _act("2026-03-11T00:00:00", "Otro", "Nueva", idx=10)],
        })
        reg = {"EXP-0001": _registro(ids={"1"}), "EXP-0002": _registro(ids={"9"})}
        resp, _c = self._pedir([p1, p2], reg, cliente)
        d = resp.json()
        self.assertEqual([n["radicado_interno"] for n in d["novedades"]], ["EXP-0002"])
        self.assertEqual([a["id_actuacion"] for a in d["novedades"][0]["actuaciones"]], ["10"])

    def test_error_en_un_proceso_va_a_errores_y_no_tumba_el_resto(self):
        import httpx
        from fastapi import HTTPException

        procesos = [_proceso(1, RAD23), _proceso(2, RAD23_B), _proceso(3, "73001400300320240078900")]
        cliente = ClienteRamaFalso({
            RAD23: [_act(idx=1)],
            RAD23_B: httpx.ConnectError(f"fallo {BASE_SECRETA}"),
            "73001400300320240078900": HTTPException(404, "No se encontró proceso para el radicado indicado"),
        })
        reg = {p["radicado_interno"]: _registro() for p in procesos}
        registros = self.logs()
        resp, _c = self._pedir(procesos, reg, cliente)
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertEqual([n["radicado_interno"] for n in d["novedades"]], ["EXP-0001"])
        self.assertEqual(
            d["errores"],
            [
                {"radicado_interno": "EXP-0002", "error": grok_api._MSG_NO_RESPONDE},
                {"radicado_interno": "EXP-0003", "error": grok_api._MSG_NO_ENCONTRADO},
            ],
        )
        self.assertEqual(d["consultados"], 1)
        visible = resp.text + " ".join(registros)
        for secreto in (BASE_SECRETA, "rama-secreta"):
            self.assertNotIn(secreto, visible)

    def test_timeout_por_consulta_va_a_errores(self):
        cliente = ClienteRamaFalso({RAD23: [_act(idx=1)]})
        cliente.retardo = 1.0
        with mock.patch.object(grok_api, "_timeout_consulta_s", return_value=0.05):
            resp, _c = self._pedir([_proceso(1)], {"EXP-0001": _registro()}, cliente)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["errores"], [{"radicado_interno": "EXP-0001", "error": grok_api._MSG_TIEMPO}])

    def test_tope_de_concurrencia(self):
        procesos = [_proceso(n, f"{n:023d}") for n in range(1, 13)]
        cliente = ClienteRamaFalso({p["radicado_rama"]: [_act(idx=int(p["radicado_rama"]))] for p in procesos})
        cliente.retardo = 0.02
        reg = {p["radicado_interno"]: _registro() for p in procesos}
        with mock.patch.dict(os.environ, {"GROK_RAMA_CONCURRENCY": "2"}):
            resp, _c = self._pedir(procesos, reg, cliente)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["con_novedades"], 12)
        self.assertLessEqual(cliente.max_en_vuelo, 2)
        self.assertGreaterEqual(cliente.max_en_vuelo, 1)

    def test_concurrencia_configurada_se_acota(self):
        for valor, esperado in (("99", 5), ("0", 1), ("x", 3), ("", 3)):
            with mock.patch.dict(os.environ, {"GROK_RAMA_CONCURRENCY": valor}):
                self.assertEqual(grok_api._concurrencia(), esperado, valor)

    def test_plazo_total_agotado_manda_el_resto_a_errores(self):
        procesos = [_proceso(n, f"{n:023d}") for n in range(1, 5)]
        cliente = ClienteRamaFalso({p["radicado_rama"]: [_act(idx=1)] for p in procesos})
        cliente.retardo = 0.05
        reg = {p["radicado_interno"]: _registro() for p in procesos}
        with mock.patch.dict(os.environ, {"GROK_RAMA_CONCURRENCY": "1"}), mock.patch.object(
            grok_api, "_timeout_total_s", return_value=0.0
        ):
            resp, _c = self._pedir(procesos, reg, cliente)
        d = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(d["errores"]), 4)
        self.assertTrue(all(e["error"] == grok_api._MSG_TIEMPO for e in d["errores"]))
        self.assertEqual(cliente.llamadas, [])

    def test_sin_procesos_no_abre_cliente(self):
        cliente = ClienteRamaFalso()
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
            grok_api, "_cargar_procesos_y_registros", return_value=(0, [], {})
        ), self.con_cliente(cliente) as ctor:
            resp = self.get(self.URL, GROK)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["novedades"], [])
        ctor.assert_not_called()

    def test_rama_no_configurada_503(self):
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.dict(os.environ, {"LEGAL_API_BASE_URL": ""}), mock.patch.object(
            grok_api, "_cargar_procesos_y_registros", return_value=(1, [_proceso(1)], {"EXP-0001": _registro()})
        ):
            resp = self.get(self.URL, GROK)
        self.assertEqual(resp.status_code, 503)

    def test_parametros_invalidos_422(self):
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
            grok_api, "_cargar_procesos_y_registros"
        ) as carga, self.con_cliente(ClienteRamaFalso()) as ctor:
            for q in ("estado=BORRAR", "desde=ayer", "desde=2026-13-45", "limit=0", "limit=101",
                      "offset=-1", "limit=abc"):
                self.assertEqual(self.get(f"{self.URL}?{q}", GROK).status_code, 422, q)
        carga.assert_not_called()
        ctor.assert_not_called()

    def test_otros_metodos_405(self):
        with _env(GROK_BOT_API_KEY=GROK):
            for metodo in ("post", "put", "patch", "delete"):
                resp = getattr(self.client, metodo)(self.URL, headers={"X-API-Key": GROK})
                self.assertEqual(resp.status_code, 405, metodo)

    def test_consultas_de_base_de_datos_son_solo_select_y_en_solo_lectura(self):
        class Cur(_FakeCursor):
            def fetchone(self):
                self.ultimo = self.sql[-1]
                if "COUNT(*)" in self.ultimo:
                    return {"total": 1}
                if "to_regclass" in self.ultimo:
                    return {"ok": True}
                return None

            def fetchall(self):
                u = self.sql[-1]
                if "FROM procesos" in u and "LIMIT" in u:
                    return [_proceso(1)]
                return []

        cursor = Cur()
        with mock.patch.object(grok_api, "_lectura", _fake_lectura(cursor)):
            total, procesos, registros = grok_api._cargar_procesos_y_registros("ACTIVOS", 100, 0)
        self.assertEqual((total, len(procesos)), (1, 1))
        self.assertIn("EXP-0001", registros)
        self.assertGreaterEqual(len(cursor.sql), 4)
        for sql in cursor.sql:
            cabeza = sql.upper().lstrip()
            self.assertTrue(cabeza.startswith("SELECT"), sql)
            for escritura in ("INSERT", "UPDATE ", "DELETE", "ALTER ", "DROP "):
                self.assertNotIn(escritura, cabeza)

    def test_ya_registrada_por_id_hash_o_texto_manual(self):
        import datetime as dt

        import sync_procesos_service as sps

        a = sps.mapear_actuacion_api(
            _act("2026-03-10T00:00:00", "Auto", "Se admite la demanda", idx=5),
            radicado_interno="EXP-1", radicado_rama=RAD23,
        )
        self.assertTrue(grok_api._ya_registrada(a, _registro(ids={"5"})))
        self.assertTrue(grok_api._ya_registrada(a, _registro(hashes={a["content_hash"]})))
        manual = (dt.date(2026, 3, 10), sps._norm_text("Auto: se admite la demanda ejecutiva"))
        self.assertTrue(grok_api._ya_registrada(a, _registro(manuales=[manual])))
        otra_fecha = (dt.date(2026, 3, 11), manual[1])
        self.assertFalse(grok_api._ya_registrada(a, _registro(manuales=[otra_fecha])))
        otro_texto = (dt.date(2026, 3, 10), "algo distinto")
        self.assertFalse(grok_api._ya_registrada(a, _registro(manuales=[otro_texto])))
        self.assertFalse(grok_api._ya_registrada(a, _registro(ids={"99"})))

    def test_auditoria_y_rate_limit_de_grok(self):
        ventana = rate_limit.SlidingWindow(1, 60)
        cliente = ClienteRamaFalso()
        with _env(GROK_BOT_API_KEY=GROK), mock.patch.object(
            rate_limit.limiter, "grok_window", ventana
        ), mock.patch.object(
            grok_api, "_cargar_procesos_y_registros", return_value=(0, [], {})
        ), self.con_cliente(cliente), mock.patch("main._json_log") as log:
            ok = self.get(f"{self.URL}?desde=2026-01-01", GROK)
            limitado = self.get(f"{self.URL}?desde=2026-01-01", GROK)
        self.assertEqual((ok.status_code, limitado.status_code), (200, 429))
        llamadas = [c for c in log.call_args_list if c.args[1:2] == ("m2m_audit",)]
        self.assertEqual([c.kwargs["estado"] for c in llamadas], [200, 429])
        self.assertEqual(llamadas[0].kwargs["ruta"], self.URL)
        self.assertNotIn("2026-01-01", repr(llamadas))


if __name__ == "__main__":
    unittest.main()
