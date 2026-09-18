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
import migracion_expedientes_runtime
import main
import proceso_partes_runtime
import proceso_partes_service
import security
import sms_cartera_runtime
import tasas
from observability import log_msg


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
        liquidador.dedupe_expensas()
        tasas.asegurar_tabla_tasas()
        agenda_service.ensure_schema()
        proceso_partes_service.ensure_schema()
        tasas.prueba_conexion_sfc()
        log_msg("✅ [BACKGROUND]", "Mantenimiento inicial completado")
    except Exception as exc:
        log_msg("⚠️ [BACKGROUND]", f"Aviso en mantenimiento inicial: {exc}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    # El pool de PostgreSQL sigue siendo perezoso; esta llamada solo instala
    # la capa de acceso, no fuerza una conexión contra Neon.
    db.install_psycopg2_pool()

    # La estructura normalizada y la unicidad de contactos deben estar listas
    # antes de aceptar tráfico. Antes se ejecutaban en segundo plano y dejaban
    # una ventana en la que /crear_expediente_completo podía fallar.
    log_msg("🔧 [BOOTSTRAP]", "Verificando esquema de procesos y contactos antes del tráfico...")
    proceso_partes_service.ensure_schema()
    log_msg("✅ [BOOTSTRAP]", "Esquema de procesos y contactos verificado.")

    # Migra de forma transaccional los antiguos EN REPARTO1..6 al esquema
    # ID interno EXP-xxxx + radicado_rama=EN REPARTO antes de aceptar tráfico.
    migracion_expedientes_runtime.migrate_legacy_en_reparto()

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
