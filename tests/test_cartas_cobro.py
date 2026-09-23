"""Tests unitarios del módulo de cartas de cobro Word."""
from __future__ import annotations

import sys
import unittest
import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cartas_cobro_service as svc


class CartasCobroUnitTests(unittest.TestCase):
    def test_fecha_limite_default_mas_cinco_dias(self):
        self.assertEqual(svc.fecha_limite_default(date(2026, 9, 22)), date(2026, 9, 27))

    def test_dedupe_por_inmueble_conserva_primero(self):
        filas = [
            {"inmueble_id": 1, "deudor_nombre": "A"},
            {"inmueble_id": 1, "deudor_nombre": "B"},
            {"inmueble_id": 2, "deudor_nombre": "C"},
        ]
        out = svc.dedupe_por_inmueble(filas)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["deudor_nombre"], "A")
        self.assertEqual(out[1]["inmueble_id"], 2)

    def test_resolver_seleccion_rechaza_duplicados_y_faltantes(self):
        candidatos = [
            {"inmueble_id": 10, "saldo_total": 1000},
            {"inmueble_id": 20, "saldo_total": 2000},
        ]
        sel = svc.resolver_seleccion(candidatos, [20, 20, 10])
        self.assertEqual([c["inmueble_id"] for c in sel], [20, 10])
        with self.assertRaises(ValueError):
            svc.resolver_seleccion(candidatos, [99])
        with self.assertRaises(ValueError):
            svc.resolver_seleccion(candidatos, [])

    def test_catalogo_variables_incluye_claves_clave(self):
        claves = {v["clave"] for v in svc.PLANTILLA_VARIABLES}
        for requerida in (
            "deudor_nombre", "cedula", "torre_apto", "conjunto", "nombre_ph",
            "monto", "fecha_limite", "atn_codeudor", "telefono_despacho",
        ):
            self.assertIn(requerida, claves)

    def test_renderizar_cuerpo_y_atn_vacio(self):
        candidato = {
            "deudor_nombre": "ANA",
            "deudor_identificacion": "1",
            "torre_apto": "101",
            "conjunto_residencial": "Demo",
            "nombre_ph": "PH Demo",
            "saldo_total": 2500,
            "codeudor_nombre": None,
        }
        ctx = svc.construir_contexto_variables(candidato, fecha_limite=date(2026, 9, 27))
        self.assertEqual(ctx["atn_codeudor"], "")
        self.assertEqual(ctx["monto"], "$ 2.500")
        texto = svc.renderizar_cuerpo(
            "Hola {{deudor_nombre}} {{atn_codeudor}} total {{monto}}",
            ctx,
        )
        self.assertEqual(texto, "Hola ANA  total $ 2.500")

        ctx2 = svc.construir_contexto_variables(
            {**candidato, "codeudor_nombre": "JUAN"},
            fecha_limite=date(2026, 9, 27),
        )
        self.assertEqual(ctx2["atn_codeudor"], "Atn. JUAN")

    def test_guardar_plantilla_valida_campos(self):
        with self.assertRaises(ValueError):
            svc.guardar_plantilla(nombre="", tipo_cartera="PREJURIDICO", cuerpo="x")
        with self.assertRaises(ValueError):
            svc.guardar_plantilla(nombre="A", tipo_cartera="OTRO", cuerpo="x")
        with self.assertRaises(ValueError):
            svc.guardar_plantilla(nombre="A", tipo_cartera="PREJURIDICO", cuerpo="  ")

    def test_docx_desde_plantilla_personalizada(self):
        candidato = {
            "inmueble_id": 3,
            "deudor_nombre": "ANA PEREZ",
            "deudor_identificacion": "123",
            "torre_apto": "T1",
            "conjunto_residencial": "Demo",
            "nombre_ph": "PH Demo",
            "saldo_total": 1000,
            "codeudor_nombre": "JUAN",
        }
        cuerpo = (
            "Señor(a)\n{{deudor_nombre}}\n{{atn_codeudor}}\n"
            "REF: CUSTOM - {{nombre_ph}}\n\n"
            "Respetado(a) señor(a),\n\n"
            "Debe {{monto}} antes del {{fecha_limite}}.\n\n"
            "Atentamente,\n\n{{firmante_nombre}}"
        )
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt

        doc = Document(
            BytesIO(
                svc.construir_carta_docx(
                    candidato,
                    fecha_limite=date(2026, 9, 27),
                    cuerpo_plantilla=cuerpo,
                )
            )
        )
        texto = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("ANA PEREZ", texto)
        self.assertIn("Atn. JUAN", texto)
        self.assertIn("REF: CUSTOM - PH Demo", texto)
        self.assertIn("$ 1.000", texto)
        self.assertIn("27 de septiembre de 2026", texto)
        just = [p for p in doc.paragraphs if p.text.startswith("Debe $")]
        self.assertTrue(just)
        self.assertEqual(just[0].alignment, WD_ALIGN_PARAGRAPH.JUSTIFY)
        for p in doc.paragraphs:
            for run in p.runs:
                if run.text.strip():
                    self.assertEqual(run.font.name, "Arial")
                    self.assertEqual(run.font.size, Pt(11))

    def test_docx_incluye_campos_y_atn_codeudor(self):
        candidato = {
            "inmueble_id": 7,
            "deudor_nombre": "ANA PEREZ",
            "deudor_identificacion": "123456",
            "torre_apto": "T1-101",
            "conjunto_residencial": "Altos del Parque",
            "nombre_ph": "PH Altos del Parque",
            "saldo_total": 1500000,
            "codeudor_nombre": "JUAN PEREZ",
        }
        data = svc.construir_carta_docx(
            candidato,
            fecha_limite=date(2026, 9, 27),
            fecha_carta=date(2026, 9, 22),
        )
        self.assertTrue(data.startswith(b"PK"))
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt

        doc = Document(BytesIO(data))
        texto = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("Señor(a)", texto)
        self.assertIn("ANA PEREZ", texto)
        self.assertIn("C.C. 123456", texto)
        self.assertIn("Propietario(a) del (T1-101) (Altos del Parque) Pereira.", texto)
        self.assertIn("Atn. JUAN PEREZ", texto)
        self.assertIn("REF: REQUERIMIENTO DE PAGO PREJURÍDICO - PH Altos del Parque", texto)
        self.assertIn("Respetado(a) señor(a),", texto)
        self.assertIn("Actuando en mi calidad de apoderado legal de PH Altos del Parque", texto)
        self.assertIn("suma total de $ 1.500.000", texto)
        self.assertIn("El bienestar y mantenimiento", texto)
        self.assertIn("Le otorgamos un plazo máximo hasta el 27 de septiembre de 2026", texto)
        self.assertIn("• Teléfono / WhatsApp: 310 6927812", texto)
        self.assertIn("• Correo electrónico: notificacionesdiegoparedes@outlook.com", texto)
        self.assertIn("Hacemos de su conocimiento que", texto)
        self.assertIn("Confiamos en su voluntad", texto)
        self.assertIn("Diego Alejandro Paredes García", texto)
        self.assertIn("Abogado Apoderado", texto)
        # Sin encabezado de ciudad/fecha de carta
        self.assertNotIn("Pereira, 22 de septiembre", texto)

        # Tipografía y justificación del cuerpo
        runs_arial = []
        for p in doc.paragraphs:
            for run in p.runs:
                if run.text.strip():
                    self.assertEqual(run.font.name, "Arial")
                    self.assertEqual(run.font.size, Pt(11))
                    runs_arial.append(run)
        self.assertTrue(runs_arial)
        justificados = [
            p for p in doc.paragraphs
            if p.text.startswith("Actuando en mi calidad")
            or p.text.startswith("A la fecha")
            or p.text.startswith("El bienestar")
        ]
        self.assertTrue(justificados)
        for p in justificados:
            self.assertEqual(p.alignment, WD_ALIGN_PARAGRAPH.JUSTIFY)

    def test_docx_sin_codeudor_no_incluye_atn(self):
        candidato = {
            "inmueble_id": 8,
            "deudor_nombre": "SOLO DEUDOR",
            "deudor_identificacion": "999",
            "torre_apto": "A-2",
            "conjunto_residencial": "Conjunto X",
            "nombre_ph": "PH X",
            "saldo_total": 100,
            "codeudor_nombre": None,
        }
        from docx import Document

        doc = Document(
            BytesIO(
                svc.construir_carta_docx(
                    candidato,
                    fecha_limite=date(2026, 10, 1),
                    fecha_carta=date(2026, 9, 22),
                )
            )
        )
        texto = "\n".join(p.text for p in doc.paragraphs)
        self.assertNotIn("Atn.", texto)
        self.assertIn("Respetado(a) señor(a),", texto)
        self.assertIn("• Teléfono / WhatsApp: 310 6927812", texto)

    def test_paquete_zip_varias_cartas(self):
        items = [
            {
                "inmueble_id": 1,
                "deudor_nombre": "A",
                "deudor_identificacion": "1",
                "torre_apto": "101",
                "conjunto_residencial": "Demo",
                "nombre_ph": "PH Demo",
                "saldo_total": 10,
            },
            {
                "inmueble_id": 2,
                "deudor_nombre": "B",
                "deudor_identificacion": "2",
                "torre_apto": "102",
                "conjunto_residencial": "Demo",
                "nombre_ph": "PH Demo",
                "saldo_total": 20,
            },
        ]
        contenido, filename, media = svc.generar_paquete_docx(
            items, fecha_limite=date(2026, 9, 30), fecha_carta=date(2026, 9, 22)
        )
        self.assertEqual(media, "application/zip")
        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(BytesIO(contenido)) as zf:
            names = zf.namelist()
            self.assertEqual(len(names), 2)
            self.assertTrue(all(n.endswith(".docx") for n in names))

    def test_listar_candidatos_excluye_saldo_no_verificado(self):
        filas = [
            {
                "inmueble_id": 1,
                "obligacion_id": 11,
                "deudor_nombre": "OK",
                "deudor_identificacion": "1",
                "torre_apto": "1",
                "conjunto_residencial": "C",
                "nombre_ph": "PH",
                "nit_ph": "",
                "tipo_cartera": "PREJURIDICO",
                "radicado_interno": None,
                "codeudor_nombre": None,
                "codeudor_identificacion": None,
                "conjunto_id": 5,
                "deudor_contacto_id": 9,
            },
            {
                "inmueble_id": 2,
                "obligacion_id": 22,
                "deudor_nombre": "BAD",
                "deudor_identificacion": "2",
                "torre_apto": "2",
                "conjunto_residencial": "C",
                "nombre_ph": "PH",
                "nit_ph": "",
                "tipo_cartera": "PREJURIDICO",
                "radicado_interno": None,
                "codeudor_nombre": None,
                "codeudor_identificacion": None,
                "conjunto_id": 5,
                "deudor_contacto_id": 8,
            },
        ]

        class FakeCur:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, *args, **kwargs):
                return None

            def fetchall(self):
                return filas

        class FakeConn:
            def cursor(self, cursor_factory=None):
                return FakeCur()

            def release(self):
                return None

        def fake_saldo(oid, fecha_corte=None):
            if oid == 11:
                return {"saldo_verificado": True, "saldo_total": 5000, "saldo_fuente": "LIQUIDADOR_PH"}
            return {"saldo_verificado": False, "saldo_total": None, "saldo_fuente": "ERROR"}

        with patch.object(svc.db, "get_connection", return_value=FakeConn()), patch.object(
            svc.catalogos_service,
            "obtener_conjunto",
            return_value={"id": 5, "nombre": "C", "persona_juridica": "PH", "nit": "900"},
        ), patch.object(
            svc.obligacion_saldo_service, "calcular_saldo_obligacion", side_effect=fake_saldo
        ):
            out = svc.listar_candidatos(conjunto_id=5, fecha_corte=date(2026, 9, 22))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["inmueble_id"], 1)
        self.assertEqual(out[0]["saldo_total"], 5000.0)


if __name__ == "__main__":
    unittest.main()
