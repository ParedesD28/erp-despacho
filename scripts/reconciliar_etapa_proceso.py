#!/usr/bin/env python3
"""Reconcilia procesos.etapa_actual con las etapas de actuaciones.etapa.

Regla (etapas_procesales.calcular_etapa): etapa = la de mayor rango entre las
actuaciones del proceso; "Terminación del Proceso" siempre prevalece. Las
fechas no influyen y los valores fuera del catálogo se ignoran (se reportan).

Solo procesos JURIDICO con al menos una actuación con etapa válida y con rango.
No borra ni modifica actuaciones; únicamente UPDATE procesos.etapa_actual.
Idempotente: tras --apply, una nueva ejecución no reporta diferencias.

Uso:
  # Solo reporta (default; transacción de solo lectura)
  DATABASE_URL=... python scripts/reconciliar_etapa_proceso.py
  DATABASE_URL=... python scripts/reconciliar_etapa_proceso.py --json salida.json

  # Aplica los UPDATE
  DATABASE_URL=... python scripts/reconciliar_etapa_proceso.py --apply

Revisar el reporte antes de usar --apply en producción.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import etapas_procesales  # noqa: E402


def planear(procesos: Iterable[dict[str, Any]], actuaciones: Iterable[dict[str, Any]]) -> dict[str, list[dict]]:
    """Calcula diferencias sin I/O.

    procesos: {"radicado_interno", "tipo_cartera", "etapa_actual"}
    actuaciones: {"radicado_interno", "etapa"}
    Devuelve {"cambios": [...], "etapa_actual_invalida": [...],
              "prejuridico_con_etapa": [...], "etapas_actuacion_invalidas": [...]}.
    """
    por_proceso: dict[str, list[Any]] = defaultdict(list)
    for a in actuaciones:
        por_proceso[a["radicado_interno"]].append(a.get("etapa"))

    cambios: list[dict] = []
    etapa_actual_invalida: list[dict] = []
    prejuridico_con_etapa: list[dict] = []
    etapas_invalidas: list[dict] = []

    for p in procesos:
        rad = p["radicado_interno"]
        actual = p.get("etapa_actual")
        if str(p.get("tipo_cartera") or "").upper() == "PREJURIDICO":
            if actual:
                prejuridico_con_etapa.append({"radicado_interno": rad, "etapa_actual": actual})
            continue
        resultado = etapas_procesales.calcular_resultado(por_proceso.get(rad, []))
        if resultado.invalidos:
            etapas_invalidas.append({"radicado_interno": rad, "valores": list(resultado.invalidos)})
        if actual and actual not in etapas_procesales.RANGO_ETAPA and actual != etapas_procesales.ETAPA_NEUTRA:
            etapa_actual_invalida.append({"radicado_interno": rad, "etapa_actual": actual})
        if resultado.etapa and resultado.etapa != actual:
            cambios.append(
                {
                    "radicado_interno": rad,
                    "antes": actual,
                    "despues": resultado.etapa,
                    "actuaciones": len(por_proceso.get(rad, [])),
                }
            )
    return {
        "cambios": cambios,
        "etapa_actual_invalida": etapa_actual_invalida,
        "prejuridico_con_etapa": prejuridico_con_etapa,
        "etapas_actuacion_invalidas": etapas_invalidas,
    }


def _cargar(cur) -> tuple[list[dict], list[dict]]:
    cur.execute("SELECT radicado_interno, tipo_cartera, etapa_actual FROM procesos ORDER BY radicado_interno")
    procesos = [
        {"radicado_interno": r[0], "tipo_cartera": r[1], "etapa_actual": r[2]} for r in cur.fetchall()
    ]
    cur.execute("SELECT radicado_interno, etapa FROM actuaciones")
    actuaciones = [{"radicado_interno": r[0], "etapa": r[1]} for r in cur.fetchall()]
    return procesos, actuaciones


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Aplica los UPDATE (por defecto solo reporta).")
    parser.add_argument("--json", metavar="RUTA", help="Escribe el reporte completo en JSON.")
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL es obligatoria.", file=sys.stderr)
        return 2

    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        if not args.apply:
            conn.set_session(readonly=True)
        with conn:
            with conn.cursor() as cur:
                if args.apply:
                    # Bloquea los procesos mientras se calcula y aplica.
                    cur.execute("LOCK TABLE procesos IN SHARE ROW EXCLUSIVE MODE")
                procesos, actuaciones = _cargar(cur)
                plan = planear(procesos, actuaciones)
                if args.apply:
                    for c in plan["cambios"]:
                        cur.execute(
                            "UPDATE procesos SET etapa_actual=%s WHERE radicado_interno=%s AND etapa_actual IS DISTINCT FROM %s",
                            (c["despues"], c["radicado_interno"], c["despues"]),
                        )
    finally:
        conn.close()

    print(f"Procesos: {len(procesos)} | actuaciones: {len(actuaciones)}")
    print(f"Procesos con etapa_actual distinta de la derivada: {len(plan['cambios'])}")
    for c in plan["cambios"]:
        print(f"  {c['radicado_interno']}: {c['antes']!r} -> {c['despues']!r}")
    for clave in ("etapa_actual_invalida", "prejuridico_con_etapa", "etapas_actuacion_invalidas"):
        print(f"{clave}: {len(plan[clave])}")
        for fila in plan[clave]:
            print(f"  {fila}")
    print("APLICADO" if args.apply else "DRY-RUN (no se escribió nada; use --apply)")
    if args.json:
        Path(args.json).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
