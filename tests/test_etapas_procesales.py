import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import etapas_procesales as ep
import expedientes_service

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconciliar_etapa_proceso.py"
_spec = importlib.util.spec_from_file_location("reconciliar_etapa_proceso", _SCRIPT)
recon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recon)

_COLS = {
    "procesos": {"radicado_interno", "tipo_cartera", "etapa_actual"},
    "actuaciones": {"id", "radicado_interno", "fecha", "etapa", "descripcion", "usuario", "tipificacion_sugerida"},
}


class FakeCursor:
    """Cursor mínimo en memoria para procesos/actuaciones."""

    def __init__(self, tipo_cartera="JURIDICO", etapa_actual=None, actuaciones=None):
        self.proceso = {"tipo_cartera": tipo_cartera, "etapa_actual": etapa_actual}
        self.actuaciones = list(actuaciones or [])
        self._next = 1
        self._res = []
        self.rowcount = 0
        self.updates = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._res = []
        if s.startswith("SELECT tipo_cartera"):
            self._res = [(self.proceso["tipo_cartera"],)]
        elif "FOR UPDATE" in s:
            self._res = [(1,)]
        elif s.startswith("SELECT etapa FROM actuaciones"):
            self._res = [(a["etapa"],) for a in self.actuaciones]
        elif s.startswith("UPDATE procesos SET etapa_actual"):
            if self.proceso["etapa_actual"] != params[0]:
                self.proceso["etapa_actual"] = params[0]
                self.updates += 1
        elif s.startswith("INSERT INTO actuaciones"):
            cols = [c.strip() for c in s.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, params))
            row["id"] = self._next
            self._next += 1
            self.actuaciones.append(row)
            self._res = [(row["id"],)]
        elif s.startswith("DELETE FROM actuaciones"):
            antes = len(self.actuaciones)
            self.actuaciones = [a for a in self.actuaciones if a["id"] != params[0]]
            self.rowcount = antes - len(self.actuaciones)
        else:
            raise AssertionError(f"SQL inesperado: {s}")

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)


E1, E3, E4, E5, E7 = (ep.ETAPAS_PROCESALES[i] for i in (0, 2, 3, 4, 6))
TERM = ep.ETAPA_TERMINACION


class CalculoEtapaTests(unittest.TestCase):
    def test_etapa_menor_con_fecha_posterior_no_retrocede(self):
        # Orden cronológico: la última actuación (por fecha) es de rango menor.
        self.assertEqual(ep.calcular_etapa([E1, E4, E3]), E4)

    def test_fecha_posterior_misma_etapa_se_mantiene(self):
        self.assertEqual(ep.calcular_etapa([E3, E3]), E3)

    def test_terminacion_prevalece_aunque_no_sea_la_ultima(self):
        self.assertEqual(ep.calcular_etapa([E5, TERM, E1, E7]), TERM)

    def test_valor_desconocido_se_rechaza_en_entrada(self):
        with self.assertRaises(ep.EtapaInvalidaError):
            ep.normalizar_etapa("Etapa inventada")

    def test_valor_desconocido_se_ignora_y_reporta_al_recalcular(self):
        r = ep.calcular_resultado([E3, "basura", "basura"])
        self.assertEqual(r.etapa, E3)
        self.assertEqual(r.invalidos, ("basura",))

    def test_normaliza_tildes_mayusculas_y_alias_inicio(self):
        self.assertEqual(ep.normalizar_etapa("  4. medidas CAUTELARES "), E4)
        self.assertEqual(ep.normalizar_etapa("terminacion del proceso"), TERM)
        self.assertEqual(ep.normalizar_etapa("Inicio"), E1)

    def test_etapa_neutra_no_avanza_ni_retrocede(self):
        self.assertEqual(ep.calcular_etapa([E3, ep.ETAPA_NEUTRA]), E3)
        self.assertIsNone(ep.calcular_etapa([ep.ETAPA_NEUTRA, "", None]))

    def test_vacio_obligatorio(self):
        with self.assertRaises(ep.EtapaInvalidaError):
            ep.normalizar_etapa("", permitir_vacio=False)
        self.assertIsNone(ep.normalizar_etapa(None))

    def test_catalogo_unico_reutilizado(self):
        self.assertEqual(expedientes_service.CANONICAL_STAGES, list(ep.ETAPAS_SELECCIONABLES))
        self.assertEqual(ep.RANGO_ETAPA[TERM], len(ep.ETAPAS_PROCESALES))


class ServicioSincronizacionTests(unittest.TestCase):
    def setUp(self):
        for target, new in (
            ("expedientes_service._cols", lambda cur, table: set(_COLS[table])),
            ("expedientes_service._table_exists", lambda cur, table: True),
        ):
            p = patch(target, new)
            p.start()
            self.addCleanup(p.stop)

    def registrar(self, cur, etapa, fecha="2026-01-01"):
        return expedientes_service.registrar_actuacion(
            cur, "EXP-1", fecha=fecha, etapa=etapa, descripcion=f"d-{etapa}-{fecha}"
        )

    def test_registrar_actualiza_ambas_columnas(self):
        cur = FakeCursor(etapa_actual=E1)
        self.registrar(cur, E3)
        self.assertEqual(cur.actuaciones[0]["etapa"], E3)
        self.assertEqual(cur.proceso["etapa_actual"], E3)

    def test_lower_etapa_later_date_no_regresa(self):
        cur = FakeCursor(etapa_actual=E1)
        self.registrar(cur, E4, "2026-02-01")
        self.registrar(cur, E3, "2026-03-01")
        self.assertEqual(cur.proceso["etapa_actual"], E4)

    def test_same_etapa_later_date(self):
        cur = FakeCursor(etapa_actual=E1)
        self.registrar(cur, E3, "2026-02-01")
        updates = cur.updates
        self.registrar(cur, E3, "2026-03-01")
        self.assertEqual(cur.proceso["etapa_actual"], E3)
        self.assertEqual(cur.updates, updates)

    def test_terminacion(self):
        cur = FakeCursor(etapa_actual=E1)
        self.registrar(cur, E7, "2026-05-01")
        self.registrar(cur, TERM, "2026-01-01")
        self.assertEqual(cur.proceso["etapa_actual"], TERM)
        self.registrar(cur, E5, "2026-09-01")
        self.assertEqual(cur.proceso["etapa_actual"], TERM)

    def test_unknown_value_falla_sin_escribir(self):
        cur = FakeCursor(etapa_actual=E1)
        with self.assertRaises(ep.EtapaInvalidaError):
            self.registrar(cur, "Etapa inventada")
        self.assertEqual(cur.actuaciones, [])
        self.assertEqual(cur.proceso["etapa_actual"], E1)

    def test_etapa_vacia_es_neutra_y_no_cambia_proceso(self):
        cur = FakeCursor(etapa_actual=E3)
        self.registrar(cur, None)
        self.assertEqual(cur.actuaciones[0]["etapa"], ep.ETAPA_NEUTRA)
        self.assertEqual(cur.proceso["etapa_actual"], E3)

    def test_prejuridico_no_se_modifica(self):
        cur = FakeCursor(tipo_cartera="PREJURIDICO", etapa_actual=None)
        self.registrar(cur, E3)
        self.assertIsNone(cur.proceso["etapa_actual"])

    def test_eliminar_recalcula(self):
        cur = FakeCursor(etapa_actual=E1)
        self.registrar(cur, E3, "2026-02-01")
        id_alta = self.registrar(cur, E5, "2026-03-01")
        self.assertEqual(cur.proceso["etapa_actual"], E5)
        self.assertTrue(expedientes_service.eliminar_actuacion(cur, "EXP-1", id_alta))
        self.assertEqual(cur.proceso["etapa_actual"], E3)
        self.assertFalse(expedientes_service.eliminar_actuacion(cur, "EXP-1", 999))


class ReconciliacionTests(unittest.TestCase):
    def test_plan_reporta_y_es_idempotente(self):
        procesos = [
            {"radicado_interno": "A", "tipo_cartera": "JURIDICO", "etapa_actual": E1},
            {"radicado_interno": "B", "tipo_cartera": "JURIDICO", "etapa_actual": E4},
            {"radicado_interno": "C", "tipo_cartera": "JURIDICO", "etapa_actual": "texto libre"},
            {"radicado_interno": "D", "tipo_cartera": "PREJURIDICO", "etapa_actual": None},
            {"radicado_interno": "E", "tipo_cartera": "JURIDICO", "etapa_actual": E3},
        ]
        acts = [
            {"radicado_interno": "A", "etapa": E3},
            {"radicado_interno": "A", "etapa": "Inicio"},
            {"radicado_interno": "B", "etapa": E4},
            {"radicado_interno": "B", "etapa": E3},
            {"radicado_interno": "C", "etapa": TERM},
            {"radicado_interno": "E", "etapa": "basura"},
        ]
        plan = recon.planear(procesos, acts)
        self.assertEqual(
            {c["radicado_interno"]: (c["antes"], c["despues"]) for c in plan["cambios"]},
            {"A": (E1, E3), "C": ("texto libre", TERM)},
        )
        self.assertEqual([x["radicado_interno"] for x in plan["etapa_actual_invalida"]], ["C"])
        self.assertEqual([x["radicado_interno"] for x in plan["etapas_actuacion_invalidas"]], ["E"])

        for c in plan["cambios"]:
            next(p for p in procesos if p["radicado_interno"] == c["radicado_interno"])["etapa_actual"] = c["despues"]
        self.assertEqual(recon.planear(procesos, acts)["cambios"], [])


if __name__ == "__main__":
    unittest.main()
