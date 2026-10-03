"""
Regresión: descarga de certificados multi-grupo (UI batch) no debe perder
cuentas por colisión de nombre en el ZIP externo.

Causa histórica (31 emitibles → ~16 descargados):
  BATCH_SIZE=15 ⇒ grupos 15 + 16; cada grupo devolvía Certificados_deuda.zip;
  JSZip.file(mismo_nombre) pisaba el primero y solo quedaba el último grupo.

Causa adicional (N → N-1): renombre A.docx×2 → A_1.docx sin registrar el
nombre nuevo; un A_1.docx natural colisionaba y JSZip/extractores colapsaban.
"""
from __future__ import annotations

import io
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def unique_zip_entry_name(usados: set[str], name: str) -> str:
    """Espejo de uniqueZipEntryName() / nombre_unico_entrada_zip()."""
    base = name or "Certificado_deuda.docx"
    if base not in usados:
        usados.add(base)
        return base
    stem, sep, ext = base.rpartition(".")
    n = 1
    while True:
        candidate = f"{stem}_{n}.{ext}" if sep and stem else f"{base}_{n}"
        if candidate not in usados:
            usados.add(candidate)
            return candidate
        n += 1


def empaquetar_certificados_planos(
    partes: list[tuple[str, bytes]],
) -> tuple[dict[str, bytes], int]:
    """
    Espejo de empaquetarCertificadosPlanos(): aplana ZIP/docx del servidor
    en un mapa plano con nombres únicos (sin anidar ni pisar).
    """
    out: dict[str, bytes] = {}
    usados: set[str] = set()
    total = 0
    for filename, raw in partes:
        lower = (filename or "").lower()
        if lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    leaf = info.filename.split("/")[-1] or info.filename
                    key = unique_zip_entry_name(usados, leaf)
                    out[key] = zf.read(info)
                    total += 1
        else:
            key = unique_zip_entry_name(usados, filename or "certificado.docx")
            out[key] = raw
            total += 1
    return out, total


def _zip_de_nombres(nombres: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for n in nombres:
            zf.writestr(n, f"contenido:{n}".encode())
    return buf.getvalue()


class ColisionNombreZipTests(unittest.TestCase):
    def test_jszip_style_overwrite_pierde_primer_grupo(self):
        """Reproduce el bug: mismo path ⇒ última escritura gana."""
        entries: dict[str, bytes] = {}
        entries["Certificados_deuda.zip"] = b"grupo1-15-certs"
        entries["Certificados_deuda.zip"] = b"grupo2-16-certs"
        self.assertEqual(entries["Certificados_deuda.zip"], b"grupo2-16-certs")
        self.assertEqual(len(entries), 1)

    def test_aplanar_dos_grupos_conserva_31(self):
        grupo1 = _zip_de_nombres([f"Certificado_A_{i}.docx" for i in range(15)])
        grupo2 = _zip_de_nombres([f"Certificado_B_{i}.docx" for i in range(16)])
        planos, total = empaquetar_certificados_planos(
            [
                ("Certificados_deuda.zip", grupo1),
                ("Certificados_deuda.zip", grupo2),  # mismo nombre servidor
            ]
        )
        self.assertEqual(total, 31)
        self.assertEqual(len(planos), 31)

    def test_nombres_duplicados_entre_grupos_reciben_sufijo(self):
        g1 = _zip_de_nombres(["Certificado_Mirador_2-1.docx"])
        g2 = _zip_de_nombres(["Certificado_Mirador_2-1.docx"])
        planos, total = empaquetar_certificados_planos(
            [
                ("Certificados_deuda.zip", g1),
                ("Certificados_deuda.zip", g2),
            ]
        )
        self.assertEqual(total, 2)
        self.assertIn("Certificado_Mirador_2-1.docx", planos)
        self.assertIn("Certificado_Mirador_2-1_1.docx", planos)

    def test_renombre_no_pisa_nombre_natural_igual(self):
        """A.docx×2 + A_1.docx natural → 3 entradas distintas (no 61→60)."""
        usados: set[str] = set()
        names = [
            unique_zip_entry_name(usados, "Certificado_PEREZ.docx"),
            unique_zip_entry_name(usados, "Certificado_PEREZ.docx"),
            unique_zip_entry_name(usados, "Certificado_PEREZ_1.docx"),
        ]
        self.assertEqual(
            names,
            [
                "Certificado_PEREZ.docx",
                "Certificado_PEREZ_1.docx",
                "Certificado_PEREZ_1_1.docx",
            ],
        )
        self.assertEqual(len(set(names)), 3)

    def test_servidor_zip_sin_entradas_duplicadas(self):
        import certificados_deuda_flujo_service as flujo

        usados: set[str] = set()
        generados = [
            ("Certificado_X_2-1.docx", b"a"),
            ("Certificado_X_2-1.docx", b"b"),
            ("Certificado_X_2-1_1.docx", b"c"),
        ]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for nombre, raw in generados:
                zf.writestr(flujo.nombre_unico_entrada_zip(usados, nombre), raw)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)
        self.assertIn("Certificado_X_2-1_1_1.docx", names)


class TemplateAntiColisionTests(unittest.TestCase):
    def test_ui_aplana_zips_y_usa_nombres_unicos(self):
        html = (ROOT / "templates" / "estado_cuenta_pdf.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("empaquetarCertificadosPlanos", html)
        self.assertIn("uniqueZipEntryName", html)
        self.assertIn("JSZip.loadAsync", html)
        self.assertIn("ZIP plano", html)
        self.assertIn("new Set()", html)
        self.assertIn("usados.has", html)
        # No debe volver al patrón que pisaba: zip.file(filename) con el
        # Content-Disposition crudo de varios grupos.
        self.assertNotIn(
            "zip.file(filename, await res.arrayBuffer())",
            html,
        )
        self.assertIn("btn-certs-label", html)
        self.assertIn("un solo ZIP plano", html)

    def test_ui_guarda_descarga_doble_clic(self):
        """Regresión: un clic → un a.click(); candado antes de antefirma/fetch."""
        html = (ROOT / "templates" / "estado_cuenta_pdf.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("certDescargaLoteEnCurso", html)
        self.assertIn("certDescargaUnoEnCurso", html)
        self.assertIn("triggerBlobDownload", html)
        # Candado sincrónico antes del await de antefirma (doble clic).
        self.assertIn(
            "Candado sincrónico ANTES de antefirma/await",
            html,
        )
        self.assertIn("vistos[ji].has(map.localIndex)", html)
        # Un solo addEventListener de descarga de lote (no onclick + listener).
        self.assertEqual(
            html.count("btnDescargarCerts.addEventListener('click'"),
            1,
        )
        self.assertEqual(
            html.count("btnProcesarCert.addEventListener('click'"),
            1,
        )
        # Generar preview no auto-descarga (false), evita preview+download doble.
        self.assertIn(
            "runProcesarCertificado(false)",
            html,
        )

    def test_ui_muestra_conteo_y_headers_parciales(self):
        html = (ROOT / "templates" / "estado_cuenta_pdf.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("cert-resumen-conteo", html)
        self.assertIn("no_emitibles_detalle", html)
        self.assertIn("X-Certificados-Fallidos", html)
        self.assertIn("X-Certificados-Generados", html)
        self.assertIn("X-Certificados-Pedidos", html)
        self.assertIn("c-no-emit", html)


class BatchSizeHipótesisTests(unittest.TestCase):
    def test_31_con_batch_15_da_grupos_15_15_1(self):
        # chunk(arr, 15) como en la UI: slice(i, i+size)
        batch = 15
        n = 31
        sizes = [min(batch, n - i) for i in range(0, n, batch)]
        self.assertEqual(sizes, [15, 15, 1])
        # Con colisión: ZIP×ZIP se pisan; el docx del último grupo sobrevive.
        # Usuario ve: 15 (último ZIP) + 1 (docx) = 16.
        self.assertEqual(sizes[1] + sizes[2], 16)

    def test_colision_tres_grupos_explica_16_de_31(self):
        """15+15+1 con overwrite de Certificados_deuda.zip ⇒ 16 Word visibles."""
        g1 = _zip_de_nombres([f"A_{i}.docx" for i in range(15)])
        g2 = _zip_de_nombres([f"B_{i}.docx" for i in range(15)])
        docx_suelto = b"PK-fake-docx-single"

        # Bug viejo (estilo JSZip.file por filename crudo):
        entries: dict[str, bytes] = {}
        entries["Certificados_deuda.zip"] = g1
        entries["Certificados_deuda.zip"] = g2  # pisa g1
        entries["Certificado_ultimo.docx"] = docx_suelto
        self.assertEqual(len(entries), 2)
        with zipfile.ZipFile(io.BytesIO(entries["Certificados_deuda.zip"])) as zf:
            self.assertEqual(len(zf.namelist()), 15)

        # Fix: aplanar conserva 31
        planos, total = empaquetar_certificados_planos(
            [
                ("Certificados_deuda.zip", g1),
                ("Certificados_deuda.zip", g2),
                ("Certificado_ultimo.docx", docx_suelto),
            ]
        )
        self.assertEqual(total, 31)
        self.assertEqual(len(planos), 31)

    def test_template_default_batch_size_15(self):
        html = (ROOT / "templates" / "estado_cuenta_pdf.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("ui_batch_size|default(15)", html)
        svc = (ROOT / "estado_cuenta_pdf_service.py").read_text(encoding="utf-8")
        self.assertIn('ESTADO_CUENTA_UI_BATCH_SIZE", "15"', svc)

    def test_61_con_batch_15_da_cinco_grupos(self):
        batch = 15
        n = 61
        sizes = [min(batch, n - i) for i in range(0, n, batch)]
        self.assertEqual(sizes, [15, 15, 15, 15, 1])
        self.assertEqual(sum(sizes), 61)


if __name__ == "__main__":
    unittest.main()
