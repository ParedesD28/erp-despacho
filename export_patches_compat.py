"""Compatibilidad final para la ruta PDF: acepta fecha de corte desde formulario."""

from datetime import date, datetime

from fastapi import Form, Request
from fastapi.responses import FileResponse

import main
from export_patches import generar_pdf_liquidacion


main.app.router.routes[:] = [
    r for r in main.app.router.routes
    if not (
        getattr(r, "path", None) == "/liquidador/exportar/pdf"
        and "POST" in getattr(r, "methods", set())
    )
]


@main.app.post("/liquidador/exportar/pdf")
async def exportar_pdf_unificado_final(
    request: Request,
    inmueble_id: int = Form(...),
    tipo_tasa: str = Form(...),
    tasa_fija: float = Form(2.5),
    honorarios_pct: float = Form(23.8),
    gastos: float = Form(0.0),
    fecha_corte: date = Form(...),
):
    resultados, resumen, inm_info = main.motor_calculo_judicial(
        inmueble_id, tipo_tasa, tasa_fija, honorarios_pct, gastos, fecha_corte
    )
    path = generar_pdf_liquidacion(inmueble_id, fecha_corte, resultados, resumen, inm_info)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"Liquidacion_{inmueble_id}_{fecha_corte.isoformat()}.pdf",
    )


print("[EXPORT_PATCHES] Ruta PDF final registrada con fecha_corte de formulario", flush=True)
