"""Purga o audita acuerdos_pago en Neon.

Uso (con DATABASE_URL en el entorno):
  python scripts/limpiar_acuerdos_prueba.py --audit
  python scripts/limpiar_acuerdos_prueba.py --purge --confirm "ELIMINAR TODOS"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

import db  # noqa: E402


def _audit(cur) -> None:
    cur.execute("SELECT COUNT(*) FROM acuerdos_pago")
    total = cur.fetchone()[0]
    cur.execute(
        """
        SELECT UPPER(COALESCE(estado,'PENDIENTE')) AS estado, COUNT(*) 
        FROM acuerdos_pago GROUP BY 1 ORDER BY 2 DESC
        """
    )
    print(f"Total acuerdos_pago: {total}")
    for estado, n in cur.fetchall():
        print(f"  {estado}: {n}")

    cur.execute(
        """
        SELECT obligacion_id, fecha_compromiso, COUNT(*) AS n,
               STRING_AGG(id::text, ',' ORDER BY id) AS ids
        FROM acuerdos_pago
        WHERE UPPER(COALESCE(estado,'PENDIENTE')) NOT IN ('ANULADO','ELIMINADO')
          AND obligacion_id IS NOT NULL
        GROUP BY 1, 2
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        LIMIT 30
        """
    )
    dups = cur.fetchall()
    print(f"Grupos duplicados (obligacion+fecha): {len(dups)}")
    for row in dups:
        print(f"  obligacion={row[0]} fecha={row[1]} n={row[2]} ids={row[3]}")


def _purge(cur) -> int:
    cur.execute("SELECT COUNT(*) FROM acuerdos_pago")
    total = int(cur.fetchone()[0] or 0)
    cur.execute(
        """
        SELECT to_regclass('public.acuerdos_pago_cuotas') IS NOT NULL
        """
    )
    if cur.fetchone()[0]:
        cur.execute("DELETE FROM acuerdos_pago_cuotas")
    cur.execute(
        """
        UPDATE vencimientos SET anulado=TRUE
        WHERE tipo='ACUERDO_PAGO'
           OR COALESCE(observaciones,'') ILIKE '%Acuerdo #%'
        """
    )
    cur.execute(
        """
        SELECT to_regclass('public.gestiones_crm') IS NOT NULL
        """
    )
    if cur.fetchone()[0]:
        cur.execute(
            """
            UPDATE gestiones_crm SET anulado=TRUE
            WHERE resumen ILIKE '%[ACUERDO DE PAGO #%'
            """
        )
        try:
            cur.execute(
                """
                UPDATE gestiones_crm SET estado='ANULADO'
                WHERE resumen ILIKE '%[ACUERDO DE PAGO #%'
                """
            )
        except Exception:
            pass
    cur.execute("DELETE FROM acuerdos_pago")
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        print("ERROR: falta DATABASE_URL", file=sys.stderr)
        return 2
    if not args.audit and not args.purge:
        parser.print_help()
        return 1

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                if args.audit:
                    _audit(cur)
                if args.purge:
                    if args.confirm.strip().upper() != "ELIMINAR TODOS":
                        print("ERROR: use --confirm \"ELIMINAR TODOS\"", file=sys.stderr)
                        return 3
                    n = _purge(cur)
                    print(f"Purgados {n} acuerdos_pago (+ cuotas/CRM/vencimientos asociados).")
    finally:
        conn.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
