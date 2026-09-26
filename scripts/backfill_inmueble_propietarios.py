#!/usr/bin/env python3
"""Backfill idempotente de inmueble_propietarios desde datos ya radicados.

Uso (con DATABASE_URL configurada):
  python scripts/backfill_inmueble_propietarios.py
  python scripts/backfill_inmueble_propietarios.py --dry-run

No elimina ni sobrescribe vínculos existentes.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SQL_CONTACTO = """
INSERT INTO inmueble_propietarios (inmueble_id, contacto_id, es_principal)
SELECT i.id, i.contacto_id, TRUE
FROM inmuebles_ph i
WHERE i.contacto_id IS NOT NULL
ON CONFLICT (inmueble_id, contacto_id) DO NOTHING
"""

SQL_DEMANDADOS = """
INSERT INTO inmueble_propietarios (inmueble_id, contacto_id, es_principal)
SELECT DISTINCT p.inmueble_id, pp.contacto_id, COALESCE(pp.es_principal, FALSE)
FROM procesos p
JOIN proceso_partes pp
  ON pp.radicado_interno = p.radicado_interno
WHERE p.inmueble_id IS NOT NULL
  AND UPPER(pp.rol) = 'DEMANDADO'
ON CONFLICT (inmueble_id, contacto_id) DO NOTHING
"""

SQL_FALTANTES = """
SELECT COUNT(*) AS faltantes
FROM procesos p
JOIN proceso_partes pp
  ON pp.radicado_interno = p.radicado_interno
WHERE p.inmueble_id IS NOT NULL
  AND UPPER(pp.rol) = 'DEMANDADO'
  AND NOT EXISTS (
      SELECT 1
      FROM inmueble_propietarios ip
      WHERE ip.inmueble_id = p.inmueble_id
        AND ip.contacto_id = pp.contacto_id
  )
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo reporta cuántos vínculos faltan; no escribe.",
    )
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL no está configurada.", file=sys.stderr)
        return 2

    import db

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_FALTANTES)
            antes = int(cur.fetchone()[0] or 0)
            print(f"Vínculos demandado→inmueble faltantes antes: {antes}")

            if args.dry_run:
                return 0

            with conn:
                cur.execute(SQL_CONTACTO)
                desde_contacto = cur.rowcount if cur.rowcount is not None else 0
                cur.execute(SQL_DEMANDADOS)
                desde_partes = cur.rowcount if cur.rowcount is not None else 0

            cur.execute(SQL_FALTANTES)
            despues = int(cur.fetchone()[0] or 0)

        print(f"Insertados desde inmuebles_ph.contacto_id: {desde_contacto}")
        print(f"Insertados desde proceso_partes DEMANDADO: {desde_partes}")
        print(f"Vínculos faltantes después: {despues}")
        return 0
    finally:
        conn.release()


if __name__ == "__main__":
    raise SystemExit(main())
