"""Regresión: /expedientes debe poder buscar por cédula de las partes.

Antes: cargar_procesos_general_sin_duplicados solo traía nombres;
filtrarTodo buscaba en fila.textContent y la cédula no estaba en la fila.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)


def _normalizar(texto: str) -> str:
    import unicodedata

    t = (texto or "").lower()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.strip()


def _normalizar_identificacion(texto: str) -> str:
    return re.sub(r"[.\s\-]", "", _normalizar(texto))


def _texto_coincide(contenido: str, busqueda: str) -> bool:
    """Espejo de textoCoincide() en templates/expedientes.html."""
    if not busqueda:
        return True
    contenido_n = _normalizar(contenido)
    busqueda_n = _normalizar(busqueda)
    if busqueda_n in contenido_n:
        return True
    busqueda_id = _normalizar_identificacion(busqueda)
    if not busqueda_id:
        return False
    return busqueda_id in _normalizar_identificacion(contenido)


class ExpedientesSqlContratoTests(unittest.TestCase):
    def test_sql_agrega_identificaciones_de_partes(self):
        src = (ROOT / "expedientes_service.py").read_text(encoding="utf-8")
        fn = src.split(
            "def cargar_procesos_general_sin_duplicados", 1
        )[1].split("\ndef ", 1)[0]
        self.assertIn("demandante_identificacion", fn)
        self.assertIn("demandado_identificacion", fn)
        self.assertIn("c.identificacion", fn)
        self.assertIn("STRING_AGG(DISTINCT c.identificacion", fn)
        # Filtros de estado no deben romperse.
        self.assertIn("ACTIVOS", fn)
        self.assertIn("INACTIVOS", fn)
        self.assertIn("TODOS", fn)


class ExpedientesTemplateContratoTests(unittest.TestCase):
    def test_fila_expone_cedulas_en_partes_y_data_attrs(self):
        tpl = (ROOT / "templates" / "expedientes.html").read_text(encoding="utf-8")
        self.assertIn("demandante_identificacion", tpl)
        self.assertIn("demandado_identificacion", tpl)
        self.assertIn("data-demandante-id", tpl)
        self.assertIn("data-demandado-id", tpl)
        self.assertIn("CC {{ p.demandante_identificacion }}", tpl)
        self.assertIn("CC {{ p.demandado_identificacion }}", tpl)

    def test_filtrar_normaliza_puntos_espacios_guiones(self):
        tpl = (ROOT / "templates" / "expedientes.html").read_text(encoding="utf-8")
        self.assertIn("normalizarIdentificacion", tpl)
        self.assertIn("textoCoincide", tpl)
        self.assertIn(r"[.\s\-]", tpl)
        # Filtros de cartera/estado siguen presentes.
        self.assertIn("coincideCartera", tpl)
        self.assertIn("coincideEstado", tpl)
        self.assertIn("dataset.cartera", tpl)
        self.assertIn("dataset.estado", tpl)


class ExpedientesNormalizacionBusquedaTests(unittest.TestCase):
    def test_cedula_con_puntos_encuentra_sin_formato(self):
        fila = "EXP-0009 JURIDICO DEMANDANTE vs DEUDOR DEMO CC 1.234.567.890"
        self.assertTrue(_texto_coincide(fila, "1234567890"))
        self.assertTrue(_texto_coincide(fila, "1.234.567.890"))
        self.assertTrue(_texto_coincide(fila, "1 234 567 890"))

    def test_cedula_con_guion_nit(self):
        fila = "EXP-0010 PREJURIDICO PH DEMO CC 800123456-1 vs DEUDOR"
        self.assertTrue(_texto_coincide(fila, "8001234561"))
        self.assertTrue(_texto_coincide(fila, "800123456-1"))

    def test_nombre_sigue_encontrando(self):
        fila = "EXP-0009 JURIDICO MARIA LOPEZ vs JUAN PEREZ CC 900100200"
        self.assertTrue(_texto_coincide(fila, "maría"))
        self.assertTrue(_texto_coincide(fila, "juan perez"))

    def test_no_coincide_cedula_ajena(self):
        fila = "EXP-0009 JURIDICO DEMANDANTE vs DEUDOR CC 900100200"
        self.assertFalse(_texto_coincide(fila, "111222333"))


class ExpedientesCargaIdentificacionesTests(unittest.TestCase):
    def test_cargar_incluye_identificaciones_en_filas(self):
        import expedientes_service

        fake_row = {
            "radicado_interno": "EXP-0042",
            "radicado_rama": "",
            "tipo_cartera": "JURIDICO",
            "naturaleza": "EJECUTIVO",
            "juzgado": "",
            "etapa_actual": "",
            "estado": "ACTIVO",
            "pretensiones": None,
            "medidas_cautelares": None,
            "demandante_nombre": "PH DEMO",
            "demandante_identificacion": "800123456-1",
            "demandado_nombre": "DEUDOR DEMO",
            "demandado_identificacion": "1.090.876.543",
            "abogado_asignado": "",
        }
        cur = MagicMock()
        cur.fetchall.return_value = [fake_row]
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)

        with patch.object(expedientes_service.db, "get_connection", return_value=conn):
            lista = expedientes_service.cargar_procesos_general_sin_duplicados("ACTIVOS")

        self.assertEqual(len(lista), 1)
        self.assertEqual(lista[0]["demandante_identificacion"], "800123456-1")
        self.assertEqual(lista[0]["demandado_identificacion"], "1.090.876.543")

        sql = cur.execute.call_args[0][0]
        self.assertIn("demandante_identificacion", sql)
        self.assertIn("demandado_identificacion", sql)
        self.assertIn("c.identificacion", sql)


if __name__ == "__main__":
    unittest.main()
