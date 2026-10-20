"""Unit tests for external_legal_client.LegalAPIClient (httpx + respx)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import respx
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Base URL de prueba — host ficticio, sin proveedor real.
TEST_BASE = "https://legal-api.test.local"


def _set_env(**overrides: str) -> None:
    os.environ["LEGAL_API_BASE_URL"] = TEST_BASE
    os.environ["LEGAL_API_MAX_ATTEMPTS"] = "3"
    os.environ["LEGAL_API_RETRY_MIN_WAIT_S"] = "0.01"
    os.environ["LEGAL_API_RETRY_MAX_WAIT_S"] = "0.05"
    # Limpiar headers extra / UA salvo que el test los pase explícitamente.
    if "LEGAL_API_EXTRA_HEADERS" not in overrides:
        os.environ.pop("LEGAL_API_EXTRA_HEADERS", None)
    if "LEGAL_API_USER_AGENT" not in overrides:
        os.environ.pop("LEGAL_API_USER_AGENT", None)
    for key, value in overrides.items():
        os.environ[key] = value


class ModelosTests(unittest.TestCase):
    def test_detalle_aliases_y_extra_ignore(self):
        from external_legal_client import ProcesoDetalleResponse

        m = ProcesoDetalleResponse.model_validate(
            {
                "despacho": "Juzgado 01 Civil",
                "fechaProceso": "2020-01-15T00:00:00",
                "ponentes": "DRA. X",
                "estado": "Activo",
                "campoDesconocido": 123,
            }
        )
        self.assertEqual(m.juzgado, "Juzgado 01 Civil")
        self.assertEqual(m.fecha_creacion, "2020-01-15T00:00:00")
        self.assertEqual(m.ponente, "DRA. X")
        self.assertEqual(m.estado, "Activo")
        self.assertFalse(hasattr(m, "campoDesconocido"))

    def test_actuacion_y_sujeto_aliases(self):
        from external_legal_client import ActuacionResponse, SujetoResponse

        a = ActuacionResponse.model_validate(
            {
                "fechaActuacion": "2024-06-01",
                "anotacion": "Auto",
                "fechaInicial": "2024-06-02",
                "fechaFinal": "2024-06-10",
            }
        )
        self.assertEqual(a.fecha_actuacion, "2024-06-01")
        self.assertEqual(a.inicio_termino, "2024-06-02")
        self.assertEqual(a.fin_termino, "2024-06-10")

        s = SujetoResponse.model_validate(
            {"tipoSujeto": "Demandado", "nombreRazonSocial": "ACME SAS"}
        )
        self.assertEqual(s.tipo_sujeto, "Demandado")
        self.assertEqual(s.nombre, "ACME SAS")


class LegalAPIClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _set_env()

    async def test_missing_base_url(self) -> None:
        from external_legal_client import LegalAPIClient

        os.environ.pop("LEGAL_API_BASE_URL", None)
        with self.assertRaises(HTTPException) as ctx:
            LegalAPIClient()
        self.assertEqual(ctx.exception.status_code, 503)

    @respx.mock
    async def test_consultar_id_por_radicado(self) -> None:
        from external_legal_client import LegalAPIClient

        route = respx.get(
            f"{TEST_BASE}/Procesos/Consulta/NumRadicado",
            params={"numero": "11001400300120210012300", "SoloActivos": "false"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "procesos": [{"idProceso": 987654321, "llaveProceso": "x"}],
                    "paginacion": {
                        "cantidadRegistros": 1,
                        "registrosPagina": 20,
                        "cantidadPaginas": 1,
                        "pagina": 1,
                    },
                },
            )
        )

        async with LegalAPIClient(
            retry_min_wait=0.01,
            retry_max_wait=0.02,
            max_attempts=3,
        ) as client:
            id_interno = await client.consultar_id_por_radicado(
                "11001400300120210012300"
            )

        self.assertEqual(id_interno, 987654321)
        self.assertTrue(route.called)
        req = route.calls.last.request
        accept = req.headers.get("Accept", "")
        self.assertIn("*/*", accept)
        self.assertIn("application/json", accept)
        self.assertNotIn("Content-Type", req.headers)
        self.assertEqual(req.headers.get("Accept-Language"), "es-ES,es;q=0.9")
        ua = req.headers.get("User-Agent", "")
        self.assertIn("Mozilla/", ua)
        self.assertIn("Chrome/", ua)

    @respx.mock
    async def test_accept_header_flexible_negotiation(self) -> None:
        """El cliente debe negociar Accept con */* (evita 406 en APIs estrictas)."""
        from external_legal_client import DEFAULT_ACCEPT, LegalAPIClient

        self.assertIn("*/*", DEFAULT_ACCEPT)

        route = respx.get(
            url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"procesos": [{"idProceso": 1}]},
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            await client.consultar_id_por_radicado("11001400300120210012300")

        accept = route.calls.last.request.headers.get("Accept", "")
        self.assertIn("*/*", accept)
        self.assertTrue(
            accept.startswith("application/json") or "application/json" in accept
        )

    @respx.mock
    async def test_default_user_agent_browser_like(self) -> None:
        """Default UA estilo navegador (Mozilla/Chrome); override vía LEGAL_API_USER_AGENT."""
        from external_legal_client import DEFAULT_USER_AGENT, LegalAPIClient

        self.assertIn("Mozilla/", DEFAULT_USER_AGENT)
        self.assertIn("Chrome/", DEFAULT_USER_AGENT)

        route = respx.get(
            url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"procesos": [{"idProceso": 42}]},
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            await client.consultar_id_por_radicado("11001400300120210012300")

        ua = route.calls.last.request.headers.get("User-Agent", "")
        self.assertIn("Mozilla/", ua)
        self.assertIn("Chrome/", ua)

        with patch.dict(
            os.environ,
            {"LEGAL_API_USER_AGENT": "CustomLegalAgent/2.0"},
            clear=False,
        ):
            route.reset()
            async with LegalAPIClient(base_url=TEST_BASE) as client:
                await client.consultar_id_por_radicado("11001400300120210012300")
            self.assertEqual(
                route.calls.last.request.headers.get("User-Agent"),
                "CustomLegalAgent/2.0",
            )

    @respx.mock
    async def test_http_406_incluye_snippet_del_cuerpo(self) -> None:
        from external_legal_client import LegalAPIClient

        respx.get(url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado").mock(
            return_value=httpx.Response(
                406,
                text="Not Acceptable: only text/html supported",
                headers={"Content-Type": "text/plain"},
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            with self.assertRaises(HTTPException) as ctx:
                await client.consultar_id_por_radicado("11001400300120210012300")
        self.assertEqual(ctx.exception.status_code, 502)
        detail = str(ctx.exception.detail)
        self.assertIn("HTTP 406", detail)
        self.assertIn("Not Acceptable", detail)

    @respx.mock
    async def test_respuesta_html_pide_base_rest(self) -> None:
        from external_legal_client import LegalAPIClient

        html = (
            "<!DOCTYPE html><html><head><title>Login</title></head>"
            "<body><form>Iniciar sesión</form></body></html>"
        )
        respx.get(url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado").mock(
            return_value=httpx.Response(
                200,
                text=html,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            with self.assertRaises(HTTPException) as ctx:
                await client.consultar_id_por_radicado("66001418900220260028000")
        self.assertEqual(ctx.exception.status_code, 502)
        detail = str(ctx.exception.detail)
        self.assertIn("HTML", detail)
        self.assertIn("LEGAL_API_BASE_URL", detail)
        self.assertIn("REST", detail)
        self.assertIn("HTTP 200", detail)
        self.assertIn("Content-Type:", detail)
        self.assertIn("text/html", detail)
        self.assertIn("Cuerpo:", detail)

    @respx.mock
    async def test_respuesta_no_json_incluye_diagnostico(self) -> None:
        from external_legal_client import LegalAPIClient

        body = "Gateway Timeout — upstream closed connection unexpectedly " + ("x" * 400)
        respx.get(url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado").mock(
            return_value=httpx.Response(
                200,
                text=body,
                headers={"Content-Type": "text/plain"},
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            with self.assertRaises(HTTPException) as ctx:
                await client.consultar_id_por_radicado("11001400300120210012300")
        detail = str(ctx.exception.detail)
        self.assertIn("no json", detail.lower())
        self.assertIn("HTTP 200", detail)
        self.assertIn("Content-Type:", detail)
        self.assertIn("text/plain", detail)
        self.assertIn("Cuerpo:", detail)
        self.assertLessEqual(len(detail), 900)

    @respx.mock
    async def test_busqueda_array_raiz_y_aliases_id(self) -> None:
        from external_legal_client import LegalAPIClient

        respx.get(url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado").mock(
            return_value=httpx.Response(
                200,
                json=[{"id": "555666777", "llaveProceso": "abc"}],
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            id_interno = await client.consultar_id_por_radicado(
                "11001400300120210012300"
            )
        self.assertEqual(id_interno, 555666777)

    @respx.mock
    async def test_busqueda_objeto_con_id_top_level(self) -> None:
        from external_legal_client import LegalAPIClient

        respx.get(url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado").mock(
            return_value=httpx.Response(
                200,
                json={"id_interno": 112233, "llaveProceso": "z"},
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            id_interno = await client.consultar_id_por_radicado(
                "11001400300120210012300"
            )
        self.assertEqual(id_interno, 112233)

    @respx.mock
    async def test_extra_headers_desde_env(self) -> None:
        from external_legal_client import LegalAPIClient

        _set_env(
            LEGAL_API_EXTRA_HEADERS='{"Authorization":"Bearer test-token","X-Api-Key":"k1"}'
        )
        route = respx.get(
            url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"procesos": [{"idProceso": 9}]},
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            await client.consultar_id_por_radicado("11001400300120210012300")
        headers = route.calls.last.request.headers
        self.assertEqual(headers.get("Authorization"), "Bearer test-token")
        self.assertEqual(headers.get("X-Api-Key"), "k1")
        # Defaults siguen presentes.
        self.assertIn("application/json", headers.get("Accept", ""))

    @respx.mock
    async def test_consultar_radicado_vacio(self) -> None:
        from external_legal_client import LegalAPIClient

        async with LegalAPIClient(base_url=TEST_BASE) as client:
            with self.assertRaises(HTTPException) as ctx:
                await client.consultar_id_por_radicado("  ")
        self.assertEqual(ctx.exception.status_code, 400)

    @respx.mock
    async def test_consultar_sin_resultados(self) -> None:
        from external_legal_client import LegalAPIClient

        respx.get(url__startswith=f"{TEST_BASE}/Procesos/Consulta/NumRadicado").mock(
            return_value=httpx.Response(
                200,
                json={"procesos": [], "paginacion": {"cantidadRegistros": 0}},
            )
        )
        async with LegalAPIClient(base_url=TEST_BASE) as client:
            with self.assertRaises(HTTPException) as ctx:
                await client.consultar_id_por_radicado("000")
        self.assertEqual(ctx.exception.status_code, 404)

    @respx.mock
    async def test_obtener_detalle_actuaciones_sujetos(self) -> None:
        from external_legal_client import LegalAPIClient

        pid = 42
        respx.get(f"{TEST_BASE}/Proceso/Detalle/{pid}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "idProceso": pid,
                    "despacho": "Juzgado 02",
                    "fechaProceso": "2021-03-01",
                    "estado": "Activo",
                    "ponentes": "DR. Y",
                },
            )
        )
        respx.get(f"{TEST_BASE}/Proceso/Actuaciones/{pid}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "actuaciones": [
                        {
                            "fechaActuacion": "2024-01-01",
                            "anotacion": "Auto admisorio",
                            "fechaInicial": None,
                            "fechaFinal": None,
                        }
                    ]
                },
            )
        )
        respx.get(f"{TEST_BASE}/Proceso/SujetosProcesales/{pid}").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "tipoSujeto": "Demandante",
                        "nombreRazonSocial": "PH Ejemplo",
                    }
                ],
            )
        )

        async with LegalAPIClient(base_url=TEST_BASE) as client:
            detalle = await client.obtener_detalle(pid)
            actuaciones = await client.obtener_actuaciones(pid)
            sujetos = await client.obtener_sujetos(pid)

        self.assertEqual(detalle.juzgado, "Juzgado 02")
        self.assertEqual(detalle.fecha_creacion, "2021-03-01")
        self.assertEqual(detalle.estado, "Activo")
        self.assertEqual(detalle.ponente, "DR. Y")
        self.assertEqual(len(actuaciones), 1)
        self.assertEqual(actuaciones[0].anotacion, "Auto admisorio")
        self.assertEqual(len(sujetos), 1)
        self.assertEqual(sujetos[0].nombre, "PH Ejemplo")

    @respx.mock
    async def test_retry_on_503_then_success(self) -> None:
        from external_legal_client import LegalAPIClient

        route = respx.get(f"{TEST_BASE}/Proceso/Detalle/1").mock(
            side_effect=[
                httpx.Response(503, json={"error": "unavailable"}),
                httpx.Response(503, json={"error": "unavailable"}),
                httpx.Response(
                    200,
                    json={"despacho": "OK", "fechaProceso": "2020-01-01"},
                ),
            ]
        )
        async with LegalAPIClient(
            base_url=TEST_BASE,
            max_attempts=3,
            retry_min_wait=0.01,
            retry_max_wait=0.02,
        ) as client:
            detalle = await client.obtener_detalle(1)

        self.assertEqual(detalle.juzgado, "OK")
        self.assertEqual(route.call_count, 3)

    @respx.mock
    async def test_retry_exhausted_raises_http_exception(self) -> None:
        from external_legal_client import LegalAPIClient

        respx.get(f"{TEST_BASE}/Proceso/Detalle/1").mock(
            return_value=httpx.Response(502, text="bad gateway")
        )
        async with LegalAPIClient(
            base_url=TEST_BASE,
            max_attempts=3,
            retry_min_wait=0.01,
            retry_max_wait=0.02,
        ) as client:
            with self.assertRaises(HTTPException) as ctx:
                await client.obtener_detalle(1)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("no disponible", ctx.exception.detail.lower())

    @respx.mock
    async def test_read_timeout_retried_then_504(self) -> None:
        from external_legal_client import LegalAPIClient

        route = respx.get(f"{TEST_BASE}/Proceso/Detalle/9").mock(
            side_effect=httpx.ReadTimeout("read timed out")
        )
        async with LegalAPIClient(
            base_url=TEST_BASE,
            max_attempts=3,
            retry_min_wait=0.01,
            retry_max_wait=0.02,
        ) as client:
            with self.assertRaises(HTTPException) as ctx:
                await client.obtener_detalle(9)
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertEqual(route.call_count, 3)

    async def test_context_manager_closes_owned_client(self) -> None:
        from external_legal_client import LegalAPIClient

        client = LegalAPIClient(base_url=TEST_BASE)
        mock_close = AsyncMock()
        with patch.object(client._client, "aclose", mock_close):
            async with client:
                pass
        mock_close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
