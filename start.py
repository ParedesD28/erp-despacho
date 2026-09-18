"""Punto único de arranque del ERP Gestión Judicial.

start.py no contiene rutas ni reglas de negocio. La aplicación y sus rutas
viven en main.py; agenda_service.py concentra exclusivamente la lógica de
agenda/vencimientos y se registra una sola vez durante el bootstrap.
"""
from __future__ import annotations

import os
import threading
import time

from dotenv import load_dotenv
import uvicorn

load_dotenv()

import agenda_service
import db
import liquidador
import main
import proceso_partes_runtime
import proceso_partes_service
import security
import sms_cartera_runtime
import sms_saldo_service
import sms_router
import tasas
import schema_preflight
from observability import log_msg

def _verificar_dependencias_sms() -> None:
    """Verificación de contrato SMS antes de aceptar tráfico."""
    required = {
        "sms_router.router": getattr(sms_router, "router", None),
        "sms_router._candidatos_cartera": getattr(sms_router, "_candidatos_cartera", None),
        "sms_router._reclamar_lote": getattr(sms_router, "_reclamar_lote", None),
        "sms_saldo_service.calcular_saldo_ph": getattr(sms_saldo_service, "calcular_saldo_ph", None),
        "sms_saldo_service.enriquecer_candidatos": getattr(sms_saldo_service, "enriquecer_candidatos", None),
        "sms_saldo_service.actualizar_item_cola": getattr(sms_saldo_service, "actualizar_item_cola", None),
        "sms_cartera_runtime.install": getattr(sms_cartera_runtime, "install", None),
    }
    faltantes = [nombre for nombre, obj in required.items() if obj is None]
    if faltantes:
        raise RuntimeError(
            "[SMS PREFLIGHT] Dependencias SMS ausentes: " + ", ".join(faltantes)
        )



def _ejecutar_mantenimiento_segundo_plano() -> None:
    """Tareas idempotentes de mantenimiento que no deben bloquear el arranque."""
    time.sleep(1)
    log_msg("⚙️ [BACKGROUND]", "Iniciando tareas de verificación y sincronización...")

    try:
        migrated = security.migrate_legacy_passwords(db.POOL)
        if migrated:
            log_msg("🔑 [SEGURIDAD]", f"Migradas {migrated} contraseñas heredadas a bcrypt")
    except Exception as exc:
        log_msg("⚠️ [SEGURIDAD]", f"Aviso en migración de contraseñas: {exc}")

    try:
        # Las operaciones estructurales/destructivas dejaron de ejecutarse en startup.
        # El mantenimiento de expensas y las migraciones se ejecutan de forma explícita.
        tasas.prueba_conexion_sfc()
        log_msg("✅ [BACKGROUND]", "Verificaciones externas completadas")
    except Exception as exc:
        log_msg("⚠️ [BACKGROUND]", f"Aviso en verificación externa: {exc}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    # El pool de PostgreSQL sigue siendo perezoso; esta llamada solo instala
    # la capa de acceso, no fuerza una conexión contra Neon.
    db.install_psycopg2_pool()

    log_msg("🔧 [SCHEMA PREFLIGHT]", "Verificando esquema productivo en solo lectura...")
    _verificar_dependencias_sms()
    schema_preflight.verify()
    log_msg("✅ [SCHEMA PREFLIGHT]", "Esquema productivo verificado; no se ejecutan DDL al arrancar.")

    # Las lecturas normalizadas se activan antes de aceptar tráfico.
    proceso_partes_runtime.install()

    # SMS usa contactos + proceso_partes como fuente adicional de candidatos,
    # sin eliminar el flujo existente de propiedad horizontal.
    sms_cartera_runtime.install()

    # Registro único de las rutas de agenda sobre main.app. No hay lógica de
    # negocio ni duplicación de endpoints dentro del arranque.
    agenda_service.register_routes()

    hilo_mantenimiento = threading.Thread(
        target=_ejecutar_mantenimiento_segundo_plano,
        daemon=True,
        name="erp-mantenimiento",
    )
    hilo_mantenimiento.start()

    log_msg("🚀 [ARRANQUE]", f"Iniciando ERP en puerto {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port)
