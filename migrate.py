"""Runner de migraciones SQL con control de versiones en `schema_migrations`.

Uso:
    python migrate.py status   # lista aplicadas/pendientes (no escribe)
    python migrate.py check    # exit 1 si hay migraciones pendientes (no escribe)
    python migrate.py apply    # aplica pendientes, en orden, con advisory lock
    python migrate.py apply --dry-run   # ejecuta en transaccion y hace ROLLBACK

Reglas:
- Version = nombre del archivo sin `.sql`. El orden es el lexicografico.
- Cada archivo corre en UNA transaccion (el runner hace commit/rollback; en
  --dry-run todo corre en una sola transaccion que se revierte al final); los
  archivos nuevos no deben incluir BEGIN/COMMIT.
- Las migraciones <= BASELINE_VERSION son legacy: nunca se ejecutan. En una BD
  vacia se aplica `migrations/baseline/<BASELINE_VERSION>_baseline_schema.sql`;
  en una BD con esquema (Neon) el baseline se da por aplicado.
- Un advisory lock de sesion serializa runners concurrentes (varias instancias
  arrancando a la vez).
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT / "migrations"
BASELINE_VERSION = "20261006_usuario_conjuntos_portal_ph"
BASELINE_FILE = MIGRATIONS_DIR / "baseline" / "20261006_baseline_schema.sql"
ADVISORY_LOCK_KEY = 71302599
VERSION_RE = re.compile(r"^\d{8}_[a-z0-9_]+$")


class MigrationError(RuntimeError):
    """Migracion fallida o pendiente; el arranque/despliegue debe abortar."""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def legacy(self) -> bool:
        return self.version <= BASELINE_VERSION


def discover(directory: Path | None = None) -> list[Migration]:
    base = directory or MIGRATIONS_DIR
    items = [Migration(p.stem, p) for p in sorted(base.glob("*.sql"))]
    versions = [m.version for m in items]
    if len(versions) != len(set(versions)):
        raise MigrationError("Versiones de migracion duplicadas")
    return items


def _connect(dsn: str | None = None):
    import psycopg2

    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise MigrationError("DATABASE_URL no está configurada")
    return psycopg2.connect(dsn, connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "15")))


def _ensure_tracker(conn, *, commit: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT")
    if commit:
        conn.commit()


def _applied(conn) -> dict[str, str | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def _is_empty_db(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('public.procesos') IS NULL AND to_regclass('public.contactos') IS NULL"
        )
        return bool(cur.fetchone()[0])


def _tracker_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        return bool(cur.fetchone()[0])


def _acquire_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    conn.commit()


def _release_lock(conn) -> None:
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        conn.commit()
    except Exception:
        pass


def compute_pending(
    migrations: list[Migration], applied: dict[str, str | None]
) -> list[Migration]:
    return [m for m in migrations if not m.legacy and m.version not in applied]


def status(conn=None, migrations: list[Migration] | None = None) -> dict:
    """Estado en solo lectura (no crea el tracker)."""
    own = conn is None
    conn = conn or _connect()
    try:
        migrations = migrations if migrations is not None else discover()
        has_tracker = _tracker_exists(conn)
        applied: dict[str, str | None] = {}
        if has_tracker:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='schema_migrations'"
                )
                cols = {r[0] for r in cur.fetchall()}
                if "checksum" in cols:
                    cur.execute("SELECT version, checksum FROM schema_migrations")
                else:
                    cur.execute("SELECT version, NULL FROM schema_migrations")
                applied = {r[0]: r[1] for r in cur.fetchall()}
        empty = _is_empty_db(conn)
        pending = compute_pending(migrations, applied)
        baseline_ok = BASELINE_VERSION in applied or empty
        changed = [
            m.version
            for m in migrations
            if not m.legacy and applied.get(m.version) and applied[m.version] != m.checksum
        ]
        return {
            "empty_db": empty,
            "baseline_ok": baseline_ok,
            "applied": sorted(applied),
            "pending": [m.version for m in pending],
            "legacy_ignored": [m.version for m in migrations if m.legacy],
            "modified_after_apply": changed,
        }
    finally:
        if own:
            conn.close()


def _run_file(conn, migration: Migration, *, dry_run: bool) -> None:
    sql = migration.sql
    if re.search(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;", sql, flags=re.I | re.M):
        raise MigrationError(
            f"{migration.version}: no debe contener BEGIN/COMMIT/ROLLBACK; el runner gestiona la transacción"
        )
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '30s'")
            cur.execute(sql)
            cur.execute(
                """
                INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)
                ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum
                """,
                (migration.version, migration.checksum),
            )
        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        raise MigrationError(f"{migration.version}: {exc}") from exc


def _apply_baseline(conn, *, dry_run: bool) -> None:
    if not BASELINE_FILE.exists():
        raise MigrationError(f"Falta el baseline {BASELINE_FILE}")
    try:
        with conn.cursor() as cur:
            cur.execute(BASELINE_FILE.read_text(encoding="utf-8"))
            cur.execute("SELECT pg_catalog.set_config('search_path', 'public', false)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT")
            cur.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (BASELINE_VERSION, hashlib.sha256(BASELINE_FILE.read_bytes()).hexdigest()),
            )
        if not dry_run:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        raise MigrationError(f"baseline: {exc}") from exc


def apply_pending(
    conn=None,
    migrations: list[Migration] | None = None,
    *,
    dry_run: bool = False,
    log=print,
) -> list[str]:
    """Aplica migraciones pendientes bajo advisory lock. Devuelve las aplicadas."""
    own = conn is None
    conn = conn or _connect()
    done: list[str] = []
    try:
        _acquire_lock(conn)
        try:
            migrations = migrations if migrations is not None else discover()
            empty = _is_empty_db(conn)
            if empty and not (_tracker_exists(conn) and BASELINE_VERSION in _applied_safe(conn)):
                log(f"[MIGRATE] BD vacía: aplicando baseline {BASELINE_VERSION}")
                _apply_baseline(conn, dry_run=dry_run)
                done.append(f"baseline:{BASELINE_VERSION}")
            _ensure_tracker(conn, commit=not dry_run)
            applied = _applied(conn)
            if BASELINE_VERSION not in applied:
                raise MigrationError(
                    f"La BD tiene esquema pero no está marcada en el baseline {BASELINE_VERSION}. "
                    "Revisar manualmente antes de continuar."
                )
            for migration in compute_pending(migrations, applied):
                log(f"[MIGRATE] {'DRY-RUN ' if dry_run else ''}aplicando {migration.version}")
                _run_file(conn, migration, dry_run=dry_run)
                done.append(migration.version)
            if dry_run:
                conn.rollback()
                log("[MIGRATE] dry-run: transacción revertida, la BD no cambió")
            return done
        finally:
            _release_lock(conn)
    finally:
        if own:
            conn.close()


def _applied_safe(conn) -> dict[str, str | None]:
    try:
        return _applied(conn)
    except Exception:
        conn.rollback()
        return {}


def ensure_up_to_date(mode: str | None = None, log=print) -> None:
    """Guardia de arranque. Lanza MigrationError si el esquema no está al día.

    MIGRATIONS_MODE=check (defecto): falla si hay pendientes.
    MIGRATIONS_MODE=apply: aplica pendientes bajo lock y falla si alguna rompe.
    MIGRATIONS_MODE=off: omite (solo para emergencias/entornos sin BD).
    """
    mode = (mode or os.getenv("MIGRATIONS_MODE") or "check").strip().lower()
    if mode == "off":
        log("[MIGRATE] MIGRATIONS_MODE=off: guardia de migraciones omitida")
        return
    if mode == "apply":
        applied = apply_pending(log=log)
        log(f"[MIGRATE] aplicadas al arrancar: {applied or 'ninguna'}")
    elif mode != "check":
        raise MigrationError(f"MIGRATIONS_MODE inválido: {mode!r}")
    state = status()
    if not state["baseline_ok"]:
        raise MigrationError(
            "[MIGRATIONS] La BD no está marcada en el baseline de migraciones; ejecutar `python migrate.py apply`"
        )
    if state["pending"]:
        raise MigrationError(
            "[MIGRATIONS] Migraciones pendientes: "
            + ", ".join(state["pending"])
            + ". Ejecutar `python migrate.py apply` contra la BD ANTES de desplegar."
        )
    log("[MIGRATE] esquema al día")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "status":
            state = status()
            for key in ("empty_db", "baseline_ok", "pending", "modified_after_apply"):
                print(f"{key}: {state[key]}")
            print(f"applied: {len(state['applied'])}; legacy_ignored: {len(state['legacy_ignored'])}")
            return 0
        if cmd == "check":
            ensure_up_to_date("check")
            return 0
        if cmd == "apply":
            done = apply_pending(dry_run="--dry-run" in argv)
            print(f"[MIGRATE] {'simuladas' if '--dry-run' in argv else 'aplicadas'}: {done or 'ninguna'}")
            return 0
    except MigrationError as exc:
        print(f"[MIGRATE][ERROR] {exc}", file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
