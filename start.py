"""Startup y arranque productivo del ERP Gestión Judicial."""
from __future__ import annotations

import os
from dotenv import load_dotenv
import uvicorn

load_dotenv()

import db
import security
import liquidador
import tasas
import main
from observability import _json_log

# 1. Instalar pool de conexiones para que cualquier connect() pase por ThreadedConnectionPool
db.install_psycopg2_pool()

# 2. Migrar contraseñas legacy a bcrypt si existen
try:
    migrated = security.migrate_legacy_passwords(db.POOL)
    if migrated:
        _json_log("INFO", "password_migration", migrated_users=migrated)
except Exception as exc:
    _json_log("ERROR", "password_migration_failed", error=str(exc))

# 3. Mantenimiento y verificación inicial de esquemas
try:
    liquidador.dedupe_expensas()
    tasas.asegurar_tabla_tasas()
    main._ensure_crm_and_vencimientos_schema()
    tasas.prueba_conexion_sfc()
    _json_log("INFO", "startup_maintenance_completed")
except Exception as exc:
    _json_log("ERROR", "startup_maintenance_failed", error=str(exc))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    print(f"🚀 Iniciando ERP Gestión Judicial en el puerto {port}...", flush=True)
    uvicorn.run("main:app", host="0.0.0.0", port=port)
