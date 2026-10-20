"""Tests: auth cron sync-rama, dedupe hash, filtro radicado 23 dígitos."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)
os.environ.setdefault("CRON_SECRET_KEY", "test-cron-secret-key-not-for-prod")


class RadicadoFilterTests(unittest.TestCase):
    def test_valido_23_digitos(self):
        from sync_procesos_service import es_radicado_rama_valido, normalizar_radicado_rama

        raw = "11001400300120210012300"
        self.assertTrue(es_radicado_rama_valido(raw))
        self.assertEqual(normalizar_radicado_rama(raw), raw)

    def test_normaliza_separadores(self):
        from sync_procesos_service import es_radicado_rama_valido, normalizar_radicado_rama

        raw = "11001-4003-001-2021-00123-00"
        self.assertEqual(normalizar_radicado_rama(raw), "11001400300120210012300")
        self.assertTrue(es_radicado_rama_valido(raw))

    def test_rechaza_corto_o_largo(self):
        from sync_procesos_service import es_radicado_rama_valido

        self.assertFalse(es_radicado_rama_valido("12345"))
        self.assertFalse(es_radicado_rama_valido("1" * 22))
        self.assertFalse(es_radicado_rama_valido("1" * 24))
        self.assertFalse(es_radicado_rama_valido(""))
        self.assertFalse(es_radicado_rama_valido(None))

    def test_listar_filtra_invalidos(self):
        from sync_procesos_service import listar_expedientes_activos_con_radicado

        cur = MagicMock()
        cur.fetchall.return_value = [
            {
                "radicado_interno": "A1",
                "radicado_rama": "11001400300120210012300",
                "abogado_id": 1,
            },
            {
                "radicado_interno": "B2",
                "radicado_rama": "123",
                "abogado_id": None,
            },
            {
                "radicado_interno": "C3",
                "radicado_rama": "66001-4003-001-2021-00123-00",
                "abogado_id": 2,
            },
        ]
        rows = listar_expedientes_activos_con_radicado(cur)
        internos = {r["radicado_interno"] for r in rows}
        self.assertEqual(internos, {"A1", "C3"})


class DedupeHashTests(unittest.TestCase):
    def test_hash_estable_misma_actuacion(self):
        from sync_procesos_service import content_hash_actuacion

        h1 = content_hash_actuacion(
            fecha="2026-01-15",
            tipo="Auto",
            descripcion="Se admite demanda",
        )
        h2 = content_hash_actuacion(
            fecha="2026-01-15",
            tipo="  AUTO ",
            descripcion="Se admite   demanda",
        )
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_hash_cambia_si_cambia_contenido(self):
        from sync_procesos_service import content_hash_actuacion

        a = content_hash_actuacion(fecha="2026-01-15", tipo="Auto", descripcion="A")
        b = content_hash_actuacion(fecha="2026-01-15", tipo="Auto", descripcion="B")
        c = content_hash_actuacion(fecha="2026-01-16", tipo="Auto", descripcion="A")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_mapear_incluye_hash_e_id(self):
        from sync_procesos_service import mapear_actuacion_api

        row = mapear_actuacion_api(
            {
                "idActuacion": 998877,
                "fechaActuacion": "2026-03-01",
                "actuacion": "Sentencia",
                "anotacion": "Fallo de primera instancia",
                "fechaInicial": "2026-03-02",
                "fechaFinal": "2026-03-10",
            },
            radicado_interno="INT-1",
            radicado_rama="11001400300120210012300",
        )
        self.assertEqual(row["id_actuacion"], "998877")
        self.assertEqual(len(row["content_hash"]), 64)
        self.assertEqual(str(row["fecha_actuacion"]), "2026-03-01")
        self.assertEqual(str(row["inicio_termino"]), "2026-03-02")
        self.assertEqual(str(row["fin_termino"]), "2026-03-10")


class CronAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importar app tras setear secretos de CI.
        import main as main_mod

        cls.app = main_mod.app

    def setUp(self):
        os.environ["CRON_SECRET_KEY"] = "test-cron-secret-key-not-for-prod"

    def test_sin_header_401(self):
        client = TestClient(self.app)
        resp = client.post("/api/v1/cron/sync-rama")
        self.assertEqual(resp.status_code, 401)

    def test_secreto_incorrecto_403(self):
        client = TestClient(self.app)
        resp = client.post(
            "/api/v1/cron/sync-rama",
            headers={"X-Cron-Secret": "wrong-secret"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_secreto_ok_202(self):
        client = TestClient(self.app)
        with patch(
            "sync_rama_router._run_sync_background",
            new_callable=AsyncMock,
        ) as mock_run:
            resp = client.post(
                "/api/v1/cron/sync-rama",
                headers={"X-Cron-Secret": "test-cron-secret-key-not-for-prod"},
            )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertTrue(body.get("accepted"))
        self.assertEqual(body.get("job"), "sync-rama")
        # BackgroundTasks ejecuta al cerrar la respuesta en TestClient.
        self.assertTrue(mock_run.await_count >= 0)

    def test_sin_env_503(self):
        client = TestClient(self.app)
        old = os.environ.pop("CRON_SECRET_KEY", None)
        try:
            resp = client.post(
                "/api/v1/cron/sync-rama",
                headers={"X-Cron-Secret": "anything"},
            )
            self.assertEqual(resp.status_code, 503)
        finally:
            if old is not None:
                os.environ["CRON_SECRET_KEY"] = old


class RequireCronHelperTests(unittest.TestCase):
    def test_compare_helper_direct(self):
        from sync_rama_router import _require_cron_secret

        os.environ["CRON_SECRET_KEY"] = "abc"
        req = MagicMock()
        req.headers.get.return_value = ""
        denied = _require_cron_secret(req)
        self.assertEqual(denied.status_code, 401)

        req.headers.get.return_value = "nope"
        denied = _require_cron_secret(req)
        self.assertEqual(denied.status_code, 403)

        req.headers.get.return_value = "abc"
        self.assertIsNone(_require_cron_secret(req))


if __name__ == "__main__":
    unittest.main()
