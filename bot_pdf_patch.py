"""Hace que el endpoint interno del bot use exactamente el mismo PDF oficial de la web."""

import os

import bot_api
import main
from bot_pdf_secure import build_signed_pdf_url


if hasattr(main, "generar_pdf_liquidacion"):
    def _generar_pdf_unificado(inmueble_id, fecha_corte, resultados, resumen, inm_info):
        path = main.generar_pdf_liquidacion(
            inmueble_id, fecha_corte, resultados, resumen, inm_info
        )
        base = (bot_api.PUBLIC_BASE_URL or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
        if not base:
            raise RuntimeError("PUBLIC_BASE_URL/RENDER_EXTERNAL_URL no esta configurada")
        filename = os.path.basename(path)
        return build_signed_pdf_url(filename, base)

    bot_api._generar_pdf = _generar_pdf_unificado
    print("[BOT PDF] Generador unificado con descarga publica firmada activado", flush=True)
else:
    print("[BOT PDF][ALERTA] No se encontró el generador PDF unificado", flush=True)
