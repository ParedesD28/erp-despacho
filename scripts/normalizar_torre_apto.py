#!/usr/bin/env python3
"""Normaliza inmuebles_ph.torre_apto al formato canónico `2-42`.

Usa los mismos helpers que el matching de certificados
(`certificados_deuda_repository.normalizar_torre_apto`).

Uso:
  # Solo listar cambios (default; no escribe)
  DATABASE_URL=... python scripts/normalizar_torre_apto.py --dry-run

  # Aplicar UPDATE idempotente
  DATABASE_URL=... python scripts/normalizar_torre_apto.py --apply

NO ejecutar --apply contra producción Neon sin dry-run y revisión de conflictos.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certificados_deuda_repository import (  # noqa: E402
    _claves_unidad,
    _unidad_coincide,
    normalizar_torre_apto,
)


def planear_filas(filas: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Calcula cambios y conflictos sobre una muestra de filas (sin I/O).

    Cada fila: ``{"id", "conjunto_id", "torre_apto"}``.
    Idempotente: canónicos y vacíos no generan cambio; basura sin tokens tampoco.
    """
    cambios: list[dict] = []
    for f in filas:
        actual = (f.get("torre_apto") or "").strip()
        if not actual:
            continue
        canon = normalizar_torre_apto(actual)
        if not canon or canon == actual:
            continue
        cambios.append(
            {
                "id": int(f["id"]),
                "conjunto_id": f.get("conjunto_id"),
                "actual": actual,
                "canonico": canon,
            }
        )

    por_conjunto: dict[object, list[dict]] = defaultdict(list)
    for f in filas:
        por_conjunto[f.get("conjunto_id")].append(f)

    conflictos: list[dict] = []
    ids_cambio = {c["id"]: c for c in cambios}
    for cid, grupo in por_conjunto.items():
        proyectado = []
        for f in grupo:
            tid = int(f["id"])
            torre = (
                ids_cambio[tid]["canonico"]
                if tid in ids_cambio
                else (f.get("torre_apto") or "").strip()
            )
            proyectado.append({"id": tid, "torre_apto": torre})
        for i, a in enumerate(proyectado):
            for b in proyectado[i + 1 :]:
                if not a["torre_apto"] or not b["torre_apto"]:
                    continue
                if a["torre_apto"] == b["torre_apto"] or _unidad_coincide(
                    a["torre_apto"], _claves_unidad(b["torre_apto"])
                ):
                    if a["id"] in ids_cambio or b["id"] in ids_cambio:
                        conflictos.append(
                            {
                                "conjunto_id": cid,
                                "ids": sorted([a["id"], b["id"]]),
                                "claves": [a["torre_apto"], b["torre_apto"]],
                            }
                        )
    return cambios, conflictos


def _conectar():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not url:
        raise SystemExit("Defina DATABASE_URL (o NEON_DATABASE_URL).")
    conn = psycopg2.connect(url)
    return conn, RealDictCursor


def planear(cur) -> tuple[list[dict], list[dict]]:
    cur.execute(
        """
        SELECT id, conjunto_id, torre_apto
        FROM inmuebles_ph
        WHERE coalesce(btrim(torre_apto), '') <> ''
        ORDER BY id
        """
    )
    filas = [dict(r) for r in cur.fetchall()]
    return planear_filas(filas)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Listar cambios sin escribir (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplicar UPDATE. Omite filas en conflicto.",
    )
    args = parser.parse_args()
    apply = bool(args.apply)

    conn, RealDictCursor = _conectar()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cambios, conflictos = planear(cur)
            conflicto_ids = {i for c in conflictos for i in c["ids"]}
            print(f"Filas a normalizar: {len(cambios)}")
            print(f"Conflictos (no auto-update): {len(conflictos)}")
            for c in cambios[:50]:
                skip = " [CONFLICTO]" if c["id"] in conflicto_ids else ""
                print(
                    f"  id={c['id']} conjunto={c['conjunto_id']} "
                    f"{c['actual']!r} → {c['canonico']!r}{skip}"
                )
            if len(cambios) > 50:
                print(f"  … y {len(cambios) - 50} más")
            for conf in conflictos[:20]:
                print(
                    f"  CONFLICTO conjunto={conf['conjunto_id']} "
                    f"ids={conf['ids']} claves={conf['claves']}"
                )

            if not apply:
                print("Dry-run OK. Pase --apply para escribir (omite conflictos).")
                return 0

            aplicados = 0
            for c in cambios:
                if c["id"] in conflicto_ids:
                    continue
                cur.execute(
                    "UPDATE inmuebles_ph SET torre_apto=%s "
                    "WHERE id=%s AND torre_apto IS DISTINCT FROM %s",
                    (c["canonico"], c["id"], c["canonico"]),
                )
                aplicados += cur.rowcount
            conn.commit()
            omitidos = len(conflicto_ids & {c["id"] for c in cambios})
            print(f"Aplicados: {aplicados} (omitidos por conflicto: {omitidos})")
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
