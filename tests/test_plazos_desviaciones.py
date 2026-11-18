"""Keywords, desviación, ranking unificado e idempotencia del cálculo."""
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

import desviaciones_procesales_service as dps
import etapas_procesales as ep
import plazos_procesales as pp


class KeywordsPlazoTests(unittest.TestCase):
    def test_inadmision_antes_que_admision(self):
        p = pp.mapear_actuacion_a_plazo("Auto", "Se inadmite la demanda")
        self.assertEqual(p.codigo, "INADMISION_SUBSANAR")
        self.assertEqual(p.responsable, pp.RESPONSABLE_PARTE)
        self.assertEqual(p.etapa_asociada, ep.ETAPAS_PROCESALES[1])
        self.assertTrue(p.genera_vencimiento)

    def test_admision_es_juzgado(self):
        p = pp.mapear_actuacion_a_plazo("Auto", "Se admite la demanda")
        self.assertEqual(p.codigo, "ADMISION")
        self.assertEqual(p.responsable, pp.RESPONSABLE_JUZGADO)
        self.assertEqual(pp.etiqueta_reloj(p.responsable), "Despacho")

    def test_notificacion_es_parte(self):
        p = pp.mapear_actuacion_a_plazo("Notificación", "Se ordena notificar")
        self.assertEqual(p.responsable, pp.RESPONSABLE_PARTE)
        self.assertEqual(pp.etiqueta_reloj(p.responsable), "Abogado")

    def test_excepciones_cgp_442(self):
        p = pp.mapear_actuacion_a_plazo("Auto", "Traslado de excepciones")
        self.assertEqual(p.codigo, "EXCEPCIONES_TRASLADO")
        self.assertEqual(p.termino_dias, 10)

    def test_sentencia_juzgado(self):
        p = pp.mapear_actuacion_a_plazo("Sentencia", "Fallo de primera instancia")
        self.assertEqual(p.etapa_asociada, ep.ETAPAS_PROCESALES[6])
        self.assertEqual(p.responsable, pp.RESPONSABLE_JUZGADO)

    def test_etapa_manual_sin_keywords(self):
        p = pp.mapear_actuacion_a_plazo("Nota", "El abogado registró", etapa=ep.ETAPAS_PROCESALES[4])
        self.assertEqual(p.etapa_asociada, ep.ETAPAS_PROCESALES[4])
        self.assertEqual(p.responsable, pp.RESPONSABLE_PARTE)

    def test_termino_rama_sin_match(self):
        p = pp.mapear_actuacion_a_plazo("X", "texto raro", tiene_termino=True)
        self.assertEqual(p.codigo, "TERMINO_RAMA")
        self.assertEqual(p.responsable, pp.RESPONSABLE_PARTE)

    def test_sin_match_devuelve_none(self):
        self.assertIsNone(pp.mapear_actuacion_a_plazo("xyz", "sin indicios"))

    def test_auto_tramite_con_termino_es_parte(self):
        p = pp.mapear_actuacion_a_plazo("Auto de trámite", "constancia", tiene_termino=True)
        self.assertEqual(p.responsable, pp.RESPONSABLE_PARTE)

    def test_etapa_asociada_del_catalogo(self):
        for plazo in pp.PLAZOS_LEGALES:
            self.assertIn(plazo.etapa_asociada, ep.ETAPAS_SELECCIONABLES)


class DesviacionCalendarioTests(unittest.TestCase):
    def test_fin_esperado_habiles_salta_finde_y_festivo(self):
        # Receso judicial 20 dic–10 ene; 12 ene 2026 es lunes de Reyes (traslado).
        fin = dps.calcular_fin_esperado(date(2025, 12, 31), 1, pp.UNIDAD_DIAS_HABILES)
        self.assertEqual(fin, date(2026, 1, 13))

    def test_receso_judicial_fin_de_anio(self):
        self.assertTrue(dps.es_receso_judicial(date(2026, 12, 20)))
        self.assertTrue(dps.es_receso_judicial(date(2027, 1, 10)))
        self.assertFalse(dps.es_receso_judicial(date(2027, 1, 12)))

    def test_desviacion_positiva_si_cierra_tarde(self):
        esperado = date(2026, 3, 10)
        real = date(2026, 3, 17)
        dias = dps.calcular_desviacion_dias(esperado, real)
        self.assertGreater(dias, 0)

    def test_desviacion_negativa_si_cierra_antes(self):
        esperado = date(2026, 3, 17)
        real = date(2026, 3, 10)
        dias = dps.calcular_desviacion_dias(esperado, real)
        self.assertLess(dias, 0)

    def test_fechas_invalidas_no_rompen(self):
        self.assertIsNone(dps.calcular_fin_esperado(None, 5, pp.UNIDAD_DIAS_HABILES))
        self.assertIsNone(dps.calcular_fin_esperado("no-es-fecha", 5, pp.UNIDAD_DIAS_HABILES))
        self.assertIsNone(dps.calcular_fin_esperado(date(2026, 1, 15), "x", pp.UNIDAD_DIAS_HABILES))
        self.assertIsNone(dps.calcular_desviacion_dias(None, date.today()))

    def test_semaforo_parte_vs_juzgado(self):
        self.assertEqual(dps.semaforo_dias(pp.RESPONSABLE_PARTE, 2), "verde")
        self.assertEqual(dps.semaforo_dias(pp.RESPONSABLE_PARTE, 7), "amarillo")
        self.assertEqual(dps.semaforo_dias(pp.RESPONSABLE_PARTE, 20), "rojo")
        self.assertEqual(dps.semaforo_dias(pp.RESPONSABLE_JUZGADO, 10), "verde")


class RelojTramosTests(unittest.TestCase):
    def test_duenio_lo_marca_el_ultimo_acto(self):
        eventos = [
            {
                "id": 1,
                "fecha": date(2026, 2, 2),
                "responsable": pp.RESPONSABLE_JUZGADO,
                "tipo": "Admisión",
                "descripcion": "Se admite",
            },
            {
                "id": 2,
                "fecha": date(2026, 2, 10),
                "responsable": pp.RESPONSABLE_PARTE,
                "tipo": "Notificación",
                "descripcion": "Hay que notificar",
            },
        ]
        tramos = dps.construir_tramos(eventos, hoy=date(2026, 2, 12))
        self.assertEqual(tramos[0]["duenio"], "Despacho")
        self.assertTrue(tramos[-1]["abierto"])
        self.assertEqual(tramos[-1]["duenio"], "Abogado")
        self.assertTrue(tramos[-1]["dias"] >= 1)


class RankingUnificadoPuroTests(unittest.TestCase):
    def test_calcular_etapa_no_cambia_con_union(self):
        valores = [ep.ETAPAS_PROCESALES[0], ep.ETAPAS_PROCESALES[4], ep.ETAPA_NEUTRA]
        self.assertEqual(ep.calcular_etapa(valores), ep.ETAPAS_PROCESALES[4])
        self.assertEqual(
            ep.calcular_etapa(valores + [ep.ETAPAS_PROCESALES[2]]),
            ep.ETAPAS_PROCESALES[4],
        )


class _MemCursor:
    """Cursor mínimo para procesar_actuacion / vencimientos."""

    def __init__(self):
        self.ap = {
            "id": 7,
            "radicado_interno": "EXP-1",
            "tipo_actuacion": "Auto",
            "descripcion": "Se inadmite la demanda para subsanar",
            "etapa_asociada": None,
            "es_manual": False,
            "inicio_termino": date(2026, 3, 2),
            "fin_termino": None,
            "fecha_actuacion": date(2026, 3, 2),
            "justificacion": None,
            "codigo_plazo": None,
            "responsable": None,
            "calculado_at": None,
        }
        self.desviaciones = []
        self.vencimientos = []
        self._res = []
        self.statements = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.statements.append(s)
        self._res = []
        if "information_schema.tables" in s:
            self._res = [(1,)]
        elif "information_schema.columns" in s:
            if "actuaciones_proceso" in str(params):
                cols = [
                    "id", "radicado_interno", "tipo_actuacion", "descripcion",
                    "etapa_asociada", "codigo_plazo", "responsable",
                    "termino_legal_dias", "termino_legal_unidad",
                    "fin_termino_esperado", "desviacion_dias",
                    "desviacion_atribuible", "justificacion", "calculado_at",
                    "es_manual", "inicio_termino", "fin_termino", "fecha_actuacion",
                ]
            elif "desviaciones_procesales" in str(params):
                cols = ["id", "actuacion_proceso_id"]
            elif "vencimientos" in str(params):
                cols = [
                    "id", "radicado_interno", "titulo", "fecha_vencimiento",
                    "estado", "observaciones", "tipo", "categoria", "anulado", "valor",
                ]
            else:
                cols = []
            self._res = [(c,) for c in cols]
        elif s.startswith("SELECT * FROM actuaciones_proceso"):
            self._res = [self.ap]
        elif s.startswith("UPDATE actuaciones_proceso"):
            keys = [p.strip() for p in s.split("SET", 1)[1].split("WHERE", 1)[0].split(",")]
            names = [k.split("=")[0].strip() for k in keys]
            for n, v in zip(names, params[:-1]):
                self.ap[n] = v
        elif s.startswith("INSERT INTO desviaciones_procesales"):
            row = {"actuacion_proceso_id": params[1]}
            existing = [d for d in self.desviaciones if d["actuacion_proceso_id"] == params[1]]
            if existing:
                existing[0].update(row)
            else:
                self.desviaciones.append(row)
        elif "FROM vencimientos" in s and "SELECT id" in s:
            marca = str(params[1]) if params and len(params) > 1 else ""
            hits = [v for v in self.vencimientos if marca.strip("%") in str(v.get("observaciones") or "")]
            self._res = [(v["id"],) for v in hits][:1]
        elif s.startswith("INSERT INTO vencimientos"):
            vid = len(self.vencimientos) + 1
            cols = [c.strip() for c in s.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, params))
            row["id"] = vid
            self.vencimientos.append(row)
            self._res = [(vid,)]
        else:
            self._res = []

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)


class ProcesarIdempotenteTests(unittest.TestCase):
    def test_procesar_dos_veces_un_solo_vencimiento(self):
        cur = _MemCursor()
        r1 = dps._procesar_actuacion(cur, 7, hoy=date(2026, 3, 3))
        self.assertEqual(r1["codigo_plazo"], "INADMISION_SUBSANAR")
        self.assertEqual(r1["responsable"], pp.RESPONSABLE_PARTE)
        self.assertEqual(len(cur.vencimientos), 1)
        self.assertEqual(len(cur.desviaciones), 1)
        dps._procesar_actuacion(cur, 7, hoy=date(2026, 3, 3))
        self.assertEqual(len(cur.vencimientos), 1)
        self.assertEqual(len(cur.desviaciones), 1)
        self.assertIsNotNone(cur.ap["fin_termino_esperado"])
        self.assertEqual(cur.ap["etapa_asociada"], ep.ETAPAS_PROCESALES[1])

    def test_sin_match_no_lanza(self):
        cur = _MemCursor()
        cur.ap["descripcion"] = "texto que no mapea"
        cur.ap["tipo_actuacion"] = "???"
        cur.ap["inicio_termino"] = None
        cur.ap["fin_termino"] = None
        self.assertIsNotNone(dps._procesar_actuacion(cur, 7, hoy=date(2026, 3, 3)))

    def test_procesar_actuacion_envuelve_errores(self):
        self.assertIsNone(dps.procesar_actuacion(None, "no-int"))


class SyncIdempotenciaTests(unittest.TestCase):
    def test_insertar_conflicto_no_reprocesa_id(self):
        from sync_procesos_service import _insertar_actuacion

        cur = _MemCursor()
        # Simula ON CONFLICT DO NOTHING: fetchone vacío.
        cur._res = []

        def execute(sql, params=None):
            cur.statements.append(" ".join(sql.split()))
            cur._res = []

        cur.execute = execute
        row = {
            "radicado_interno": "EXP-1",
            "radicado_rama": "1" * 23,
            "id_actuacion": "9",
            "content_hash": "abc",
            "fecha_actuacion": date(2026, 3, 1),
            "fecha_registro": None,
            "tipo_actuacion": "Auto",
            "descripcion": "x",
            "con_documentos": False,
            "inicio_termino": None,
            "fin_termino": None,
            "tipo_termino": None,
            "vencido": None,
        }
        with patch("sync_procesos_service._cols", return_value={"radicado_interno", "content_hash"}):
            self.assertIsNone(_insertar_actuacion(cur, row))


if __name__ == "__main__":
    unittest.main()
