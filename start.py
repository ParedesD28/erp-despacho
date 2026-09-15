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
import security
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
        tasas.prueba_conexion_sfc()
        log_msg("✅ [BACKGROUND]", "Mantenimiento inicial completado")
    except Exception as exc:
        log_msg("⚠️ [BACKGROUND]", f"Aviso en mantenimiento inicial: {exc}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    # El pool de PostgreSQL sigue siendo perezoso; esta llamada solo instala
    # la capa de acceso, no fuerza una conexión contra Neon.
    db.install_psycopg2_pool()

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
