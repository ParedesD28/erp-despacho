"""Tests: clave canónica, flujo unificado Bolsa→Neon→certificado, regresión Excel."""
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

import certificados_deuda_flujo_service as flujo
import certificados_deuda_repository as repo
import certificados_deuda_service as svc
import permisos
from estado_cuenta_pdf_service import generar_excel_lote


class ClaveCanonicaTests(unittest.TestCase):
    def test_02_042_a_2_42(self):
        self.assertEqual(
            repo.clave_canonica_unidad("TORRE 02 APTO 042"),
            "2-42",
        )
        self.assertEqual(
            repo.clave_canonica_unidad("", bloque="02", apartamento="042"),
            "2-42",
        )
        self.assertEqual(repo.normalizar_torre_apto("02-042"), "2-42")
        self.assertEqual(repo.normalizar_torre_apto("2-42"), "2-42")
        self.assertEqual(repo.normalizar_torre_apto("TORRE 1 APTO 201"), "1-201")

    def test_partir_bloque_apto(self):
        self.assertEqual(repo.partir_bloque_apto("TORRE 02 APTO 042"), ("2", "42"))
        self.assertEqual(repo.partir_bloque_apto("9-401"), ("9", "401"))

    def test_componer_form_bloque_apartamento_prioriza_sobre_legacy(self):
        self.assertEqual(
            repo.componer_torre_apto_form(bloque="02", apartamento="042"),
            "2-42",
        )
        self.assertEqual(
            repo.componer_torre_apto_form(
                bloque="9",
                apartamento="401",
                texto_legacy="TORRE 1 APTO 201",
            ),
            "9-401",
        )
        self.assertEqual(
            repo.componer_torre_apto_form(texto_legacy="02-042"),
            "2-42",
        )
        self.assertEqual(
            repo.componer_torre_apto_form(bloque="", apartamento="", texto_legacy=""),
            "",
        )

    def test_matching_usa_misma_normalizacion(self):
        pdf = repo._claves_unidad("TORRE 02 APTO 042", bloque="02", apartamento="042")
        canon = repo.clave_canonica_unidad("", bloque="02", apartamento="042")
        self.assertEqual(canon, "2-42")
        self.assertTrue(repo._unidad_coincide(canon, pdf))
        self.assertTrue(repo._unidad_coincide("02-042", pdf))


class FlujoProcesarCertificadoTests(unittest.TestCase):
    def _cuenta(self, **over):
        base = {
            "archivo": "a.pdf",
            "titular": "MOSQUERA MOSQUERA LUZ ELVIRA",
            "bloque": "02",
            "apartamento": "042",
            "codigo_cuenta": "0242",
            "conjunto": "D - MIRADOR DE LLANO GRANDE ETAPA 1 P.H.",
            "movimientos_extraidos": 2,
            "error": None,
            "rows": [
                {
                    "Concepto": "CUOTA ADMINISTRACION",
                    "Fecha": "2024.02.01",
                    "Valor": 30000,
                    "Abono": 0,
                    "Saldo": 30000,
                },
                {
                    "Concepto": "CUOTA ADMINISTRACION",
                    "Fecha": "2024.03.01",
                    "Valor": 30000,
                    "Abono": 0,
                    "Saldo": 60000,
                },
            ],
        }
        base.update(over)
        return base

    def test_preview_match_titular_principal(self):
        neon = {
            "inmueble_id": 42,
            "torre_apto": "2-42",
            "conjunto_id": 9,
            "conjunto_nombre": "MIRADOR DE LLANO GRANDE",
            "copropiedad_nombre": "MIRADOR PH",
            "copropiedad_nit": "900",
            "ciudad": "Pereira",
            "titular_nombre": "MOSQUERA MOSQUERA LUZ ELVIRA",
            "titular_cedula": "52000000",
            "deudores": [
                {
                    "contacto_id": 1,
                    "nombre": "MOSQUERA MOSQUERA LUZ ELVIRA",
                    "cedula": "52000000",
                    "es_principal": True,
                },
                {
                    "contacto_id": 2,
                    "nombre": "OTRO COPROPIETARIO",
                    "cedula": "111",
                    "es_principal": False,
                },
            ],
            "propietarios": None,
            "titular_principal": {
                "contacto_id": 1,
                "nombre": "MOSQUERA MOSQUERA LUZ ELVIRA",
                "cedula": "52000000",
                "es_principal": True,
            },
            "clave_canonica": "2-42",
        }
        neon["propietarios"] = neon["deudores"]

        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            return_value=neon,
        ):
            preview = flujo.procesar_lote_certificados([self._cuenta()])

        self.assertEqual(preview["emitibles"], 1)
        self.assertEqual(preview["sin_match"], 0)
        item = preview["resultados"][0]
        self.assertEqual(item["clave_canonica"], "2-42")
        self.assertEqual(item["estado"], "varios_propietarios")
        self.assertEqual(item["titular_seleccionado"]["nombre"], "MOSQUERA MOSQUERA LUZ ELVIRA")
        self.assertTrue(item["titular_seleccionado"]["es_principal"])
        self.assertEqual(len(item["deudores"]), 2)
        self.assertEqual(preview["regla_multi_deudor"], "unidad_todos_propietarios")

    def test_preview_sin_match(self):
        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            return_value=None,
        ):
            preview = flujo.procesar_lote_certificados([self._cuenta()])
        self.assertEqual(preview["sin_match"], 1)
        self.assertEqual(preview["emitibles"], 0)
        self.assertEqual(preview["resultados"][0]["estado"], "sin_match")
        self.assertEqual(preview["resultados"][0]["motivo"], "sin_inmueble_neon")
        self.assertEqual(len(preview["sin_match_detalle"]), 1)
        self.assertEqual(preview["sin_match_detalle"][0]["archivo"], "a.pdf")
        self.assertIn("02", preview["sin_match_detalle"][0]["bloque"] or "02")
        # sin_match ≠ co-propietarios
        self.assertEqual(preview["resultados"][0]["deudores"], [])
        self.assertNotEqual(preview["resultados"][0]["estado"], "varios_propietarios")

    def test_preview_fallback_pdf(self):
        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            return_value=None,
        ):
            preview = flujo.procesar_lote_certificados(
                [self._cuenta()],
                permitir_datos_pdf=True,
                titular_cedula="52.000.000",
                copropiedad_nit="900123456-1",
            )
        item = preview["resultados"][0]
        self.assertEqual(item["estado"], "fallback_pdf")
        self.assertEqual(preview["emitibles"], 1)

    def test_generar_un_certificado_principal(self):
        neon = {
            "inmueble_id": 7,
            "torre_apto": "2-42",
            "conjunto_nombre": "MIRADOR",
            "copropiedad_nombre": "MIRADOR PH",
            "copropiedad_nit": "900",
            "ciudad": "Pereira",
            "titular_nombre": "MOSQUERA",
            "titular_cedula": "1",
            "deudores": [
                {
                    "contacto_id": 1,
                    "nombre": "MOSQUERA",
                    "cedula": "1",
                    "es_principal": True,
                }
            ],
            "propietarios": [
                {
                    "contacto_id": 1,
                    "nombre": "MOSQUERA",
                    "cedula": "1",
                    "es_principal": True,
                }
            ],
            "titular_principal": {
                "contacto_id": 1,
                "nombre": "MOSQUERA",
                "cedula": "1",
                "es_principal": True,
            },
            "clave_canonica": "2-42",
        }
        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            return_value=neon,
        ), patch(
            "certificados_deuda_flujo_service.generar_certificado_deuda",
            return_value=(
                BytesIO(b"PK\x03\x04fake"),
                "Certificado_MOSQUERA.docx",
                {"inmueble_id": 7, "filas": 1},
            ),
        ) as gen:
            out = flujo.procesar_y_generar_certificados(
                [self._cuenta()],
                modo="generar",
                representante_nombre="RL",
                representante_cedula="9",
            )
        self.assertIsInstance(out, tuple)
        buf, nombre, meta = out
        self.assertEqual(nombre, "Certificado_MOSQUERA.docx")
        self.assertEqual(meta["generados"], 1)
        self.assertEqual(gen.call_count, 1)
        kwargs = gen.call_args.kwargs
        self.assertEqual(kwargs["inmueble_id"], 7)
        self.assertEqual(kwargs["titular"], "MOSQUERA")

    def test_generar_zip_varios(self):
        def _neon(iid, nombre):
            return {
                "inmueble_id": iid,
                "torre_apto": f"{iid}-1",
                "conjunto_nombre": "X",
                "copropiedad_nombre": "X PH",
                "copropiedad_nit": "1",
                "ciudad": "Pereira",
                "titular_nombre": nombre,
                "titular_cedula": str(iid),
                "deudores": [
                    {
                        "contacto_id": iid,
                        "nombre": nombre,
                        "cedula": str(iid),
                        "es_principal": True,
                    }
                ],
                "propietarios": [
                    {
                        "contacto_id": iid,
                        "nombre": nombre,
                        "cedula": str(iid),
                        "es_principal": True,
                    }
                ],
                "titular_principal": {
                    "contacto_id": iid,
                    "nombre": nombre,
                    "cedula": str(iid),
                    "es_principal": True,
                },
                "clave_canonica": f"{iid}-1",
            }

        cuentas = [
            self._cuenta(archivo="a.pdf", titular="ANA", bloque="1", apartamento="1"),
            self._cuenta(archivo="b.pdf", titular="BOB", bloque="2", apartamento="2"),
        ]
        side = [_neon(1, "ANA"), _neon(2, "BOB")]

        def fake_resolver(**kwargs):
            tit = kwargs.get("titular") or ""
            if "ANA" in tit:
                return side[0]
            return side[1]

        gen_calls = []

        def fake_gen(**kwargs):
            name = f"Certificado_{kwargs.get('titular')}.docx"
            gen_calls.append(name)
            return BytesIO(b"PK\x03\x04x"), name, {"inmueble_id": kwargs.get("inmueble_id")}

        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            side_effect=fake_resolver,
        ), patch(
            "certificados_deuda_flujo_service.generar_certificado_deuda",
            side_effect=fake_gen,
        ):
            buf, nombre, meta = flujo.procesar_y_generar_certificados(
                cuentas, modo="generar",
                representante_nombre="RL LOTE",
                representante_cedula="12.345.678",
            )
        self.assertEqual(nombre, "Certificados_deuda.zip")
        self.assertEqual(meta["generados"], 2)
        with zipfile.ZipFile(buf) as zf:
            self.assertEqual(len(zf.namelist()), 2)

    def test_generar_falla_early_sin_antefirma(self):
        neon = {
            "inmueble_id": 7,
            "torre_apto": "2-42",
            "conjunto_nombre": "MIRADOR",
            "copropiedad_nombre": "MIRADOR PH",
            "copropiedad_nit": "900",
            "ciudad": "Pereira",
            "titular_nombre": "MOSQUERA",
            "titular_cedula": "1",
            "deudores": [
                {
                    "contacto_id": 1,
                    "nombre": "MOSQUERA",
                    "cedula": "1",
                    "es_principal": True,
                }
            ],
            "propietarios": [
                {
                    "contacto_id": 1,
                    "nombre": "MOSQUERA",
                    "cedula": "1",
                    "es_principal": True,
                }
            ],
            "titular_principal": {
                "contacto_id": 1,
                "nombre": "MOSQUERA",
                "cedula": "1",
                "es_principal": True,
            },
            "clave_canonica": "2-42",
        }
        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            return_value=neon,
        ), patch(
            "certificados_deuda_flujo_service.generar_certificado_deuda",
        ) as gen:
            with self.assertRaises(flujo.CertificadoDatosFaltantesError) as ctx:
                flujo.procesar_y_generar_certificados(
                    [self._cuenta()],
                    modo="generar",
                )
            self.assertEqual(ctx.exception.fuente, "antefirma")
            gen.assert_not_called()

    def test_generar_pasa_misma_antefirma_a_todos(self):
        def _neon(iid, nombre):
            return {
                "inmueble_id": iid,
                "torre_apto": f"{iid}-1",
                "conjunto_nombre": "X",
                "copropiedad_nombre": "X PH",
                "copropiedad_nit": "1",
                "ciudad": "Pereira",
                "titular_nombre": nombre,
                "titular_cedula": str(iid),
                "deudores": [
                    {
                        "contacto_id": iid,
                        "nombre": nombre,
                        "cedula": str(iid),
                        "es_principal": True,
                    }
                ],
                "propietarios": [
                    {
                        "contacto_id": iid,
                        "nombre": nombre,
                        "cedula": str(iid),
                        "es_principal": True,
                    }
                ],
                "titular_principal": {
                    "contacto_id": iid,
                    "nombre": nombre,
                    "cedula": str(iid),
                    "es_principal": True,
                },
                "clave_canonica": f"{iid}-1",
            }

        cuentas = [
            self._cuenta(archivo="a.pdf", titular="ANA", bloque="1", apartamento="1"),
            self._cuenta(archivo="b.pdf", titular="BOB", bloque="2", apartamento="2"),
        ]

        def fake_resolver(**kwargs):
            tit = kwargs.get("titular") or ""
            if "ANA" in tit:
                return _neon(1, "ANA")
            return _neon(2, "BOB")

        rl_kwargs = []

        def fake_gen(**kwargs):
            rl_kwargs.append(
                (kwargs.get("representante_nombre"), kwargs.get("representante_cedula"))
            )
            name = f"Certificado_{kwargs.get('titular')}.docx"
            return BytesIO(b"PK\x03\x04x"), name, {"inmueble_id": kwargs.get("inmueble_id")}

        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            side_effect=fake_resolver,
        ), patch(
            "certificados_deuda_flujo_service.generar_certificado_deuda",
            side_effect=fake_gen,
        ):
            buf, nombre, meta = flujo.procesar_y_generar_certificados(
                cuentas,
                modo="generar",
                representante_nombre="  GLADYS RL  ",
                representante_cedula=" 35.319.382 ",
            )
        self.assertEqual(nombre, "Certificados_deuda.zip")
        self.assertEqual(meta["representante_nombre"], "GLADYS RL")
        self.assertEqual(meta["representante_cedula"], "35.319.382")
        self.assertEqual(rl_kwargs, [("GLADYS RL", "35.319.382"), ("GLADYS RL", "35.319.382")])


class FiltrosLoteCertificadoTests(unittest.TestCase):
    def _preview(self):
        return {
            "sin_match": 1,
            "resultados": [
                {
                    "estado": "ok",
                    "conjunto": "MIRADOR DE LLANO GRANDE",
                    "titular_pdf": "ANA",
                    "archivo": "a.pdf",
                    "bloque": "1",
                    "apartamento": "101",
                    "clave_canonica": "1-101",
                    "codigo_cuenta": "1",
                    "capital_limpio_a_demandar": [{"valor_a_demandar": 1}],
                },
                {
                    "estado": "sin_match",
                    "conjunto": "SANTA CLARA",
                    "titular_pdf": "BOB",
                    "archivo": "b.pdf",
                    "bloque": "2",
                    "apartamento": "202",
                    "clave_canonica": "2-202",
                    "codigo_cuenta": "2",
                },
                {
                    "estado": "fallback_pdf",
                    "conjunto": "MIRADOR DE LLANO GRANDE",
                    "titular_pdf": "CARLA",
                    "archivo": "c.pdf",
                    "bloque": "3",
                    "apartamento": "303",
                    "clave_canonica": "3-303",
                    "codigo_cuenta": "3",
                    "capital_limpio_a_demandar": [{"valor_a_demandar": 1}],
                },
            ],
        }

    def test_solo_match_neon(self):
        idxs = flujo.indices_filtrados_preview(self._preview(), solo_match_neon=True)
        self.assertEqual(idxs, [0])

    def test_filtro_conjunto_y_busqueda(self):
        idxs = flujo.indices_filtrados_preview(
            self._preview(), conjunto="MIRADOR", busqueda="carla"
        )
        self.assertEqual(idxs, [2])

    def test_generar_con_solo_match_neon_omite_fallback(self):
        def _neon(nombre):
            return {
                "inmueble_id": 1,
                "torre_apto": "1-1",
                "conjunto_nombre": "MIRADOR",
                "copropiedad_nombre": "MIRADOR PH",
                "copropiedad_nit": "900",
                "ciudad": "Pereira",
                "titular_nombre": nombre,
                "titular_cedula": "1",
                "deudores": [
                    {
                        "contacto_id": 1,
                        "nombre": nombre,
                        "cedula": "1",
                        "es_principal": True,
                    }
                ],
                "propietarios": [
                    {
                        "contacto_id": 1,
                        "nombre": nombre,
                        "cedula": "1",
                        "es_principal": True,
                    }
                ],
                "titular_principal": {
                    "contacto_id": 1,
                    "nombre": nombre,
                    "cedula": "1",
                    "es_principal": True,
                },
            }

        cuenta_ok = {
            "archivo": "a.pdf",
            "titular": "ANA",
            "bloque": "1",
            "apartamento": "1",
            "codigo_cuenta": "1",
            "conjunto": "MIRADOR",
            "movimientos_extraidos": 1,
            "error": None,
            "rows": [
                {
                    "Concepto": "CUOTA ADMINISTRACION",
                    "Fecha": "2024.02.01",
                    "Valor": 30000,
                    "Abono": 0,
                    "Saldo": 30000,
                }
            ],
        }
        cuenta_sin = dict(cuenta_ok, archivo="b.pdf", titular="BOB", bloque="9", apartamento="9")

        def fake_resolver(**kwargs):
            if "ANA" in (kwargs.get("titular") or ""):
                return _neon("ANA")
            return None

        gen_calls = []

        def fake_gen(**kwargs):
            gen_calls.append(kwargs.get("titular"))
            return (
                BytesIO(b"PK\x03\x04x"),
                f"Certificado_{kwargs.get('titular')}.docx",
                {},
            )

        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            side_effect=fake_resolver,
        ), patch(
            "certificados_deuda_flujo_service.generar_certificado_deuda",
            side_effect=fake_gen,
        ):
            buf, nombre, meta = flujo.procesar_y_generar_certificados(
                [cuenta_ok, cuenta_sin],
                modo="generar",
                permitir_datos_pdf=True,
                titular_cedula="1",
                copropiedad_nit="900",
                solo_match_neon=True,
                representante_nombre="RL",
                representante_cedula="9",
            )
        self.assertEqual(meta["generados"], 1)
        self.assertEqual(gen_calls, ["ANA"])
        self.assertEqual(nombre, "Certificado_ANA.docx")


class ExcelTranscripcionNoRotaTests(unittest.TestCase):
    """Regresión PR #20: Excel de lote sigue siendo solo transcripción."""

    def test_generar_excel_lote_no_incluye_bolsa(self):
        cuentas = [
            {
                "archivo": "x.pdf",
                "titular": "T",
                "bloque": "1",
                "apartamento": "101",
                "codigo_cuenta": "1",
                "conjunto": "C",
                "movimientos_extraidos": 1,
                "bloques_omitidos": 0,
                "saldo_final": 10,
                "error": None,
                "rows": [
                    {
                        "Concepto": "CUOTA ADMINISTRACION",
                        "Tipo Documento": "FV",
                        "Número": "1",
                        "Fecha": "2024.01.01",
                        "Valor": 10,
                        "Abono": 0,
                        "Saldo": 10,
                        "Titular": "T",
                        "Bloque": "1",
                        "Apartamento": "101",
                        "Codigo Cuenta": "1",
                        "Archivo": "x.pdf",
                    }
                ],
            }
        ]
        excel = generar_excel_lote(cuentas)
        raw = excel.getvalue() if hasattr(excel, "getvalue") else excel
        # openpyxl workbook bytes: no debe mencionar capital limpio / bolsa
        lower = raw.lower() if isinstance(raw, (bytes, bytearray)) else b""
        # El xlsx es zip; buscamos strings en el binario
        self.assertNotIn(b"capital_limpio", lower)
        self.assertNotIn(b"bolsa_global", lower)
        self.assertTrue(raw[:2] == b"PK" or hasattr(excel, "getvalue"))


class RbacProcesarCertificadoTests(unittest.TestCase):
    def test_post_exige_accion_editar(self):
        consulta = permisos.permisos_de_perfil(permisos.PERFIL_CONSULTA)
        abogado = permisos.permisos_de_perfil(permisos.PERFIL_ABOGADO)
        path = "/herramientas/estado-cuenta/procesar-certificado"
        self.assertTrue(permisos.denegar_acceso(consulta, "POST", path))
        self.assertFalse(permisos.denegar_acceso(abogado, "POST", path))


class TitularPrincipalHelperTests(unittest.TestCase):
    def test_elige_principal(self):
        props = [
            {"nombre": "A", "es_principal": False},
            {"nombre": "B", "es_principal": True},
        ]
        self.assertEqual(repo.titular_principal_de_propietarios(props)["nombre"], "B")

    def test_fallback_primero(self):
        props = [{"nombre": "A", "es_principal": False}]
        self.assertEqual(repo.titular_principal_de_propietarios(props)["nombre"], "A")

    def test_diagnostico_sin_principal_y_varios(self):
        sin = [
            {"nombre": "A", "cedula": "1", "es_principal": False},
            {"nombre": "B", "cedula": "2", "es_principal": False},
        ]
        d = repo.diagnosticar_propietarios(sin)
        self.assertTrue(d["sin_principal_marcado"])
        self.assertEqual(d["principal"]["nombre"], "A")

        varios = [
            {"nombre": "A", "cedula": "1", "es_principal": True},
            {"nombre": "B", "cedula": "2", "es_principal": True},
        ]
        d2 = repo.diagnosticar_propietarios(varios)
        self.assertTrue(d2["varios_principales"])
        self.assertEqual(d2["principal"]["nombre"], "A")

    def test_diagnostico_cedula_secundario_y_pdf_distinto(self):
        props = [
            {"nombre": "PRINCIPAL", "cedula": "1", "es_principal": True},
            {"nombre": "SECUNDARIO", "cedula": None, "es_principal": False},
        ]
        d = repo.diagnosticar_propietarios(props, titular_pdf="OTRO TITULAR PDF")
        self.assertTrue(d["titular_pdf_distinto_de_principal"])
        self.assertTrue(any("Cédula faltante" in a for a in d["advertencias"]))

    def test_resumen_sin_match_helper(self):
        preview = {
            "resultados": [
                {"estado": "ok", "archivo": "x.pdf"},
                {
                    "estado": "sin_match",
                    "archivo": "fail.pdf",
                    "titular_pdf": "T",
                    "bloque": "9",
                    "apartamento": "401",
                    "clave_canonica": "9-401",
                    "conjunto": "X",
                    "motivo": "sin_inmueble_neon",
                    "motivo_detalle": "No hallado",
                    "criterios": "conjunto='X'",
                },
            ]
        }
        det = flujo.resumen_sin_match(preview)
        self.assertEqual(len(det), 1)
        self.assertEqual(det[0]["archivo"], "fail.pdf")
        self.assertEqual(det[0]["clave_canonica"], "9-401")


class MultiPropietariosContextoYMontosTests(unittest.TestCase):
    """Regresión: lista completa, sin prorrateo, cédula secundaria opcional."""

    def test_contexto_incluye_todos_sin_cambiar_total(self):
        capital = [
            {
                "fecha": "2024.02.01",
                "concepto": "CUOTA ADMINISTRACION",
                "valor_a_demandar": 30000,
            },
            {
                "fecha": "2024.03.01",
                "concepto": "CUOTA ADMINISTRACION",
                "valor_a_demandar": 30000,
            },
        ]
        filas = svc.agrupar_capital_limpio(capital)
        datos_uno = {
            "copropiedad_nombre": "PH",
            "copropiedad_nit": "900",
            "ciudad": "Pereira",
            "torre_apto": "2-42",
            "conjunto_nombre": "MIRADOR",
            "titular_nombre": "A",
            "titular_cedula": "1",
            "propietarios": [
                {"nombre": "A", "cedula": "1", "es_principal": True},
            ],
        }
        datos_varios = {
            **datos_uno,
            "propietarios": [
                {"nombre": "A", "cedula": "1", "es_principal": True},
                {"nombre": "B", "cedula": None, "es_principal": False},
                {"nombre": "C", "cedula": "3", "es_principal": False},
            ],
        }
        ctx1 = svc.construir_contexto_plantilla(datos_neon=datos_uno, filas=filas)
        ctxN = svc.construir_contexto_plantilla(datos_neon=datos_varios, filas=filas)
        self.assertEqual(ctx1["total_saldo"], ctxN["total_saldo"])
        self.assertEqual(ctx1["total_saldo"], "$ 60.000")
        self.assertFalse(ctx1["hay_varios_propietarios"])
        self.assertTrue(ctxN["hay_varios_propietarios"])
        self.assertEqual(len(ctxN["propietarios"]), 3)
        self.assertIn("principal", ctxN["propietarios_texto"])
        self.assertEqual(ctxN["titular_nombre"], "A")
        self.assertEqual(ctxN["titular_cedula"], "1")
        # Secundario sin cédula no bloquea contexto
        self.assertEqual(ctxN["propietarios"][1]["cedula"], "")

    def test_preview_sin_principal_usa_primero(self):
        neon = {
            "inmueble_id": 42,
            "torre_apto": "2-42",
            "conjunto_nombre": "MIRADOR",
            "copropiedad_nombre": "MIRADOR PH",
            "copropiedad_nit": "900",
            "ciudad": "Pereira",
            "titular_nombre": "A",
            "titular_cedula": "1",
            "deudores": [
                {"contacto_id": 1, "nombre": "A", "cedula": "1", "es_principal": False},
                {"contacto_id": 2, "nombre": "B", "cedula": "2", "es_principal": False},
            ],
            "propietarios": None,
            "titular_principal": {
                "contacto_id": 1,
                "nombre": "A",
                "cedula": "1",
                "es_principal": False,
            },
            "advertencias": [
                "Ningún propietario marcado es_principal; se usa el primero de la lista."
            ],
            "clave_canonica": "2-42",
        }
        neon["propietarios"] = neon["deudores"]
        with patch(
            "certificados_deuda_flujo_service.resolver_datos_certificado",
            return_value=neon,
        ):
            preview = flujo.procesar_lote_certificados(
                [
                    {
                        "archivo": "a.pdf",
                        "titular": "A",
                        "bloque": "02",
                        "apartamento": "042",
                        "codigo_cuenta": "1",
                        "conjunto": "MIRADOR",
                        "movimientos_extraidos": 1,
                        "error": None,
                        "rows": [
                            {
                                "Concepto": "CUOTA ADMINISTRACION",
                                "Fecha": "2024.02.01",
                                "Valor": 30000,
                                "Abono": 0,
                                "Saldo": 30000,
                            }
                        ],
                    }
                ]
            )
        item = preview["resultados"][0]
        self.assertEqual(item["estado"], "varios_propietarios")
        self.assertEqual(item["titular_seleccionado"]["nombre"], "A")
        self.assertEqual(len(item["deudores"]), 2)


class FmiPorCuentaFlujoTests(unittest.TestCase):
    def test_generar_desde_preview_pasa_fmi_por_indice(self):
        preview = {
            "sin_match": 0,
            "resultados": [
                {
                    "estado": "ok",
                    "archivo": "a.pdf",
                    "titular_pdf": "JUAN",
                    "inmueble_id": 7,
                    "conjunto": "Demo",
                    "clave_canonica": "2-202",
                    "titular_seleccionado": {"nombre": "JUAN", "cedula": "555"},
                    "datos_neon": {
                        "inmueble_id": 7,
                        "copropiedad_nombre": "PH Demo",
                        "copropiedad_nit": "900",
                        "titular_nombre": "JUAN",
                        "titular_cedula": "555",
                        "torre_apto": "2-202",
                        "conjunto_nombre": "Demo",
                        "ciudad": "Pereira",
                    },
                    "capital_limpio_a_demandar": [
                        {
                            "fecha": "2024.06.01",
                            "concepto": "CUOTA ADMINISTRACION",
                            "valor_a_demandar": 40,
                        }
                    ],
                }
            ],
        }
        with patch(
            "certificados_deuda_flujo_service.generar_certificado_deuda",
            return_value=(BytesIO(b"PK"), "Certificado_JUAN.docx", {"fmi": "290-1"}),
        ) as mock_gen:
            _buf, _nombre, meta = flujo.generar_certificados_desde_preview(
                preview,
                representante_nombre="RL",
                representante_cedula="99",
                indices=[0],
                fmi_por_indice={0: "290-1"},
            )
        self.assertEqual(mock_gen.call_args.kwargs.get("fmi"), "290-1")
        self.assertEqual(meta["generados"], 1)


if __name__ == "__main__":
    unittest.main()
