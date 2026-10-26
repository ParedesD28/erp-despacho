"""Tests sync: descarga docs + upload Drive (mocks; sin red real)."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SyncDocumentosDriveTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["SYNC_RAMA_SLEEP_S"] = "0"

    def tearDown(self) -> None:
        os.environ.pop("SYNC_RAMA_SLEEP_S", None)

    def test_truthy_con_documentos(self) -> None:
        from sync_procesos_service import _truthy_con_documentos

        self.assertTrue(_truthy_con_documentos(True))
        self.assertTrue(_truthy_con_documentos("true"))
        self.assertTrue(_truthy_con_documentos(1))
        self.assertFalse(_truthy_con_documentos(False))
        self.assertFalse(_truthy_con_documentos(None))
        self.assertFalse(_truthy_con_documentos("no"))

    def test_sincronizar_un_expediente_sube_pdf_si_con_documentos(self) -> None:
        from sync_procesos_service import sincronizar_un_expediente

        client = AsyncMock()
        client.consultar_id_por_radicado = AsyncMock(return_value=99)
        client.obtener_actuaciones = AsyncMock(
            return_value=[
                SimpleNamespace(
                    model_dump=lambda: {
                        "idRegActuacion": "555",
                        "fechaActuacion": "2026-10-01",
                        "actuacion": "Auto",
                        "anotacion": "Con PDF",
                        "conDocumentos": True,
                    }
                )
            ]
        )
        doc_meta = SimpleNamespace(id_reg_documento="777", nombre="auto.pdf")
        client.descargar_pdfs_actuacion = AsyncMock(
            return_value=[(doc_meta, b"%PDF-fake")]
        )

        drive = MagicMock()
        drive.upload_pdf.return_value = {
            "file_id": "fid-1",
            "webViewLink": "https://drive.google.com/file/d/fid-1/view",
            "name": "auto.pdf",
        }

        cur = MagicMock()
        # _actuacion_ya_existe → False (fetchone None); _cols returns drive cols;
        # insert returns id; update ok; alerta insert ok
        cur.fetchone.side_effect = [
            None,  # ya_existe id_actuacion
            None,  # ya_existe content_hash
            {"column_name": "drive_url"},  # wait — _cols uses fetchall
        ]
        # Re-implement with careful side effects using patch helpers.

        def cols_side_effect(table):
            if table == "actuaciones_proceso":
                return {
                    "drive_url",
                    "drive_file_id",
                    "radicado_interno",
                    "content_hash",
                }
            if table == "procesos":
                return {"requiere_revision"}
            return set()

        with patch(
            "sync_procesos_service._actuacion_ya_existe",
            return_value=False,
        ), patch(
            "sync_procesos_service._insertar_actuacion",
            return_value=42,
        ) as mock_ins, patch(
            "sync_procesos_service._actualizar_drive_actuacion",
        ) as mock_upd, patch(
            "sync_procesos_service._crear_alerta",
        ) as mock_alerta, patch(
            "sync_procesos_service._marcar_requiere_revision",
        ), patch(
            "sync_procesos_service._cols",
            side_effect=cols_side_effect,
        ):
            metrics = asyncio.run(
                sincronizar_un_expediente(
                    client,
                    cur,
                    {
                        "radicado_interno": "INT-1",
                        "radicado_rama": "66170405300120190103500",
                        "abogado_id": 7,
                    },
                    drive=drive,
                )
            )

        self.assertEqual(metrics["nuevas"], 1)
        self.assertEqual(metrics["docs_ok"], 1)
        self.assertEqual(metrics["alertas"], 1)
        client.descargar_pdfs_actuacion.assert_awaited()
        drive.upload_pdf.assert_called_once()
        mock_upd.assert_called_once()
        args, kwargs = mock_upd.call_args
        # _actualizar_drive_actuacion(cur, actuacion_id, drive_url=…, drive_file_id=…)
        self.assertEqual(args[1], 42)
        self.assertIn("fid-1", kwargs.get("drive_url") or "")
        mock_alerta.assert_called_once()
        mock_ins.assert_called_once()

    def test_sin_drive_no_descarga_docs(self) -> None:
        from sync_procesos_service import sincronizar_un_expediente

        client = AsyncMock()
        client.consultar_id_por_radicado = AsyncMock(return_value=1)
        client.obtener_actuaciones = AsyncMock(
            return_value=[
                {
                    "idRegActuacion": "1",
                    "fechaActuacion": "2026-01-01",
                    "actuacion": "X",
                    "anotacion": "Y",
                    "conDocumentos": True,
                }
            ]
        )
        client.descargar_pdfs_actuacion = AsyncMock()

        with patch("sync_procesos_service._actuacion_ya_existe", return_value=False), patch(
            "sync_procesos_service._insertar_actuacion", return_value=1
        ), patch("sync_procesos_service._crear_alerta"), patch(
            "sync_procesos_service._marcar_requiere_revision"
        ), patch("sync_procesos_service._cols", return_value=set()):
            metrics = asyncio.run(
                sincronizar_un_expediente(
                    client,
                    MagicMock(),
                    {
                        "radicado_interno": "A",
                        "radicado_rama": "66170405300120190103500",
                    },
                    drive=None,
                )
            )

        self.assertEqual(metrics["nuevas"], 1)
        client.descargar_pdfs_actuacion.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
