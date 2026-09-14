"""Registro único y explícito de extensiones del ERP.

Los módulos legacy conservan su implementación interna para reducir riesgo de
regresión, pero dejan de parchear el framework de forma implícita: el arranque
los registra aquí en un orden determinista.
"""

import main

import compat_routes  # noqa: F401,E402
import feature_routes  # noqa: F401,E402
import data_integrity  # noqa: F401,E402
import route_patches  # noqa: F401,E402
import tasa_patch  # noqa: F401,E402
import expedientes_patch  # noqa: F401,E402
import crm_anular_patch  # noqa: F401,E402
import agent_supervision  # noqa: F401,E402
import export_patches  # noqa: F401,E402
import export_patches_compat  # noqa: F401,E402
import pdf_final_patch  # noqa: F401,E402
import bot_pdf_patch  # noqa: F401,E402
import export_final_patch  # noqa: F401,E402
import expediente_workflow_patch  # noqa: F401,E402
import expediente_cursor_hardening  # noqa: F401,E402
import expediente_workflow_hardening  # noqa: F401,E402
import expediente_stage_hardening  # noqa: F401,E402
import expediente_ui_patch  # noqa: F401,E402

# El middleware legacy de main.py se neutraliza antes de instalar la capa de
# sesión HMAC de start.py. Así existe una única política de autenticación.
import security_architecture_patch  # noqa: F401,E402

import production_checks  # noqa: F401,E402

# El antiguo sitecustomize.py registraba esta ruta por efectos colaterales del
# intérprete. Ahora queda registrada de forma explícita y auditable.
import bot_api  # noqa: E402

if not any(getattr(route, "path", None) == "/api/bot/liquidar" for route in main.app.router.routes):
    main.app.add_api_route(
        "/api/bot/liquidar",
        bot_api.liquidar_para_bot,
        methods=["POST"],
        include_in_schema=True,
    )
    print("[EXTENSIONS] Ruta /api/bot/liquidar registrada explícitamente", flush=True)
