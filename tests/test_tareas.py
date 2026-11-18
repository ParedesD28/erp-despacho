"""Asignador de tareas: servicio, plazos→tarea y buckets de Mis Tareas."""
from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import date
from pathlib import Path

from jinja2 import DictLoader, Environment

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

import desviaciones_procesales_service as dps
import plazos_procesales as pp
import tareas_service as ts
from vencimientos_estado import ESTADO_COMPLETADO, ESTADO_PENDIENTE

COLS = {
    "tareas": [
        "id", "radicado_interno", "actuacion_id", "vencimiento_id", "titulo",
        "descripcion", "asignado_a", "creado_por", "prioridad", "estado",
        "fecha_vencimiento", "fecha_completada", "es_automatica", "origen",
        "recordatorio_dias", "notas_internas", "created_at", "updated_at",
    ],
    "tareas_historial": [
        "id", "tarea_id", "usuario_id", "accion", "campo",
        "valor_anterior", "valor_nuevo", "created_at",
    ],
    "tareas_comentarios": [
        "id", "tarea_id", "usuario_id", "comentario", "created_at",
    ],
    "vencimientos": [
        "id", "radicado_interno", "titulo", "fecha_vencimiento", "estado",
        "observaciones", "tipo", "categoria", "anulado", "valor",
    ],
    "procesos": ["radicado_interno", "abogado_id"],
    "abogados": ["id", "nombre", "activo"],
    "actuaciones_proceso": [
        "id", "radicado_interno", "tipo_actuacion", "descripcion",
        "etapa_asociada", "codigo_plazo", "responsable",
        "termino_legal_dias", "termino_legal_unidad",
        "fin_termino_esperado", "desviacion_dias",
        "desviacion_atribuible", "justificacion", "calculado_at",
        "es_manual", "inicio_termino", "fin_termino", "fecha_actuacion",
    ],
    "desviaciones_procesales": ["id", "actuacion_proceso_id"],
}


class _Store:
    def __init__(self):
        self.tareas = []
        self.tareas_historial = []
        self.tareas_comentarios = []
        self.vencimientos = []
        self.procesos = [{"radicado_interno": "EXP-1", "abogado_id": 7}]
        self.abogados = [
            {"id": 7, "nombre": "Ana", "activo": True},
            {"id": 8, "nombre": "Luis", "activo": True},
        ]
        self.seq = {"tareas": 0, "tareas_historial": 0, "tareas_comentarios": 0, "vencimientos": 0}

    def table(self, name: str):
        return getattr(self, name)


class _Cur:
    def __init__(self, store: _Store):
        self.store = store
        self._res = []
        self.description = []
        self.statements = []

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.statements.append(s)
        params = list(params or [])
        self._res = []
        self.description = []

        if "information_schema.tables" in s:
            self._res = [(1,)]
            self.description = [("ok",)]
            return
        if "information_schema.columns" in s:
            table = str(params[0]) if params else ""
            cols = COLS.get(table, [])
            self._res = [(c,) for c in cols]
            self.description = [("column_name",)]
            return

        if s.startswith("SELECT * FROM actuaciones_proceso"):
            # usado por desviaciones en tests cruzados
            return

        m_ins = re.match(r"INSERT INTO (\w+) \(([^)]+)\) VALUES", s, re.I)
        if m_ins:
            table = m_ins.group(1)
            cols = [c.strip() for c in m_ins.group(2).split(",")]
            row = dict(zip(cols, params))
            self.store.seq[table] = self.store.seq.get(table, 0) + 1
            row["id"] = self.store.seq[table]
            if "estado" not in row and table == "tareas":
                row["estado"] = "Pendiente"
            if "es_automatica" in row:
                row["es_automatica"] = bool(row["es_automatica"])
            self.store.table(table).append(row)
            if "RETURNING ID" in s.upper():
                self._res = [(row["id"],)]
                self.description = [("id",)]
            return

        if s.upper().startswith("UPDATE TAREAS"):
            tid = params[-1]
            row = next((t for t in self.store.tareas if t["id"] == tid), None)
            if not row:
                return
            if "asignado_a=%s" in s:
                row["asignado_a"] = params[0]
            if "estado=%s" in s:
                row["estado"] = params[0]
                if "fecha_completada=%s" in s:
                    row["fecha_completada"] = params[1]
                if "fecha_completada=NULL" in s:
                    row["fecha_completada"] = None
            if "fecha_vencimiento=%s" in s:
                row["fecha_vencimiento"] = params[0]
            if "vencimiento_id=%s" in s:
                # orden: fecha y/o vencimiento y/o asignado
                names = re.findall(r"(fecha_vencimiento|vencimiento_id|asignado_a)=%s", s)
                for i, name in enumerate(names):
                    row[name] = params[i]
            return

        if s.upper().startswith("UPDATE VENCIMIENTOS"):
            vid = params[-1]
            row = next((v for v in self.store.vencimientos if v["id"] == vid), None)
            if not row:
                return
            if "estado=%s" in s:
                row["estado"] = params[0]
            if "fecha_vencimiento=%s" in s:
                row["fecha_vencimiento"] = params[0]
            if "titulo=%s" in s:
                row["titulo"] = params[1] if len(params) > 1 else row.get("titulo")
            return

        if "FROM procesos" in s and "abogado_id" in s:
            rad = params[0] if params else ""
            hits = [p for p in self.store.procesos if p["radicado_interno"] == rad]
            self._res = [(h["abogado_id"],) for h in hits][:1]
            self.description = [("abogado_id",)]
            return

        if "FROM vencimientos" in s and "SELECT id" in s:
            rad, marca = params[0], params[1]
            needle = str(marca).strip("%")
            hits = [
                v
                for v in self.store.vencimientos
                if v.get("radicado_interno") == rad
                and needle in str(v.get("observaciones") or "")
            ]
            self._res = [(v["id"], v.get("estado") or "Pendiente") for v in hits][:1]
            self.description = [("id",), ("estado",)]
            return

        if s.upper().startswith("SELECT * FROM TAREAS"):
            rows = list(self.store.tareas)
            if "actuacion_id=%s" in s or "vencimiento_id=%s" in s:
                filtered = []
                for t in rows:
                    ok = False
                    if "actuacion_id=%s" in s and t.get("actuacion_id") in params:
                        ok = True
                    if "vencimiento_id=%s" in s and t.get("vencimiento_id") in params:
                        ok = True
                    if ok and t.get("estado") in ts.ESTADOS_ABIERTOS:
                        filtered.append(t)
                rows = filtered
            self._res = [tuple(r.get(c) for c in COLS["tareas"]) for r in rows]
            self.description = [(c,) for c in COLS["tareas"]]
            if "LIMIT 1" in s.upper():
                self._res = self._res[:1]
            return

        if "COUNT" in s and "FROM abogados" in s:
            carga = []
            for a in self.store.abogados:
                if not a.get("activo", True):
                    continue
                propias = [t for t in self.store.tareas if t.get("asignado_a") == a["id"]]
                abiertas = [t for t in propias if t.get("estado") in ts.ESTADOS_ABIERTOS]
                carga.append(
                    (
                        a["id"],
                        a["nombre"],
                        len(abiertas),
                        len(
                            [
                                t
                                for t in abiertas
                                if t.get("fecha_vencimiento")
                                and t["fecha_vencimiento"] < date.today()
                            ]
                        ),
                        len([t for t in abiertas if t.get("prioridad") == "Urgente"]),
                        len([t for t in abiertas if t.get("prioridad") == "Alta"]),
                    )
                )
            self._res = carga
            self.description = [
                ("id",),
                ("nombre",),
                ("abiertas",),
                ("vencidas",),
                ("urgentes",),
                ("altas",),
            ]
            return

        if "FROM tareas t" in s:
            rows = []
            for t in self.store.tareas:
                if "t.asignado_a=%s" in s and t.get("asignado_a") != params[0]:
                    continue
                if "t.radicado_interno=%s" in s and t.get("radicado_interno") != params[0]:
                    continue
                if "t.id=%s" in s and t.get("id") != params[0]:
                    continue
                aa = next((a["nombre"] for a in self.store.abogados if a["id"] == t.get("asignado_a")), None)
                ac = next((a["nombre"] for a in self.store.abogados if a["id"] == t.get("creado_por")), None)
                item = dict(t)
                item["asignado_nombre"] = aa
                item["creador_nombre"] = ac
                rows.append(item)
            if "t.estado IN ('Pendiente', 'En progreso', 'Bloqueada')" in s:
                rows = [r for r in rows if r.get("estado") in ts.ESTADOS_ABIERTOS]
            keys = COLS["tareas"] + ["asignado_nombre", "creador_nombre"]
            self._res = [tuple(r.get(k) for k in keys) for r in rows]
            self.description = [(k,) for k in keys]
            if "LIMIT 1" in s.upper() or "WHERE t.id=%s" in s:
                self._res = self._res[:1]
            return

        if "FROM tareas_historial" in s:
            tid = params[0]
            rows = [h for h in self.store.tareas_historial if h.get("tarea_id") == tid]
            keys = COLS["tareas_historial"] + ["usuario_nombre"]
            out = []
            for h in rows:
                item = dict(h)
                item["usuario_nombre"] = None
                out.append(tuple(item.get(k) for k in keys))
            self._res = out
            self.description = [(k,) for k in keys]
            return

        if "FROM tareas_comentarios" in s:
            tid = params[0]
            rows = [c for c in self.store.tareas_comentarios if c.get("tarea_id") == tid]
            keys = COLS["tareas_comentarios"] + ["usuario_nombre"]
            out = []
            for c in rows:
                item = dict(c)
                item["usuario_nombre"] = None
                out.append(tuple(item.get(k) for k in keys))
            self._res = out
            self.description = [(k,) for k in keys]
            return

        if s.upper().startswith("SELECT ID, NOMBRE FROM ABOGADOS"):
            self._res = [(a["id"], a["nombre"]) for a in self.store.abogados]
            self.description = [("id",), ("nombre",)]
            return

        if s.startswith("SELECT id FROM tareas") and "vencimiento_id=%s" in s:
            vid = params[0]
            self._res = [
                (t["id"],)
                for t in self.store.tareas
                if t.get("vencimiento_id") == vid and t.get("estado") in ts.ESTADOS_ABIERTOS
            ]
            self.description = [("id",)]
            return

    def fetchone(self):
        if not self._res:
            return None
        row = self._res[0]
        self._res = self._res[1:]
        return self._as_dict(row)

    def fetchall(self):
        rows = [self._as_dict(r) for r in self._res]
        self._res = []
        return rows

    def _as_dict(self, row):
        if isinstance(row, dict):
            return row
        names = [d[0] for d in self.description] if self.description else []
        if names and len(names) == len(row):
            return dict(zip(names, row))
        return row


class PrioridadAutomaticaTests(unittest.TestCase):
    def test_recurso_urgente(self):
        self.assertEqual(ts.prioridad_automatica("APELACION", "Apelación"), "Urgente")
        self.assertEqual(ts.prioridad_automatica("REPOSICION", None), "Urgente")
        self.assertEqual(ts.prioridad_automatica(None, "Interponer recurso"), "Urgente")

    def test_contestacion_alta(self):
        self.assertEqual(ts.prioridad_automatica("EXCEPCIONES_TRASLADO", None), "Alta")
        self.assertEqual(ts.prioridad_automatica("INADMISION_SUBSANAR", None), "Alta")
        self.assertEqual(ts.prioridad_automatica(None, "Contestar la demanda"), "Alta")

    def test_default_media(self):
        self.assertEqual(ts.prioridad_automatica("NOTIFICACION", "Notificar"), "Media")
        self.assertEqual(ts.prioridad_automatica(None, "Seguimiento"), "Media")


class TareasServicioTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.cur = _Cur(self.store)

    def test_manual_exige_asignado_y_titulo(self):
        with self.assertRaises(ts.TareasError):
            ts.crear_tarea_manual(self.cur, titulo="", asignado_a=7)
        with self.assertRaises(ts.TareasError):
            ts.crear_tarea_manual(self.cur, titulo="Hacer memorial", asignado_a=None)
        t = ts.crear_tarea_manual(
            self.cur,
            titulo="Hacer memorial",
            asignado_a=7,
            creado_por=8,
            radicado_interno="EXP-1",
        )
        self.assertEqual(t["titulo"], "Hacer memorial")
        self.assertEqual(t["asignado_a"], 7)
        self.assertFalse(t["es_automatica"])
        self.assertEqual(t["origen"], "manual")
        self.assertEqual(len(self.store.tareas_historial), 1)

    def test_plazo_una_sola_abierta_y_recalcula_fecha(self):
        r1 = ts.crear_tarea_desde_plazo(
            self.cur,
            radicado="EXP-1",
            actuacion_id=11,
            vencimiento_id=21,
            titulo="Subsanar inadmisión",
            fecha=date(2026, 3, 10),
            codigo_plazo="INADMISION_SUBSANAR",
        )
        self.assertIsNotNone(r1)
        self.assertTrue(r1["es_automatica"])
        self.assertEqual(r1["origen"], "plazo")
        self.assertEqual(r1["prioridad"], "Alta")
        self.assertEqual(r1["asignado_a"], 7)
        r2 = ts.crear_tarea_desde_plazo(
            self.cur,
            radicado="EXP-1",
            actuacion_id=11,
            vencimiento_id=21,
            titulo="Subsanar inadmisión",
            fecha=date(2026, 3, 20),
            codigo_plazo="INADMISION_SUBSANAR",
        )
        self.assertEqual(len(self.store.tareas), 1)
        self.assertEqual(r2["id"], r1["id"])
        self.assertEqual(_as_date(r2["fecha_vencimiento"]), date(2026, 3, 20))

    def test_completar_sincroniza_vencimiento(self):
        self.store.vencimientos.append(
            {
                "id": 21,
                "radicado_interno": "EXP-1",
                "titulo": "Subsanar",
                "fecha_vencimiento": date(2026, 3, 10),
                "estado": ESTADO_PENDIENTE,
                "observaciones": "actuacion_proceso_id=11",
            }
        )
        self.store.seq["vencimientos"] = 21
        t = ts.crear_tarea_desde_plazo(
            self.cur,
            radicado="EXP-1",
            actuacion_id=11,
            vencimiento_id=21,
            titulo="Recurso de apelación",
            fecha=date(2026, 3, 10),
            codigo_plazo="APELACION",
        )
        self.assertEqual(t["prioridad"], "Urgente")
        done = ts.completar_tarea(self.cur, t["id"], usuario_id=7)
        self.assertEqual(done["estado"], "Completada")
        self.assertEqual(self.store.vencimientos[0]["estado"], ESTADO_COMPLETADO)

    def test_automatica_no_se_borra(self):
        t = ts.crear_tarea_desde_plazo(
            self.cur,
            radicado="EXP-1",
            actuacion_id=11,
            vencimiento_id=21,
            titulo="Notificar",
            fecha=date(2026, 4, 1),
            codigo_plazo="NOTIFICACION",
        )
        with self.assertRaises(ts.TareasError):
            ts.borrar_tarea(self.cur, t["id"], usuario_id=7)
        cancel = ts.cambiar_estado(self.cur, t["id"], estado="Cancelada", usuario_id=7)
        self.assertEqual(cancel["estado"], "Cancelada")
        self.assertEqual(len(self.store.tareas), 1)

    def test_reasignar_y_comentario(self):
        t = ts.crear_tarea_manual(
            self.cur, titulo="Visita", asignado_a=7, creado_por=7
        )
        t = ts.reasignar_tarea(self.cur, t["id"], asignado_a=8, usuario_id=7)
        self.assertEqual(t["asignado_a"], 8)
        t = ts.agregar_comentario(self.cur, t["id"], comentario="Ya hablé", usuario_id=8)
        self.assertEqual(len(t["comentarios"]), 1)

    def test_buckets_mis_tareas(self):
        hoy = date(2026, 3, 18)  # miércoles
        filas = [
            {"estado": "Pendiente", "fecha_vencimiento": date(2026, 3, 10), "titulo": "vencio"},
            {"estado": "En progreso", "fecha_vencimiento": hoy, "titulo": "hoy"},
            {"estado": "Bloqueada", "fecha_vencimiento": date(2026, 3, 20), "titulo": "semana"},
            {"estado": "Pendiente", "fecha_vencimiento": date(2026, 4, 1), "titulo": "luego"},
            {"estado": "Completada", "fecha_vencimiento": hoy, "titulo": "hecha"},
        ]
        g = ts.agrupar_por_vencimiento(filas, hoy=hoy)
        self.assertEqual([x["titulo"] for x in g["vencidas"]], ["vencio"])
        self.assertEqual([x["titulo"] for x in g["hoy"]], ["hoy"])
        self.assertEqual([x["titulo"] for x in g["esta_semana"]], ["semana"])
        self.assertEqual([x["titulo"] for x in g["mas_adelante"]], ["luego"])
        self.assertEqual(g["total_abiertas"], 4)

    def test_listar_proceso_y_carga(self):
        ts.crear_tarea_manual(
            self.cur, titulo="A", asignado_a=7, radicado_interno="EXP-1"
        )
        ts.crear_tarea_manual(
            self.cur, titulo="B", asignado_a=8, radicado_interno="EXP-1"
        )
        lista = ts.listar_tareas_proceso(self.cur, "EXP-1")
        self.assertEqual(len(lista), 2)
        carga = ts.obtener_carga_trabajo(self.cur)
        self.assertTrue(any(c["id"] == 7 and c["abiertas"] >= 1 for c in carga))


def _as_date(value):
    if isinstance(value, date) and not isinstance(value, type(date.min)):
        return value
    if hasattr(value, "date"):
        return value.date()
    return date.fromisoformat(str(value)[:10])


class PlazoCreaTareaTests(unittest.TestCase):
    def test_procesar_actuacion_parte_crea_vencimiento_y_tarea(self):
        store = _Store()
        cur = _Cur(store)
        ap = {
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

        orig = cur.execute

        def execute(sql, params=None):
            s = " ".join(str(sql).split())
            if s.startswith("SELECT * FROM actuaciones_proceso"):
                cur._res = [ap]
                cur.description = [(k,) for k in ap]
                return
            if s.startswith("UPDATE actuaciones_proceso"):
                keys = [p.strip() for p in s.split("SET", 1)[1].split("WHERE", 1)[0].split(",")]
                names = [k.split("=")[0].strip() for k in keys]
                for n, v in zip(names, params[:-1]):
                    ap[n] = v
                return
            if s.startswith("INSERT INTO desviaciones_procesales"):
                return
            orig(sql, params)

        cur.execute = execute
        r = dps._procesar_actuacion(cur, 7, hoy=date(2026, 3, 3))
        self.assertEqual(r["codigo_plazo"], "INADMISION_SUBSANAR")
        self.assertEqual(r["responsable"], pp.RESPONSABLE_PARTE)
        self.assertEqual(len(store.vencimientos), 1)
        self.assertEqual(len(store.tareas), 1)
        self.assertTrue(store.tareas[0]["es_automatica"])
        self.assertEqual(store.tareas[0]["origen"], "plazo")
        self.assertEqual(store.tareas[0]["asignado_a"], 7)
        dps._procesar_actuacion(cur, 7, hoy=date(2026, 3, 3))
        self.assertEqual(len(store.vencimientos), 1)
        self.assertEqual(len(store.tareas), 1)

    def test_recalcular_actualiza_fecha_vencimiento_existente(self):
        store = _Store()
        cur = _Cur(store)
        store.vencimientos.append(
            {
                "id": 3,
                "radicado_interno": "EXP-1",
                "titulo": "Viejo",
                "fecha_vencimiento": "2026-01-01",
                "estado": ESTADO_PENDIENTE,
                "observaciones": "actuacion_proceso_id=9",
                "anulado": False,
            }
        )
        store.seq["vencimientos"] = 3
        vid = dps._crear_vencimiento_si_aplica(
            cur,
            radicado="EXP-1",
            actuacion_id=9,
            titulo="Subsanar inadmisión (CGP art. 90)",
            fecha=date(2026, 3, 15),
            codigo_plazo="INADMISION_SUBSANAR",
        )
        self.assertEqual(vid, 3)
        self.assertEqual(len(store.vencimientos), 1)
        self.assertEqual(store.vencimientos[0]["fecha_vencimiento"], date(2026, 3, 15).isoformat())
        self.assertEqual(len(store.tareas), 1)


class TemplatesTareasTests(unittest.TestCase):
    def test_mis_tareas_compila(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        tareas = (ROOT / "templates" / "tareas.html").read_text(encoding="utf-8")
        env = Environment(loader=DictLoader({"base.html": base, "tareas.html": tareas}))
        env.get_template("tareas.html")

    def test_expediente_tiene_seccion_tareas(self):
        detalle = (ROOT / "templates" / "detalle_expediente_v4.html").read_text(encoding="utf-8")
        self.assertIn(">Tareas<", detalle)
        self.assertIn("/tareas/nueva", detalle)
        self.assertNotIn("CRM de tareas", detalle)

    def test_migracion_idempotente(self):
        sql = (ROOT / "migrations" / "20261118_asignador_tareas.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS tareas", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS tareas_historial", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS tareas_comentarios", sql)
        self.assertIn("uq_tareas_abierta_actuacion", sql)
        self.assertNotRegex(sql, r"(?im)^\s*(BEGIN|COMMIT|ROLLBACK)\s*;")
        self.assertNotIn("CONCURRENTLY", sql.upper())


class PermisosTareasTests(unittest.TestCase):
    def test_cliente_ph_no_entra(self):
        import permisos

        ph = permisos.permisos_de_perfil(permisos.PERFIL_CLIENTE_PH)
        self.assertTrue(permisos.denegar_acceso(ph, "GET", "/tareas"))


if __name__ == "__main__":
    unittest.main()
