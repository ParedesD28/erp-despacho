"""Hace que el endpoint interno del bot use exactamente el mismo PDF oficial de la web."""

import os

import bot_api
import main


if hasattr(main, "generar_pdf_liquidacion"):
    def _generar_pdf_unificado(inmueble_id, fecha_corte, resultados, resumen, inm_info):
        path = main.generar_pdf_liquidacion(
            inmueble_id, fecha_corte, resultados, resumen, inm_info
        )
        base = (bot_api.PUBLIC_BASE_URL or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
        if not base:
            raise RuntimeError("PUBLIC_BASE_URL/RENDER_EXTERNAL_URL no esta configurada")
        filename = os.path.basename(path)
        return f"{base}/static/pdfs/{filename}"

    bot_api._generar_pdf = _generar_pdf_unificado
    print("[BOT PDF] Generador unificado con la descarga web | URL publica preservada", flush=True)
else:
    print("[BOT PDF][ALERTA] No se encontró el generador PDF unificado", flush=True)
