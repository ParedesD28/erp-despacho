"""CLIENTE y CLIENTE PH: expediente de solo lectura."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

from jinja2 import Environment, FileSystemLoader

import permisos
import security
import session_store
import usuarios_service


_MUTACIONES = [
    ("POST", "/actuacion/nueva"),
    ("POST", "/actuacion/editar"),
    ("POST", "/actuacion/eliminar"),
    ("POST", "/expediente/estado"),
    ("POST", "/expediente/guardar-estructurado"),
    ("POST", "/tareas/nueva"),
    ("POST", "/tareas/1/completar"),
    ("POST", "/tareas/1/reasignar"),
    ("POST", "/acuerdos/guardar"),
    ("POST", "/vencimientos/guardar"),
    ("PATCH", "/expediente/EXP-0003"),
    ("DELETE", "/expediente/EXP-0003"),
]

_BOTONES_PROHIBIDOS = [
    "✎ Editar expediente",
    "⏸ Inactivar",
    "＋ Agenda",
    "Ver en CRM",
    "＋ Nueva tarea",
    "＋ Actuación",
    ">Editar<",
    ">Eliminar<",
    "Completar",
    "id=\"modal-nueva-tarea-exp\"",
    "id=\"act-modal\"",
    "id=\"act-edit-modal\"",
    "id=\"agenda-modal\"",
    "id=\"editor-expediente\"",
]


class _QP(dict):
    def __getattr__(self, name):
        return self.get(name)


def _request(perfil: str):
    return SimpleNamespace(
        state=SimpleNamespace(
            permisos=permisos.permisos_de_perfil(perfil),
            perfil_codigo=perfil,
        ),
        query_params=_QP(),
        url=SimpleNamespace(path="/expediente/EXP-0003"),
    )


def _proceso():
    return {
        "radicado_interno": "EXP-0003",
        "tipo_cartera": "JURIDICO",
        "estado": "ACTIVO",
        "etapa_actual": "DEMANDA",
        "inmueble": {
            "conjunto_id": 7,
            "torre_apto": "1-1",
            "conjunto_residencial": "Conjunto",
        },
        "inmueble_id": 9,
        "juzgado": "Juzgado 1",
        "naturaleza": "EJECUTIVO",
        "radicado_rama": "05000",
        "estado_rama": None,
        "pretensiones": 1000,
        "abogado_asignado": "Ana",
        "abogado_id": 1,
        "medidas_cautelares": "Embargo",
        "propietarios_inmueble": [],
    }


def _timeline():
    return [
        {
            "id": 50,
            "actuacion_id": 12,
            "fecha": date(2026, 3, 2),
            "etapa": "ADMISIÓN",
            "descripcion": "Se admite la demanda",
            "es_manual": True,
            "origen": "manual",
            "responsable": "Juzgado",
        }
    ]


def _render(perfil: str) -> str:
    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    return env.get_template("detalle_expediente_v4.html").render(
        request=_request(perfil),
        proceso=_proceso(),
        demandantes=[{"nombre": "Conjunto X", "identificacion": "800"}],
        demandados=[{"nombre": "Deudor", "identificacion": "123"}],
        obligaciones=[],
        obligacion_principal={
            "id": 1,
            "tipo_obligacion_nombre": "Cuotas",
            "tipo_obligacion_codigo": "CUOTAS",
            "numero_documento": None,
            "fuente_saldo": "CERTIFICADO",
            "estado": "ACTIVA",
        },
        contactos=[],
        abogados=[],
        actuaciones=_timeline(),
        timeline=_timeline(),
        tramos_reloj=[],
        reloj={"duenio": "Juzgado", "dias": 2, "nos_toca": False},
        totales_reloj={"despacho": 1, "firma": 0},
        etapas_actuacion=["DEMANDA", "ADMISIÓN"],
        alertas_expediente=[],
        tareas_proceso=[
            {
                "id": 1,
                "titulo": "Contestar traslado",
                "estado": "Pendiente",
                "prioridad": "Alta",
                "asignado_nombre": "Ana",
                "asignado_a": 7,
                "fecha_vencimiento": "2026-03-20",
                "origen": "manual",
                "es_automatica": False,
                "descripcion": "",
            }
        ],
        usuarios_tareas=[],
        acuerdos_crm=[{"tipo_contacto": "Llamada", "resumen": "Promesa", "promesa_pago_fecha": None}],
        audit_ediciones=[],
        conjuntos=[],
        inmueble_bloque="1",
        inmueble_apartamento="1",
        demandante_ids={"800"},
        demandado_ids={"123"},
    )


class PermisosLecturaClienteTests(unittest.TestCase):
    def test_get_expediente_permitido_mutaciones_no(self):
        for perfil in (permisos.PERFIL_CLIENTE, permisos.PERFIL_CLIENTE_PH):
            bag = permisos.permisos_de_perfil(perfil)
            self.assertFalse(permisos.denegar_acceso(bag, "GET", "/expediente/EXP-0003"), perfil)
            self.assertFalse(permisos.denegar_acceso(bag, "GET", "/expedientes"), perfil)
            self.assertTrue(permisos.denegar_acceso(bag, "GET", "/tareas"), perfil)
            self.assertTrue(permisos.denegar_acceso(bag, "GET", "/crm"), perfil)
            self.assertTrue(permisos.denegar_acceso(bag, "GET", "/expediente-docs/EXP-0003/a.pdf"), perfil)
            for metodo, ruta in _MUTACIONES:
                self.assertTrue(
                    permisos.denegar_acceso(bag, metodo, ruta),
                    (perfil, metodo, ruta),
                )


class UiLecturaClienteTests(unittest.TestCase):
    def test_cliente_ve_memoria_sin_botones(self):
        for perfil in (permisos.PERFIL_CLIENTE, permisos.PERFIL_CLIENTE_PH):
            html = _render(perfil)
            self.assertIn("EXP-0003", html)
            self.assertIn("Memoria del expediente", html)
            self.assertIn("Se admite la demanda", html)
            self.assertIn("Reloj Parte vs Juzgado", html)
            self.assertIn("Medidas cautelares", html)
            self.assertIn("Embargo", html)
            self.assertNotIn("h2 class=\"text-sm font-black text-slate-900\">Tareas<", html)
            self.assertNotIn("＋ Nueva tarea", html)
            self.assertNotIn("Contestar traslado", html)
            for token in _BOTONES_PROHIBIDOS:
                self.assertNotIn(token, html, (perfil, token))
            if perfil == permisos.PERFIL_CLIENTE_PH:
                self.assertIn("← Unidades", html)
            else:
                self.assertIn("← Expedientes", html)

    def test_abogado_sigue_viendo_botones(self):
        html = _render(permisos.PERFIL_ABOGADO)
        self.assertIn("✎ Editar expediente", html)
        self.assertIn("⏸ Inactivar", html)
        self.assertIn("＋ Agenda", html)
        self.assertIn("Ver en CRM", html)
        self.assertIn("＋ Actuación", html)
        self.assertIn(">Editar<", html)
        self.assertIn(">Eliminar<", html)
        self.assertIn("＋ Nueva tarea", html)
        self.assertIn("Contestar traslado", html)
        self.assertIn("Completar", html)


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, cursor_factory=None):
        return _FakeCur()

    def commit(self):
        return None

    def rollback(self):
        return None

    def release(self):
        return None


class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class HttpLecturaClienteTests(unittest.TestCase):
    def setUp(self):
        import main
        from fastapi.testclient import TestClient

        self.main = main
        session_store.reset_cache_for_tests()
        self.client = TestClient(main.app, follow_redirects=False)
        self._patches = [
            patch.object(session_store, "esta_revocada", return_value=False),
            patch.object(main.db, "get_connection", return_value=_FakeConn()),
            patch.object(main.expedientes_service, "_get_process", return_value=_proceso()),
            patch.object(main.expedientes_service, "_sync_stage", return_value="DEMANDA"),
            patch.object(main.expedientes_service, "_get_demandantes", return_value=[{"nombre": "C", "identificacion": "1"}]),
            patch.object(main.expedientes_service, "_get_demandados", return_value=[{"nombre": "D", "identificacion": "2"}]),
            patch.object(main.expedientes_service, "_get_actuaciones", return_value=_timeline()),
            patch.object(main.expedientes_service, "_contact_options", return_value=[]),
            patch.object(main.expedientes_service, "_get_abogados", return_value=[]),
            patch.object(main.expedientes_service, "_get_crm_agreements", return_value=[]),
            patch.object(main.expedientes_service, "_audit", return_value=[]),
            patch.object(
                main.obligaciones_service,
                "obtener_obligaciones_proceso",
                return_value=[
                    {
                        "id": 1,
                        "tipo_obligacion_nombre": "Cuotas",
                        "tipo_obligacion_codigo": "CUOTAS",
                        "estado": "ACTIVA",
                        "fuente_saldo": "CERTIFICADO",
                    }
                ],
            ),
            patch.object(main.catalogos_service, "listar_conjuntos", return_value=[]),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

        import desviaciones_procesales_service as dps
        import sync_procesos_service
        import tareas_service

        extra = [
            patch.object(dps, "procesar_pendientes", return_value=None),
            patch.object(
                dps,
                "timeline_unificada",
                return_value={
                    "eventos": _timeline(),
                    "tramos": [],
                    "abierto": None,
                    "totales": {"despacho": 1, "firma": 0},
                },
            ),
            patch.object(
                dps,
                "reloj_abierto",
                return_value={"duenio": "Juzgado", "dias": 2, "semaforo": None, "totales": {"despacho": 1, "firma": 0}, "nos_toca": False},
            ),
            patch.object(sync_procesos_service, "_table_exists", return_value=False),
            patch.object(tareas_service, "listar_tareas_proceso", return_value=[]),
            patch.object(tareas_service, "listar_usuarios_asignables", return_value=[]),
            patch.object(tareas_service, "adjuntar_tareas_completadas_a_timeline", return_value=None),
        ]
        for p in extra:
            p.start()
            self.addCleanup(p.stop)

    def _call(self, method, path, perfil, **kw):
        token = security.sign_session("1")
        self.client.cookies.set(security.SESSION_COOKIE, token)
        usuario = {
            "id": 1,
            "nombre": "Cliente",
            "email": "c@x.com",
            "activo": True,
            "perfil_codigo": perfil,
            "perfil_nombre": perfil,
            "password": "h",
        }
        with patch.object(usuarios_service, "obtener_usuario_por_id", return_value=usuario):
            return self.client.request(method, path, **kw)

    def test_get_expediente_ok(self):
        for perfil in ("CLIENTE", "CLIENTE_PH"):
            resp = self._call("GET", "/expediente/EXP-0003", perfil)
            self.assertEqual(resp.status_code, 200, perfil)
            body = resp.text
            self.assertIn("EXP-0003", body)
            self.assertIn("Memoria del expediente", body)
            self.assertNotIn("＋ Actuación", body)
            self.assertNotIn("＋ Nueva tarea", body)
            self.assertNotIn("Ver en CRM", body)

    def test_mutaciones_redirigen(self):
        for perfil in ("CLIENTE", "CLIENTE_PH"):
            home = permisos.home_path_para_perfil(perfil)
            for method, path in _MUTACIONES:
                resp = self._call(method, path, perfil, data={"radicado_interno": "EXP-0003"})
                self.assertEqual(resp.status_code, 303, (perfil, method, path))
                location = resp.headers.get("location", "")
                self.assertTrue(
                    location.startswith(home),
                    (perfil, method, path, location),
                )
                self.assertIn("No+tienes+permiso", location.replace(" ", "+"))


if __name__ == "__main__":
    unittest.main()
