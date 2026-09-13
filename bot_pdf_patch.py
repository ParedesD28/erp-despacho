"""Hace que el endpoint interno del bot use exactamente el mismo PDF oficial de la web."""

import bot_api
import main


if hasattr(main, "generar_pdf_liquidacion"):
    def _generar_pdf_unificado(inmueble_id, fecha_corte, resultados, resumen, inm_info):
        return main.generar_pdf_liquidacion(
            inmueble_id, fecha_corte, resultados, resumen, inm_info
        )

    bot_api._generar_pdf = _generar_pdf_unificado
    print("[BOT PDF] Generador unificado con la descarga web", flush=True)
else:
    print("[BOT PDF][ALERTA] No se encontró el generador PDF unificado", flush=True)
