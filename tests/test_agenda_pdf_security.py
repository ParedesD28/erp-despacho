"""Regresiones C1 (rutas agenda) y A1 (PDFs sin auth pública)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

AGENDA_PATHS = {
    "/vencimientos",
    "/acuerdos",
    "/acuerdos/guardar",
    "/acuerdos/cumplir",
    "/acuerdos/anular",
    "/acuerdos/eliminar",
    "/acuerdos/purgar-todos",
    "/vencimientos/guardar",
    "/vencimientos/completar",
    "/vencimientos/anular",
}


def _iter_http_routes(routes, prefix: str = ""):
    for route in routes:
        typ = type(route).__name__
        if typ == "_IncludedRouter":
            yield from _iter_http_routes(route.original_router.routes, prefix)
            continue
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        endpoint = getattr(route, "endpoint", None)
        ep_name = getattr(endpoint, "__name__", None) if endpoint else None
        if path and methods:
            full = prefix + path
            for method in methods:
                if method == "HEAD":
                    continue
                yield method, full, ep_name
        nested = getattr(route, "routes", None)
        if nested is not None:
            yield from _iter_http_routes(nested, prefix + (path or ""))


class AgendaSinDuplicadosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import agenda_service  # noqa: F401 — registra rutas sobre main.app
        import main

        cls.app = main.app

    def test_una_implementacion_por_metodo_y_path_de_agenda(self):
        by_key = defaultdict(list)
        for method, path, ep_name in _iter_http_routes(self.app.router.routes):
            if path not in AGENDA_PATHS:
                continue
            by_key[(method, path)].append(ep_name)

        duplicados = {k: v for k, v in by_key.items() if len(v) > 1}
        self.assertFalse(
            duplicados,
            msg=f"Rutas de agenda duplicadas (método, path): {duplicados}",
        )

        # La lógica rica debe ser la de agenda_service (nombres agenda_*).
        self.assertEqual(by_key.get(("GET", "/vencimientos")), ["agenda_vencimientos"])
        self.assertEqual(by_key.get(("POST", "/acuerdos/guardar")), ["agenda_guardar_acuerdo"])
        self.assertEqual(by_key.get(("POST", "/acuerdos/cumplir")), ["agenda_cumplir_acuerdo"])
        self.assertEqual(by_key.get(("POST", "/vencimientos/guardar")), ["agenda_guardar_vencimiento"])
        self.assertEqual(by_key.get(("POST", "/vencimientos/completar")), ["agenda_completar_vencimiento"])


class PdfAuthTests(unittest.TestCase):
    def test_static_pdfs_no_es_publico(self):
        import main

        self.assertFalse(main._is_public_path("/static/pdfs/secreto.pdf"))
        self.assertTrue(main._is_public_path("/static/css/app.css"))
        self.assertTrue(main._is_public_path("/api/bot/pdf/x.pdf"))

    def test_codigo_no_publica_urls_static_pdfs(self):
        for name in ("api_recaudos.py", "exportaciones.py", "recaudos_service.py", "bot_api.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("/static/pdfs/", source, msg=f"{name} aún referencia /static/pdfs/")
            self.assertNotIn('os.path.join("static", "pdfs"', source)
            self.assertNotIn('Path("static") / "pdfs"', source)

    def test_pdf_storage_fuera_de_static(self):
        import pdf_storage

        with tempfile.TemporaryDirectory() as tmp:
            old = pdf_storage.PDF_STORAGE_DIR
            try:
                pdf_storage.PDF_STORAGE_DIR = Path(tmp) / "private_pdfs"
                path = pdf_storage.resolve_pdf("demo.pdf")
                self.assertEqual(path.name, "demo.pdf")
                self.assertIn("private_pdfs", str(path))
                self.assertNotIn("static", str(path))
            finally:
                pdf_storage.PDF_STORAGE_DIR = old

    def test_middleware_bloquea_static_pdfs_sin_sesion(self):
        """Sin cookie de sesión, /static/pdfs/ no es público (redirect a login)."""
        import asyncio
        import main
        from starlette.requests import Request
        from starlette.responses import Response

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/static/pdfs/secreto.pdf",
            "raw_path": b"/static/pdfs/secreto.pdf",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
        }
        request = Request(scope)

        async def call_next(_request):
            return Response("should-not-run", status_code=200)

        response = asyncio.run(main.production_security_middleware(request, call_next))
        self.assertEqual(response.status_code, 303)
        self.assertIn("/login", response.headers.get("location", ""))

    def test_pdfs_autenticado_sirve_desde_private(self):
        import pdf_storage
        import main

        with tempfile.TemporaryDirectory() as tmp:
            old = pdf_storage.PDF_STORAGE_DIR
            try:
                pdf_storage.PDF_STORAGE_DIR = Path(tmp)
                target = Path(tmp) / "ok.pdf"
                target.write_bytes(b"%PDF-1.4 demo")
                response = main.servir_pdf_autenticado("ok.pdf")
                self.assertEqual(response.media_type, "application/pdf")
                self.assertTrue(str(response.path).endswith("ok.pdf"))
            finally:
                pdf_storage.PDF_STORAGE_DIR = old

            with self.assertRaises(Exception) as ctx:
                main.servir_pdf_autenticado("../etc/passwd")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 404)


if __name__ == "__main__":
    unittest.main()
