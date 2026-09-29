"""Tests de lógica dry-run del script scripts/normalizar_torre_apto.py."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SCRIPT = ROOT / "scripts" / "normalizar_torre_apto.py"
_SPEC = importlib.util.spec_from_file_location("normalizar_torre_apto_script", _SCRIPT)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

planear_filas = _MOD.planear_filas


class PlanearFilasDryRunTests(unittest.TestCase):
    def test_muestras_tipicas(self):
        filas = [
            {"id": 1, "conjunto_id": 9, "torre_apto": "02-042"},
            {"id": 2, "conjunto_id": 9, "torre_apto": "TORRE 1 APTO 201"},
            {"id": 3, "conjunto_id": 9, "torre_apto": "2-42"},  # ya canónico
            {"id": 4, "conjunto_id": 9, "torre_apto": ""},
            {"id": 5, "conjunto_id": 9, "torre_apto": "   "},
            {"id": 6, "conjunto_id": 9, "torre_apto": "###"},
            {"id": 7, "conjunto_id": 9, "torre_apto": "N/A"},
        ]
        cambios, conflictos = planear_filas(filas)
        by_id = {c["id"]: c for c in cambios}
        self.assertEqual(by_id[1]["canonico"], "2-42")
        self.assertEqual(by_id[2]["canonico"], "1-201")
        self.assertNotIn(3, by_id)  # ya canónico
        self.assertNotIn(4, by_id)
        self.assertNotIn(5, by_id)
        self.assertNotIn(6, by_id)  # basura sin tokens → sin canon
        self.assertNotIn(7, by_id)
        # 1 (02-042→2-42) colisiona con 3 (ya 2-42) en el mismo conjunto
        self.assertTrue(any(sorted(c["ids"]) == [1, 3] for c in conflictos))

    def test_idempotente_solo_reformatea(self):
        filas = [
            {"id": 10, "conjunto_id": 1, "torre_apto": "TORRE 02 APTO 042"},
            {"id": 11, "conjunto_id": 1, "torre_apto": "9-401"},
        ]
        cambios, conflictos = planear_filas(filas)
        self.assertEqual(conflictos, [])
        self.assertEqual(
            {(c["id"], c["actual"], c["canonico"]) for c in cambios},
            {(10, "TORRE 02 APTO 042", "2-42")},
        )

    def test_vacio_y_none(self):
        cambios, conflictos = planear_filas(
            [
                {"id": 1, "conjunto_id": None, "torre_apto": None},
                {"id": 2, "conjunto_id": None, "torre_apto": ""},
            ]
        )
        self.assertEqual(cambios, [])
        self.assertEqual(conflictos, [])


if __name__ == "__main__":
    unittest.main()
