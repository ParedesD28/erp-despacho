"""Tests de carga masiva FMI (pegar lista / CSV / plantilla)."""

from __future__ import annotations

import unittest

from certificados_fmi_masivo import (
    aplicar_fmi_lista,
    mapear_csv_a_indices,
    parse_fmi_csv,
    parse_fmi_lista,
    plantilla_csv_lote,
)


def _row(**kwargs):
    base = {
        "archivo": "",
        "clave_canonica": "",
        "codigo_cuenta": "",
        "estado": "ok",
        "capital_limpio_a_demandar": [{"x": 1}],
    }
    base.update(kwargs)
    return base


class ParseFmiListaTests(unittest.TestCase):
    def test_lineas_y_vacios(self):
        self.assertEqual(parse_fmi_lista("290-1\n\n290-2\n"), ["290-1", "", "290-2"])
        self.assertEqual(parse_fmi_lista(""), [])
        self.assertEqual(parse_fmi_lista("  290-9  "), ["290-9"])


class AplicarListaTests(unittest.TestCase):
    def test_mismo_orden_que_destino(self):
        res = aplicar_fmi_lista(
            ["A", "B", "C"],
            [2, 5, 7],
            mapa_existente={0: "Z"},
        )
        self.assertEqual(res["mapa"], {0: "Z", 2: "A", 5: "B", 7: "C"})
        self.assertEqual(res["aplicadas"], 3)
        self.assertEqual(res["sobrantes"], 0)
        self.assertEqual(res["faltantes"], 0)

    def test_sobrantes_y_faltantes(self):
        corto = aplicar_fmi_lista(["A"], [0, 1, 2])
        self.assertEqual(corto["faltantes"], 2)
        self.assertEqual(corto["mapa"][0], "A")
        self.assertNotIn(1, corto["mapa"])

        largo = aplicar_fmi_lista(["A", "B", "C", "D"], [10, 11])
        self.assertEqual(largo["sobrantes"], 2)
        self.assertEqual(largo["aplicadas"], 2)

    def test_vacio_limpia(self):
        res = aplicar_fmi_lista([""], [3], mapa_existente={3: "viejo"})
        self.assertEqual(res["mapa"][3], "")


class ParseCsvTests(unittest.TestCase):
    def test_por_indice(self):
        texto = "indice,fmi\n0,290-1\n2,290-2\n"
        filas = parse_fmi_csv(texto)
        self.assertEqual(filas, [
            {"indice": "0", "fmi": "290-1"},
            {"indice": "2", "fmi": "290-2"},
        ])

    def test_punto_y_coma_y_aliases(self):
        texto = "archivo;folio\na.pdf;290-9\n"
        filas = parse_fmi_csv(texto)
        self.assertEqual(filas[0]["archivo"], "a.pdf")
        self.assertEqual(filas[0]["fmi"], "290-9")

    def test_exige_fmi_y_clave(self):
        with self.assertRaises(ValueError):
            parse_fmi_csv("indice,x\n0,1\n")
        with self.assertRaises(ValueError):
            parse_fmi_csv("fmi\n290-1\n")


class MapearCsvTests(unittest.TestCase):
    def setUp(self):
        self.resultados = [
            _row(archivo="a.pdf", clave_canonica="B1-101", codigo_cuenta="C-1"),
            _row(archivo="b.pdf", clave_canonica="B2-202", codigo_cuenta="C-2"),
            _row(
                archivo="c.pdf",
                clave_canonica="B3-303",
                codigo_cuenta="C-3",
                estado="sin_match",
                capital_limpio_a_demandar=[],
            ),
        ]

    def test_por_indice(self):
        filas = parse_fmi_csv("indice,fmi\n1,290-B\n")
        res = mapear_csv_a_indices(filas, self.resultados)
        self.assertEqual(res["mapa"][1], "290-B")
        self.assertEqual(res["aplicadas"], 1)

    def test_por_archivo_y_torre(self):
        filas = parse_fmi_csv(
            "archivo,fmi\na.pdf,290-A\n"
        )
        res = mapear_csv_a_indices(filas, self.resultados)
        self.assertEqual(res["mapa"][0], "290-A")

        filas2 = parse_fmi_csv("torre_apto,fmi\nB2-202,290-X\n")
        res2 = mapear_csv_a_indices(filas2, self.resultados)
        self.assertEqual(res2["mapa"][1], "290-X")

    def test_ambiguo_y_sin_match(self):
        resultados = [
            _row(archivo="dup.pdf", clave_canonica="X"),
            _row(archivo="dup.pdf", clave_canonica="Y"),
        ]
        filas = parse_fmi_csv("archivo,fmi\ndup.pdf,1\nno.pdf,2\n")
        res = mapear_csv_a_indices(filas, resultados)
        self.assertEqual(res["aplicadas"], 0)
        self.assertTrue(any("ambiguo" in e for e in res["errores"]))
        self.assertTrue(any("sin match" in e for e in res["errores"]))

    def test_fuera_de_rango(self):
        filas = parse_fmi_csv("indice,fmi\n99,290\n")
        res = mapear_csv_a_indices(filas, self.resultados)
        self.assertEqual(res["aplicadas"], 0)
        self.assertTrue(any("fuera de rango" in e for e in res["errores"]))


class PlantillaCsvTests(unittest.TestCase):
    def test_plantilla_solo_emitibles(self):
        resultados = [
            _row(archivo="a.pdf", clave_canonica="B1-101", codigo_cuenta="C-1"),
            _row(
                archivo="bad.pdf",
                clave_canonica="Z",
                estado="sin_match",
                capital_limpio_a_demandar=[],
            ),
            _row(archivo="c.pdf", clave_canonica="B3-303", codigo_cuenta="C-3"),
        ]
        csv_txt = plantilla_csv_lote(resultados, fmi_por_indice={0: "290-1"})
        lines = [ln for ln in csv_txt.strip().splitlines()]
        self.assertEqual(lines[0], "indice,archivo,torre_apto,codigo_cuenta,fmi")
        self.assertEqual(len(lines), 3)  # header + 2 emitibles
        self.assertIn("0,a.pdf,B1-101,C-1,290-1", lines[1])
        self.assertIn("2,c.pdf,B3-303,C-3,", lines[2])


if __name__ == "__main__":
    unittest.main()
