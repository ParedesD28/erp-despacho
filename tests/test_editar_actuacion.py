"""Editar actuaciones desde la memoria del expediente."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

from jinja2 import Environment

import desviaciones_procesales_service as dps
import etapas_procesales as ep
import expedientes_service

E3, E5 = ep.ETAPAS_PROCESALES[2], ep.ETAPAS_PROCESALES[4]


class TemplateEditarActuacionTests(unittest.TestCase):
    def setUp(self):
        self.detalle = (ROOT / "templates" / "detalle_expediente_v4.html").read_text(
            encoding="utf-8"
        )

    def test_editar_junto_a_eliminar_en_memoria(self):
        _, resto = self.detalle.split("Memoria del expediente", 1)
        memoria, despues = resto.split("Reloj Parte vs Juzgado", 1)
        reloj, crm = despues.split(">CRM<", 1)
        self.assertIn(">Editar<", memoria)
        self.assertIn("/actuacion/eliminar", memoria)
        self.assertIn(">Eliminar<", memoria)
        self.assertIn("abrirEditarActuacion", memoria)
        self.assertLess(memoria.index(">Editar<"), memoria.index(">Eliminar<"))
        self.assertNotIn(">Editar<", reloj)
        self.assertNotIn("/actuacion/editar", reloj)
        self.assertNotIn(">Editar<", crm.split("{% else %}", 1)[0])
        self.assertIn("act-edit-modal", self.detalle)
        self.assertIn('action="/actuacion/editar"', self.detalle)
        self.assertIn("Guardar cambios", self.detalle)
        modal = self.detalle.split('id="act-edit-modal"', 1)[1]
        for campo in ('name="fecha"', 'name="etapa"', 'name="descripcion"'):
            self.assertIn(campo, modal)

    def test_render_muestra_editar_y_eliminar_en_manual(self):
        start = self.detalle.index('<section class="grid grid-cols-1 lg:grid-cols-3 gap-5 pb-10">')
        aside_start = self.detalle.index('<aside class="space-y-5">', start)
        bloque = self.detalle[start:aside_start]

        class _Proc:
            radicado_interno = "EXP-0003"

        html = Environment().from_string(bloque).render(
            timeline=[
                {
                    "id": 51,
                    "actuacion_id": None,
                    "fecha": date(2026, 3, 3),
                    "etapa": E5,
                    "descripcion": "Auto de Rama",
                    "es_manual": False,
                    "origen": "sync",
                },
                {
                    "id": 50,
                    "actuacion_id": 12,
                    "fecha": date(2026, 3, 2),
                    "etapa": E3,
                    "descripcion": "Se admite la demanda",
                    "es_manual": True,
                    "origen": "manual",
                },
            ],
            actuaciones=[],
            proceso=_Proc(),
        )
        self.assertEqual(html.count(">Editar<"), 1)
        self.assertEqual(html.count(">Eliminar<"), 1)
        self.assertIn('data-id="12"', html)
        self.assertIn("Se admite la demanda", html)
        self.assertIn("Auto de Rama", html)
        self.assertLess(html.index(">Editar<"), html.index(">Eliminar<"))
        self.assertLess(html.index("Auto de Rama"), html.index("Se admite la demanda"))

    def test_modal_editar_no_mezcla_reloj_crm_tareas(self):
        modal = self.detalle.split('id="act-edit-modal"', 1)[1].split("agenda-modal", 1)[0]
        self.assertNotIn("Reloj", modal)
        self.assertNotIn("CRM", modal)
        self.assertNotIn("/tareas", modal)
        self.assertNotIn("data-tareas-hechas", modal)


class EndpointEditarActuacionTests(unittest.TestCase):
    def test_ruta_registrada(self):
        import main

        rutas = [
            (getattr(r, "path", None), getattr(r, "methods", None) or set())
            for r in main.app.routes
        ]
        self.assertTrue(any(p == "/actuacion/editar" and "POST" in methods for p, methods in rutas))
        self.assertTrue(any(p == "/actuacion/eliminar" and "POST" in methods for p, methods in rutas))


class EspejoManualTests(unittest.TestCase):
    def test_actualiza_fila_migracion_sin_duplicar(self):
        cur = _EspejoCursor()
        with patch.object(dps, "procesar_actuacion", return_value={"ok": True}) as proc:
            ap_id = dps.actualizar_espejo_manual(
                cur,
                actuacion_id=3,
                radicado="EXP-1",
                fecha="2026-04-02",
                etapa=ep.ETAPAS_PROCESALES[3],
                descripcion="Texto nuevo",
            )
        self.assertEqual(ap_id, 5)
        self.assertEqual(len(cur.rows), 1)
        fila = cur.rows[0]
        self.assertEqual(fila["content_hash"], "migracion:3")
        self.assertEqual(fila["descripcion"], "Texto nuevo")
        self.assertEqual(fila["fecha_actuacion"], date(2026, 4, 2))
        self.assertEqual(fila["etapa_asociada"], ep.ETAPAS_PROCESALES[3])
        self.assertTrue(any(s.startswith("UPDATE actuaciones_proceso") for s in cur.statements))
        self.assertFalse(any("INSERT INTO actuaciones_proceso" in s for s in cur.statements))
        proc.assert_called_once()

    def test_sin_espejo_cae_a_dual_write(self):
        cur = _EspejoCursor()
        cur.rows = []
        with patch.object(dps, "dual_write_manual", return_value=99) as dual:
            got = dps.actualizar_espejo_manual(
                cur,
                actuacion_id=8,
                radicado="EXP-1",
                fecha="2026-04-02",
                etapa=ep.ETAPAS_PROCESALES[2],
                descripcion="Nueva",
            )
        self.assertEqual(got, 99)
        dual.assert_called_once()


class ActualizarLlamaEspejoTests(unittest.TestCase):
    def setUp(self):
        self._cols = {
            "procesos": {"radicado_interno", "tipo_cartera", "etapa_actual"},
            "actuaciones": {
                "id",
                "radicado_interno",
                "fecha",
                "etapa",
                "descripcion",
                "usuario",
                "tipificacion_sugerida",
            },
            "actuaciones_proceso": {
                "id",
                "radicado_interno",
                "content_hash",
                "fecha_actuacion",
                "etapa_asociada",
                "descripcion",
            },
        }
        p1 = patch("expedientes_service._cols", new=lambda cur, table: set(self._cols[table]))
        p2 = patch("expedientes_service._table_exists", new=lambda cur, table: table in self._cols)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_actualizar_espeja_en_actuaciones_proceso(self):
        cur = _MiniActCursor()
        aid = expedientes_service.registrar_actuacion(
            cur, "EXP-1", fecha="2026-03-01", etapa=E5, descripcion="orig"
        )
        with patch(
            "desviaciones_procesales_service.actualizar_espejo_manual", return_value=11
        ) as espejo:
            ok = expedientes_service.actualizar_actuacion(
                cur,
                "EXP-1",
                aid,
                fecha="2026-04-01",
                etapa=E3,
                descripcion="Corregido",
            )
        self.assertTrue(ok)
        espejo.assert_called_once()
        kwargs = espejo.call_args.kwargs
        self.assertEqual(kwargs["actuacion_id"], aid)
        self.assertEqual(kwargs["radicado"], "EXP-1")
        self.assertEqual(kwargs["descripcion"], "Corregido")
        self.assertEqual(kwargs["etapa"], E3)


class _MiniActCursor:
    """Cursor mínimo: insert/update/delete de actuaciones + recálculo de etapa."""

    def __init__(self):
        self.proceso = {"tipo_cartera": "JURIDICO", "etapa_actual": None}
        self.actuaciones = []
        self._next = 1
        self._res = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._res = []
        if "information_schema" in s:
            return
        if s.startswith("SELECT tipo_cartera"):
            self._res = [(self.proceso["tipo_cartera"],)]
        elif "FOR UPDATE" in s:
            self._res = [(1,)]
        elif s.startswith("SELECT etapa FROM actuaciones"):
            self._res = [(a["etapa"],) for a in self.actuaciones]
        elif s.startswith("SELECT etapa_asociada FROM actuaciones_proceso"):
            self._res = []
        elif s.startswith("UPDATE procesos SET etapa_actual"):
            self.proceso["etapa_actual"] = params[0]
        elif s.startswith("INSERT INTO actuaciones"):
            cols = [c.strip() for c in s.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, params))
            row["id"] = self._next
            self._next += 1
            self.actuaciones.append(row)
            self._res = [(row["id"],)]
        elif s.startswith("UPDATE actuaciones SET"):
            set_part = s.split("SET", 1)[1].split("WHERE", 1)[0]
            cols = [p.split("=")[0].strip() for p in set_part.split(",")]
            aid, rad = params[len(cols)], params[len(cols) + 1]
            hits = [a for a in self.actuaciones if a["id"] == aid and a.get("radicado_interno") == rad]
            self.rowcount = 0
            if hits:
                for i, col in enumerate(cols):
                    hits[0][col] = params[i]
                self.rowcount = 1
        else:
            raise AssertionError(f"SQL inesperado: {s}")

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)


class _EspejoCursor:
    def __init__(self):
        self.rows = [
            {
                "id": 5,
                "radicado_interno": "EXP-1",
                "content_hash": "migracion:3",
                "es_manual": True,
                "fecha_actuacion": date(2026, 1, 1),
                "etapa_asociada": "vieja",
                "descripcion": "antes",
                "tipo_actuacion": "Nota",
            }
        ]
        self._res = []
        self.statements = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.statements.append(s)
        self._res = []
        if "information_schema.tables" in s:
            self._res = [(1,)]
            return
        if "information_schema.columns" in s:
            cols = [
                "id",
                "radicado_interno",
                "content_hash",
                "fecha_actuacion",
                "etapa_asociada",
                "descripcion",
                "tipo_actuacion",
                "es_manual",
                "origen",
            ]
            self._res = [(c,) for c in cols]
            return
        if s.startswith("UPDATE actuaciones_proceso"):
            set_part = s.split("SET", 1)[1].split("WHERE", 1)[0]
            cols = [p.split("=")[0].strip() for p in set_part.split(",")]
            values = list(params or [])
            rad = values[len(cols)]
            hashes = {values[len(cols) + 1], values[len(cols) + 2]}
            hits = [
                r
                for r in self.rows
                if r["radicado_interno"] == rad and r["content_hash"] in hashes
            ]
            if hits:
                for i, col in enumerate(cols):
                    hits[0][col] = values[i]
                self._res = [(hits[0]["id"],)]

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)


if __name__ == "__main__":
    unittest.main()
