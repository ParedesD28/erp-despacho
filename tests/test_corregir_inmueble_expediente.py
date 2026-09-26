"""Regresión: corregir inmueble desde el editor del expediente sin duplicar filas."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

import expedientes_service


class _RecordingCursor:
    def __init__(self, responses=None):
        self.statements = []
        self._responses = list(responses or [])
        self._idx = 0

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.statements.append((normalized, params))

    def fetchone(self):
        if self._idx < len(self._responses):
            value = self._responses[self._idx]
            self._idx += 1
            return value
        return None

    def fetchall(self):
        return []


class CorregirInmuebleExpedienteTests(unittest.TestCase):
    def test_ui_editor_permite_editar_inmueble_sin_boton_corregir(self):
        html = (ROOT / "templates" / "detalle_expediente_v4.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('name="torre_apto"', html)
        self.assertIn('name="conjunto_id"', html)
        self.assertIn("/expediente/guardar-estructurado", html)
        self.assertNotIn("CORREGIR INMUEBLE", html.upper())
        self.assertIn("se reutiliza el inmueble si ya existe", html)

    def test_reutiliza_inmueble_existente_sin_clonar(self):
        """Si ya hay apto en el conjunto, se reutiliza y no se hace INSERT."""
        cur = _RecordingCursor(
            responses=[
                {"id": 7, "nombre": "CONJUNTO DEMO"},  # conjunto
                {"id": 501},  # inmueble existente destino
                None,  # no otro proceso activo
            ]
        )

        def table_exists(cursor, table):
            return table in {
                "inmuebles_ph",
                "conjuntos_residenciales",
                "inmueble_propietarios",
                "acuerdos_pago",
                "vencimientos",
                "gestiones_crm",
            }

        original = expedientes_service._table_exists
        expedientes_service._table_exists = table_exists
        try:
            nuevo = expedientes_service.corregir_inmueble_proceso(
                cur,
                radicado_interno="EXP-0100",
                proceso={
                    "inmueble_id": 300,
                    "inmueble": {
                        "id": 300,
                        "conjunto_id": 7,
                        "conjunto_residencial": "CONJUNTO DEMO",
                        "torre_apto": "TORRE 1 APTO 101",
                    },
                },
                torre_apto="TORRE 2 APTO 202",
                conjunto_id_raw="7",
                contactos_demandados=[
                    {"id": 88, "identificacion": "900100200", "nombre": "DEUDOR"},
                ],
                obligaciones=[{"id": 11}],
            )
        finally:
            expedientes_service._table_exists = original

        self.assertEqual(nuevo, 501)
        inserts = [s for s, _ in cur.statements if "INSERT INTO inmuebles_ph" in s]
        self.assertEqual(inserts, [])
        updates_proceso = [
            (s, p)
            for s, p in cur.statements
            if "UPDATE procesos SET inmueble_id" in s
        ]
        self.assertEqual(len(updates_proceso), 1)
        self.assertEqual(updates_proceso[0][1], (501, "EXP-0100"))
        updates_obl = [
            (s, p)
            for s, p in cur.statements
            if "UPDATE obligaciones" in s and "inmueble_id" in s
        ]
        self.assertEqual(len(updates_obl), 1)
        self.assertEqual(updates_obl[0][1], [501, 11])
        props = [
            p
            for s, p in cur.statements
            if "INSERT INTO inmueble_propietarios" in s
        ]
        self.assertEqual(props, [(501, 88, True)])
        limpia = [
            p
            for s, p in cur.statements
            if "DELETE FROM inmueble_propietarios" in s
        ]
        self.assertEqual(len(limpia), 1)
        self.assertEqual(limpia[0][0], 300)

    def test_actualiza_en_sitio_si_solo_este_proceso_usa_inmueble(self):
        """Evita huérfano: tipografía en apto exclusivo se corrige con UPDATE."""
        cur = _RecordingCursor(
            responses=[
                {"id": 7, "nombre": "CONJUNTO DEMO"},
                None,  # no existe destino
                {"n": 1},  # solo este proceso
            ]
        )

        def table_exists(cursor, table):
            return table in {
                "inmuebles_ph",
                "conjuntos_residenciales",
                "inmueble_propietarios",
            }

        original = expedientes_service._table_exists
        expedientes_service._table_exists = table_exists
        try:
            nuevo = expedientes_service.corregir_inmueble_proceso(
                cur,
                radicado_interno="EXP-0100",
                proceso={
                    "inmueble_id": 300,
                    "inmueble": {
                        "id": 300,
                        "conjunto_id": 7,
                        "conjunto_residencial": "CONJUNTO DEMO",
                        "torre_apto": "TORRE 1 APTO 10l",
                    },
                },
                torre_apto="TORRE 1 APTO 101",
                conjunto_id_raw="7",
                contactos_demandados=[{"id": 88}],
                obligaciones=[{"id": 11}],
            )
        finally:
            expedientes_service._table_exists = original

        self.assertEqual(nuevo, 300)
        updates = [
            p
            for s, p in cur.statements
            if "UPDATE inmuebles_ph" in s and "torre_apto" in s
        ]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], "TORRE 1 APTO 101")
        inserts = [s for s, _ in cur.statements if "INSERT INTO inmuebles_ph" in s]
        self.assertEqual(inserts, [])
        # Mismo id: no debe reasignar proceso
        reasign = [s for s, _ in cur.statements if "UPDATE procesos SET inmueble_id" in s]
        self.assertEqual(reasign, [])

    def test_rechaza_si_destino_tiene_otro_proceso_activo(self):
        cur = _RecordingCursor(
            responses=[
                {"id": 7, "nombre": "CONJUNTO DEMO"},
                {"id": 501},
                {"radicado_interno": "EXP-0099", "estado": "Activo"},
            ]
        )

        def table_exists(cursor, table):
            return table in {"inmuebles_ph", "conjuntos_residenciales"}

        original = expedientes_service._table_exists
        expedientes_service._table_exists = table_exists
        try:
            with self.assertRaises(ValueError) as ctx:
                expedientes_service.corregir_inmueble_proceso(
                    cur,
                    radicado_interno="EXP-0100",
                    proceso={
                        "inmueble_id": 300,
                        "inmueble": {
                            "id": 300,
                            "conjunto_id": 7,
                            "conjunto_residencial": "CONJUNTO DEMO",
                            "torre_apto": "TORRE 1 APTO 101",
                        },
                    },
                    torre_apto="TORRE 2 APTO 202",
                    conjunto_id_raw="7",
                    contactos_demandados=[{"id": 88}],
                    obligaciones=[{"id": 11}],
                )
        finally:
            expedientes_service._table_exists = original

        self.assertIn("EXP-0099", str(ctx.exception))
        self.assertIn("ya tiene el proceso activo", str(ctx.exception))
        updates = [s for s, _ in cur.statements if "UPDATE procesos SET inmueble_id" in s]
        self.assertEqual(updates, [])

    def test_mismo_inmueble_solo_reconcilia_propietarios(self):
        cur = _RecordingCursor(
            responses=[{"id": 7, "nombre": "CONJUNTO DEMO"}]
        )

        def table_exists(cursor, table):
            return table in {
                "inmuebles_ph",
                "conjuntos_residenciales",
                "inmueble_propietarios",
            }

        original = expedientes_service._table_exists
        expedientes_service._table_exists = table_exists
        try:
            nuevo = expedientes_service.corregir_inmueble_proceso(
                cur,
                radicado_interno="EXP-0100",
                proceso={
                    "inmueble_id": 300,
                    "inmueble": {
                        "id": 300,
                        "conjunto_id": 7,
                        "conjunto_residencial": "CONJUNTO DEMO",
                        "torre_apto": "TORRE 1 APTO 101",
                    },
                },
                torre_apto="TORRE 1 APTO 101",
                conjunto_id_raw="7",
                contactos_demandados=[
                    {"id": 88},
                    {"id": 89},
                ],
                obligaciones=[{"id": 11}],
            )
        finally:
            expedientes_service._table_exists = original

        self.assertEqual(nuevo, 300)
        props = [
            p
            for s, p in cur.statements
            if "INSERT INTO inmueble_propietarios" in s
        ]
        self.assertEqual(props, [(300, 88, True), (300, 89, False)])
        self.assertFalse(
            any("UPDATE procesos SET inmueble_id" in s for s, _ in cur.statements)
        )

    def test_handler_guardar_estructurado_invoca_corregir_inmueble(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("corregir_inmueble_proceso", source)
        self.assertIn('form.get("torre_apto")', source)
        self.assertIn("except ValueError as exc", source)


if __name__ == "__main__":
    unittest.main()
