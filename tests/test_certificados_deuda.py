"""Tests del módulo Certificados de Deuda (agrupación, matching Neon, Word)."""
from __future__ import annotations

import sys
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import certificados_deuda_repository as repo
import certificados_deuda_service as svc
import permisos


class AgruparCapitalTests(unittest.TestCase):
    def test_agrupa_ordinarias_y_extras_con_saldo_acumulado(self):
        items = [
            {
                "fecha": "2024.02.10",
                "concepto": "CUOTA ADMINISTRACION",
                "valor_a_demandar": 100.0,
                "clase_concepto": "capital",
            },
            {
                "fecha": "2024.02.15",
                "concepto": "CUOTA EXTRAORDINARIA ASCENSOR",
                "valor_a_demandar": 50.0,
                "clase_concepto": "capital",
            },
            {
                "fecha": "2024.03.01",
                "concepto": "CUOTA ADMON",
                "valor_a_demandar": 200.0,
                "clase_concepto": "capital",
            },
        ]
        filas = svc.agrupar_capital_limpio(items)
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[0].mes, "FEBRERO")
        self.assertEqual(filas[0].anio, 2024)
        self.assertEqual(filas[0].periodo, "FEBRERO 2024")
        self.assertEqual(filas[0].cuotas_ordinarias, 100.0)
        self.assertEqual(filas[0].cuotas_extraordinarias, 50.0)
        self.assertEqual(filas[0].total_mes, 150.0)
        self.assertEqual(filas[0].saldo, 150.0)
        self.assertEqual(filas[0].vencimiento, "5-feb-24")
        self.assertEqual(filas[1].mes, "MARZO")
        self.assertEqual(filas[1].cuotas_ordinarias, 200.0)
        self.assertEqual(filas[1].cuotas_extraordinarias, 0.0)
        self.assertEqual(filas[1].saldo, 350.0)

    def test_gasto_va_a_extraordinarias(self):
        items = [
            {
                "fecha": "2024.04.01",
                "concepto": "HONORARIOS PREJURIDICO",
                "valor_a_demandar": 80.0,
                "clase_concepto": "gasto",
            }
        ]
        filas = svc.agrupar_capital_limpio(items)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].cuotas_ordinarias, 0.0)
        self.assertEqual(filas[0].cuotas_extraordinarias, 80.0)

    def test_mes_corte_como_fallback_de_fecha(self):
        items = [
            {
                "mes_corte": "2024.05",
                "concepto": "CUOTA ADMINISTRATIVA",
                "valor_a_demandar": 10.0,
            }
        ]
        filas = svc.agrupar_capital_limpio(items)
        self.assertEqual(filas[0].periodo, "MAYO 2024")
        self.assertEqual(filas[0].vencimiento, "5-may-24")


class MatchingUnidadTests(unittest.TestCase):
    """Regresión del 404: PDF 'TORRE X APTO Y' vs Neon 'X-Y' / 'TX-Y'."""

    def test_clave_une_torre_apto_texto_y_bloque_apartamento(self):
        self.assertEqual(repo._clave_unidad("TORRE 1 APTO 201"), "1-201")
        self.assertEqual(repo._clave_unidad("1-201"), "1-201")
        self.assertEqual(
            repo._clave_unidad("", bloque="1", apartamento="201"),
            "1-201",
        )
        self.assertEqual(
            repo._clave_unidad("TORRE 32 APTO N0.502"),
            "32-502",
        )
        self.assertEqual(repo._clave_unidad("BLOQUE 9 APTO 401"), "9-401")

    def test_claves_cruzan_formatos_tipicos_colon_vs_neon(self):
        pdf = repo._claves_unidad(
            "TORRE 1 APTO 201",
            bloque="1",
            apartamento="201",
        )
        neon_simple = repo._claves_unidad("1-201")
        neon_t = repo._claves_unidad("T1-201")
        neon_largo = repo._claves_unidad("TORRE 1 APTO 201")
        self.assertTrue(pdf & neon_simple)
        self.assertTrue(pdf & neon_t)
        self.assertTrue(pdf & neon_largo)
        self.assertTrue(repo._unidad_coincide("1-201", pdf))
        self.assertTrue(repo._unidad_coincide("T1-201", pdf))
        self.assertTrue(repo._unidad_coincide("TORRE 1 APTO 201", pdf))
        self.assertFalse(repo._unidad_coincide("1-202", pdf))

    def test_score_conjunto_ignora_ph_y_acentos(self):
        self.assertGreaterEqual(
            repo._score_conjunto(
                "URBANIZACIÓN SANTA CLARA MANZANA 2 PH",
                "Urbanizacion Santa Clara Manzana 2",
            ),
            90,
        )
        self.assertGreaterEqual(
            repo._score_conjunto(
                "SANTA CLARA MANZANA 2",
                "URBANIZACIÓN SANTA CLARA MANZANA 2 PROPIEDAD HORIZONTAL",
            ),
            70,
        )
        self.assertEqual(
            repo._score_conjunto("OTRO CONJUNTO", "SANTA CLARA"),
            0,
        )

    def test_describir_busqueda_incluye_claves_y_codigo(self):
        txt = repo.describir_busqueda(
            conjunto_nombre="Demo PH",
            bloque="1",
            apartamento="201",
            torre_apto="TORRE 1 APTO 201",
            titular="ANA PEREZ",
            codigo_cuenta="9401",
        )
        self.assertIn("conjunto=", txt)
        self.assertIn("1-201", txt)
        self.assertIn("codigo_cuenta='9401'", txt)
        self.assertIn("no indexa", txt)

    def test_buscar_match_torre_vs_guion_con_filas_en_memoria(self):
        filas = [
            {
                "inmueble_id": 10,
                "torre_apto": "1-201",
                "conjunto_id": 3,
                "conjunto_nombre": "SANTA CLARA MANZANA 2",
                "conjunto_catalogo": "SANTA CLARA MANZANA 2",
                "copropiedad_nombre": "SANTA CLARA MANZANA 2 PH",
                "copropiedad_nit": "900",
                "ciudad": "Pereira",
                "titular_nombre": "ANA PEREZ",
                "titular_cedula": "1",
                "contacto_nombre": None,
                "contacto_cedula": None,
            },
            {
                "inmueble_id": 11,
                "torre_apto": "1-202",
                "conjunto_id": 3,
                "conjunto_nombre": "SANTA CLARA MANZANA 2",
                "conjunto_catalogo": "SANTA CLARA MANZANA 2",
                "copropiedad_nombre": "SANTA CLARA MANZANA 2 PH",
                "copropiedad_nit": "900",
                "ciudad": "Pereira",
                "titular_nombre": "OTRO",
                "titular_cedula": "2",
                "contacto_nombre": None,
                "contacto_cedula": None,
            },
        ]
        filtradas = repo._filtrar_por_conjunto(
            filas,
            conjunto_id=None,
            conjunto_nombre="URBANIZACIÓN SANTA CLARA MANZANA 2",
        )
        self.assertEqual(len(filtradas), 2)
        claves = repo._claves_unidad("TORRE 1 APTO 201", "1", "201")
        hallado = repo._elegir_por_unidad(filtradas, claves)
        self.assertIsNotNone(hallado)
        self.assertEqual(hallado["inmueble_id"], 10)
        self.assertEqual(hallado["torre_apto"], "1-201")

    def test_elegir_desambigua_por_titular(self):
        filas = [
            {
                "inmueble_id": 1,
                "torre_apto": "9-401",
                "titular_nombre": "BANOL RIVERA MAURICIO",
                "titular_cedula": "1",
                "contacto_nombre": None,
                "contacto_cedula": None,
                "conjunto_nombre": "X",
                "conjunto_catalogo": "X",
                "copropiedad_nombre": "X",
                "copropiedad_nit": "1",
                "ciudad": None,
                "conjunto_id": 1,
            },
            {
                "inmueble_id": 2,
                "torre_apto": "9-401",
                "titular_nombre": "OTRO TITULAR",
                "titular_cedula": "2",
                "contacto_nombre": None,
                "contacto_cedula": None,
                "conjunto_nombre": "X",
                "conjunto_catalogo": "X",
                "copropiedad_nombre": "X",
                "copropiedad_nit": "1",
                "ciudad": None,
                "conjunto_id": 1,
            },
        ]
        claves = repo._claves_unidad("", "9", "401")
        hallado = repo._elegir_por_unidad(
            filas, claves, titular="BAÑOL RIVERA MAURICIO"
        )
        self.assertEqual(hallado["inmueble_id"], 1)


class ValidacionDatosTests(unittest.TestCase):
    def test_validar_reporta_faltantes_exactos(self):
        with self.assertRaises(svc.CertificadoDatosFaltantesError) as ctx:
            svc.validar_datos_criticos(
                {
                    "copropiedad_nombre": "",
                    "copropiedad_nit": "900",
                    "titular_nombre": "ANA",
                    "titular_cedula": "",
                    "torre_apto": "101",
                }
            )
        msg = str(ctx.exception)
        self.assertIn("nombre de la copropiedad", msg)
        self.assertIn("cédula del titular", msg)
        self.assertNotIn("NIT de la copropiedad", msg)
        self.assertEqual(len(ctx.exception.faltantes), 2)

    def test_validar_ok_cuando_completos(self):
        svc.validar_datos_criticos(
            {
                "copropiedad_nombre": "PH X",
                "copropiedad_nit": "900",
                "titular_nombre": "ANA",
                "titular_cedula": "1",
                "torre_apto": "T1-101",
            }
        )


class GeneracionWordTests(unittest.TestCase):
    def test_plantilla_oficial_versionada(self):
        self.assertTrue(svc.PLANTILLA_PATH.is_file())
        self.assertEqual(svc.PLANTILLA_PATH.name, "CERTIFICADO_DE_DEUDA.docx")

    def test_render_plantilla_oficial(self):
        ctx = svc.construir_contexto_plantilla(
            datos_neon={
                "copropiedad_nombre": "URBANIZACIÓN DEMO",
                "copropiedad_nit": "900123",
                "torre_apto": "TORRE 1 APTO 101",
                "conjunto_nombre": "URBANIZACIÓN DEMO",
                "titular_nombre": "ANA PEREZ",
                "titular_cedula": "123",
                "ciudad": "Pereira",
            },
            filas=svc.agrupar_capital_limpio(
                [
                    {
                        "fecha": "2024.02.01",
                        "concepto": "CUOTA ADMINISTRACION",
                        "valor_a_demandar": 100,
                    }
                ]
            ),
            representante_nombre="RL DEMO",
            representante_cedula="1.234.567",
        )
        self.assertIn("filas", ctx)
        self.assertEqual(ctx["filas"][0]["mes"], "FEBRERO")
        self.assertEqual(ctx["filas"][0]["anio"], "2024")
        self.assertEqual(ctx["total_saldo"], "$ 100")
        buf = svc.renderizar_docx(ctx)
        raw = buf.getvalue()
        self.assertTrue(raw[:2] == b"PK")
        with zipfile.ZipFile(BytesIO(raw)) as zf:
            self.assertIn("word/document.xml", zf.namelist())
            xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("ANA PEREZ", xml)
            self.assertIn("FEBRERO", xml)

    def test_nombre_archivo_dinamico(self):
        self.assertEqual(
            svc.nombre_archivo_certificado("ANA PÉREZ GOMEZ"),
            "Certificado_ANA_PÉREZ_GOMEZ.docx",
        )

    def test_generar_falla_si_neon_no_encuentra(self):
        with patch(
            "certificados_deuda_service.resolver_datos_certificado",
            return_value=None,
        ):
            with self.assertRaises(svc.CertificadoNoEncontradoError) as ctx:
                svc.generar_certificado_deuda(
                    capital_limpio_a_demandar=[],
                    conjunto="Demo",
                    bloque="1",
                    apartamento="201",
                    torre_apto="TORRE 1 APTO 201",
                    codigo_cuenta="9401",
                )
            msg = str(ctx.exception)
            self.assertIn("Buscado:", msg)
            self.assertIn("1-201", msg)
            self.assertIn("codigo_cuenta", msg)
            self.assertTrue(ctx.exception.criterios)

    def test_generar_falla_si_faltan_criticos(self):
        with patch(
            "certificados_deuda_service.resolver_datos_certificado",
            return_value={
                "inmueble_id": 1,
                "copropiedad_nombre": "PH",
                "copropiedad_nit": "",
                "titular_nombre": "ANA",
                "titular_cedula": "1",
                "torre_apto": "101",
                "conjunto_nombre": "X",
            },
        ):
            with self.assertRaises(svc.CertificadoDatosFaltantesError) as ctx:
                svc.generar_certificado_deuda(
                    capital_limpio_a_demandar=[],
                    inmueble_id=1,
                )
            self.assertIn("NIT de la copropiedad", str(ctx.exception))

    def test_generar_ok_con_neon_mock(self):
        with patch(
            "certificados_deuda_service.resolver_datos_certificado",
            return_value={
                "inmueble_id": 7,
                "copropiedad_nombre": "PH Demo",
                "copropiedad_nit": "900",
                "titular_nombre": "JUAN LOPEZ",
                "titular_cedula": "555",
                "torre_apto": "2-202",
                "conjunto_nombre": "Demo",
                "ciudad": "Pereira",
            },
        ):
            buf, nombre, meta = svc.generar_certificado_deuda(
                capital_limpio_a_demandar=[
                    {
                        "fecha": "2024.06.01",
                        "concepto": "CUOTA ADMINISTRACION",
                        "valor_a_demandar": 40,
                    },
                    {
                        "fecha": "2024.06.01",
                        "concepto": "EXTRAORDINARIA",
                        "valor_a_demandar": 10,
                    },
                ],
                inmueble_id=7,
                representante_nombre="RL",
                representante_cedula="99",
            )
        self.assertEqual(nombre, "Certificado_JUAN_LOPEZ.docx")
        self.assertEqual(meta["inmueble_id"], 7)
        self.assertEqual(meta["filas"], 1)
        self.assertEqual(meta["plantilla"], "CERTIFICADO_DE_DEUDA.docx")
        self.assertTrue(buf.getvalue()[:2] == b"PK")

    def test_generar_pasa_titular_al_resolver(self):
        with patch(
            "certificados_deuda_service.resolver_datos_certificado",
            return_value={
                "inmueble_id": 3,
                "copropiedad_nombre": "PH",
                "copropiedad_nit": "1",
                "titular_nombre": "ANA",
                "titular_cedula": "9",
                "torre_apto": "1-101",
                "conjunto_nombre": "X",
                "ciudad": "Pereira",
            },
        ) as mocked:
            svc.generar_certificado_deuda(
                capital_limpio_a_demandar=[],
                conjunto="X",
                bloque="1",
                apartamento="101",
                titular="ANA PEREZ",
            )
            kwargs = mocked.call_args.kwargs
            self.assertEqual(kwargs["titular"], "ANA PEREZ")
            self.assertEqual(kwargs["bloque"], "1")
            self.assertEqual(kwargs["apartamento"], "101")


class RbacCertificadoTests(unittest.TestCase):
    def test_post_exige_accion_editar(self):
        consulta = permisos.permisos_de_perfil(permisos.PERFIL_CONSULTA)
        abogado = permisos.permisos_de_perfil(permisos.PERFIL_ABOGADO)
        path = "/herramientas/estado-cuenta/certificado-deuda"
        self.assertTrue(permisos.denegar_acceso(consulta, "POST", path))
        self.assertFalse(permisos.denegar_acceso(abogado, "POST", path))


if __name__ == "__main__":
    unittest.main()
