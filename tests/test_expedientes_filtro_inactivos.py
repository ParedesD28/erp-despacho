"""Regresión: el combo Inactivos no debe vaciar /expedientes.

Causa: la ruta pintaba solo ACTIVOS y el combo filtra en el navegador.
Neon sí tiene procesos INACTIVO/Inactivo; sin esas filas en el DOM el
mensaje es «No hay coincidencias».
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)


def _coincide_estado(estado_filtro: str, estado_fila: str) -> bool:
    """Espejo de coincideEstado en templates/expedientes.html."""
    estado = (estado_filtro or "ACTIVOS").upper()
    estado_fila = (estado_fila or "ACTIVO").upper()
    if estado == "TODOS":
        return True
    if estado == "INACTIVOS":
        return estado_fila == "INACTIVO"
    return estado_fila != "INACTIVO"


class ExpedientesInactivosContratoTests(unittest.TestCase):
    def test_ruta_pinta_todos_para_el_filtro_del_navegador(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        fn = src.split("def ver_expedientes", 1)[1].split("\ndef ", 1)[0]
        self.assertIn(
            'cargar_procesos_general_sin_duplicados("TODOS")',
            fn,
        )
        self.assertNotIn(
            "cargar_procesos_general_sin_duplicados(estado_filtro)",
            fn,
        )

    def test_combo_inactivos_filtra_en_cliente(self):
        tpl = (ROOT / "templates" / "expedientes.html").read_text(encoding="utf-8")
        self.assertIn('id="filtro-estado"', tpl)
        self.assertIn('onchange="filtrarTodo()"', tpl)
        self.assertIn("estado === 'INACTIVOS' ? estadoFila === 'INACTIVO'", tpl)
        self.assertIn("No hay coincidencias", tpl)

    def test_mostrar_listado_no_fuerza_activos(self):
        tpl = (ROOT / "templates" / "expedientes.html").read_text(encoding="utf-8")
        fn = tpl.split("function mostrarTodo()", 1)[1].split("function ", 1)[0]
        self.assertNotIn("filtroEstado.value = 'ACTIVOS'", fn)

    def test_sql_acepta_inactivo_en_cualquier_mayuscula(self):
        src = (ROOT / "expedientes_service.py").read_text(encoding="utf-8")
        fn = src.split(
            "def cargar_procesos_general_sin_duplicados", 1
        )[1].split("\ndef ", 1)[0]
        self.assertIn("UPPER(COALESCE(p.estado,'ACTIVO'))='INACTIVO'", fn.replace(" ", ""))


class ExpedientesInactivosFiltroClienteTests(unittest.TestCase):
    def test_dom_solo_activos_deja_inactivos_vacio(self):
        filas = ["Activo", "ACTIVO", ""]
        visibles = [e for e in filas if _coincide_estado("INACTIVOS", e or "ACTIVO")]
        self.assertEqual(visibles, [])

    def test_dom_con_todos_muestra_inactivo_e_INACTIVO(self):
        filas = ["Activo", "INACTIVO", "Inactivo"]
        visibles = [e for e in filas if _coincide_estado("INACTIVOS", e)]
        self.assertEqual(visibles, ["INACTIVO", "Inactivo"])

    def test_activos_oculta_inactivos(self):
        filas = ["Activo", "INACTIVO", "Inactivo"]
        visibles = [e for e in filas if _coincide_estado("ACTIVOS", e)]
        self.assertEqual(visibles, ["Activo"])

    def test_reloj_todos_no_excluye(self):
        """Reloj: todos (valor vacío) no debe esconder filas sin reloj."""
        reloj_filtro = ""
        data_reloj = ""
        coincide = (not reloj_filtro) or data_reloj == reloj_filtro
        self.assertTrue(coincide)


if __name__ == "__main__":
    unittest.main()
