"""Tests Google Drive / OAuth / enablement — mocks only."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _clear_drive_env() -> None:
    for key in (
        "GOOGLE_DRIVE_ENABLED",
        "GOOGLE_DRIVE_FOLDER_ID",
        "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "EXPEDIENTE_DOCS_ENABLED",
        "EXPEDIENTE_DOCS_DIR",
    ):
        os.environ.pop(key, None)


class DriveEnabledTests(unittest.TestCase):
    def tearDown(self) -> None:
        _clear_drive_env()

    def test_disabled_without_creds(self) -> None:
        from google_drive_client import drive_enabled

        _clear_drive_env()
        self.assertFalse(drive_enabled())

    def test_auto_enable_with_oauth(self) -> None:
        from google_drive_client import auth_mode, drive_enabled, oauth_ready

        os.environ["GOOGLE_DRIVE_FOLDER_ID"] = "folder-abc"
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid.apps.googleusercontent.com"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "csecret"
        os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] = "rtoken"
        self.assertTrue(oauth_ready())
        self.assertTrue(drive_enabled())
        self.assertEqual(auth_mode(), "oauth")

    def test_explicit_false_disables(self) -> None:
        from google_drive_client import drive_enabled

        os.environ["GOOGLE_DRIVE_ENABLED"] = "false"
        os.environ["GOOGLE_DRIVE_FOLDER_ID"] = "folder-abc"
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "sec"
        os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] = "rt"
        self.assertFalse(drive_enabled())

    def test_sa_fallback_when_no_oauth(self) -> None:
        from google_drive_client import auth_mode, drive_enabled

        os.environ["GOOGLE_DRIVE_FOLDER_ID"] = "folder-abc"
        os.environ["GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON"] = '{"type":"service_account"}'
        self.assertTrue(drive_enabled())
        self.assertEqual(auth_mode(), "service_account")

    def test_oauth_preferred_over_sa(self) -> None:
        from google_drive_client import auth_mode

        os.environ["GOOGLE_DRIVE_FOLDER_ID"] = "folder-abc"
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "sec"
        os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] = "rt"
        os.environ["GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON"] = '{"type":"service_account"}'
        self.assertEqual(auth_mode(), "oauth")


class SafeFilenameTests(unittest.TestCase):
    def test_sanitiza_y_agrega_pdf(self) -> None:
        from google_drive_client import build_actuacion_filename, safe_filename

        self.assertEqual(safe_filename("../../evil name!.bin"), "evil_name_.bin.pdf")
        self.assertTrue(safe_filename("auto.pdf").endswith(".pdf"))
        name = build_actuacion_filename(
            fecha="2026-10-01",
            tipo="Auto admisorio",
            id_actuacion="12345",
        )
        self.assertIn("2026-10-01", name)
        self.assertTrue(name.endswith(".pdf"))


class UploadPdfTests(unittest.TestCase):
    def test_upload_crea_carpetas_y_retorna_link(self) -> None:
        from google_drive_client import GoogleDriveClient

        service = MagicMock()
        files_api = service.files.return_value
        list_exec = MagicMock()
        list_exec.execute.return_value = {"files": []}
        create_folder = MagicMock()
        create_folder.execute.side_effect = [
            {"id": "folder-rad"},
            {"id": "folder-act"},
            {
                "id": "file-1",
                "name": "doc.pdf",
                "webViewLink": "https://drive.google.com/file/d/file-1/view",
            },
        ]
        files_api.list.return_value = list_exec
        files_api.create.return_value = create_folder

        client = GoogleDriveClient(folder_id="root-folder", service=service)
        with patch("googleapiclient.http.MediaIoBaseUpload", MagicMock()):
            result = client.upload_pdf(
                b"%PDF-1.4 mock",
                radicado="66170405300120190103500",
                filename="auto.pdf",
            )

        self.assertEqual(result["file_id"], "file-1")
        self.assertIn("file-1", result["webViewLink"])
        self.assertEqual(result["backend"], "google_drive")
        self.assertEqual(files_api.create.call_count, 3)


class LocalStorageTests(unittest.TestCase):
    def tearDown(self) -> None:
        _clear_drive_env()

    def test_local_upload_and_factory_fallback(self) -> None:
        import tempfile

        from expediente_docs_storage import (
            LocalExpedienteDocsStorage,
            get_document_storage,
            local_docs_enabled,
        )

        _clear_drive_env()
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EXPEDIENTE_DOCS_DIR"] = tmp
            self.assertTrue(local_docs_enabled())
            storage = get_document_storage()
            self.assertIsInstance(storage, LocalExpedienteDocsStorage)
            result = storage.upload_pdf(
                b"%PDF-local",
                radicado="66170405300120190103500",
                filename="auto.pdf",
            )
            self.assertEqual(result["backend"], "local")
            self.assertTrue(result["webViewLink"].startswith("/expediente-docs/"))
            rel = Path(tmp) / "66170405300120190103500" / "actuaciones" / "auto.pdf"
            self.assertTrue(rel.is_file())
            self.assertEqual(rel.read_bytes(), b"%PDF-local")


if __name__ == "__main__":
    unittest.main()
