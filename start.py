"""Startup y arranque productivo del ERP Gestión Judicial sin caídas ni bloqueos."""
from __future__ import annotations

import os
import threading
import time
from dotenv import load_dotenv
import uvicorn

load_dotenv()

import db
import security
import liquidador
import tasas
import main
from observability import _json_log, log_msg


def _ejecutar_mantenimiento_segundo_plano():
    """
    Ejecuta migraciones, sincronización de tasas y verificaciones de esquema en segundo plano
    sin bloquear la apertura del puerto HTTP ni demorar el inicio del servidor web.
    """
    time.sleep(1)  # Breve pausa para asegurar que Uvicorn ya esté respondiendo al balanceador
    log_msg("⚙️ [BACKGROUND]", "Iniciando tareas de verificación y sincronización...")

    # 1. Migrar contraseñas legacy a bcrypt si existen
    try:
        migrated = security.migrate_legacy_passwords(db.POOL)
        if migrated:
            log_msg("🔑 [SEGURIDAD]", f"Migradas {migrated} contraseñas heredadas a bcrypt")
    except Exception as exc:
        log_msg("⚠️ [SEGURIDAD]", f"Aviso en migración de contraseñas: {exc}")

    # 2. Mantenimiento de tablas y sincronización de tasas SFC
    try:
        liquidador.dedupe_expensas()
        tasas.asegurar_tabla_tasas()
        main._ensure_crm_and_vencimientos_schema()
        tasas.prueba_conexion_sfc()
        log_msg("✅ [BACKGROUND]", "Mantenimiento inicial y sincronización de tasas completados")
    except Exception as exc:
        log_msg("⚠️ [BACKGROUND]", f"Aviso en mantenimiento de esquemas/tasas: {exc}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    # 1. Instalar pool de conexiones a la base de datos
    db.install_psycopg2_pool()

    # 2. Lanzar mantenimiento en segundo plano
    hilo_mantenimiento = threading.Thread(target=_ejecutar_mantenimiento_segundo_plano, daemon=True)
    hilo_mantenimiento.start()

    # 3. Arrancar Uvicorn de forma inmediata (puerto disponible en menos de 1 segundo)
    log_msg("🚀 [ARRANQUE]", f"Iniciando ERP en puerto {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port)
