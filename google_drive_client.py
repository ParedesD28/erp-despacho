"""Cliente Google Drive para PDFs de actuaciones.

Auth preferida: OAuth (Desktop/Web) con refresh token — evita claves de cuenta
de servicio bloqueadas por políticas org (`iam.disableServiceAccountKeyCreation`).

Fallback legacy: `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` si aún está disponible.

Env:
  GOOGLE_DRIVE_FOLDER_ID              — carpeta raíz compartida / propia
  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET
  GOOGLE_OAUTH_REFRESH_TOKEN
  GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON   — opcional (legacy)
  GOOGLE_DRIVE_ENABLED                — opcional; auto si hay creds+folder
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_SCOPES = [DRIVE_SCOPE, "https://www.googleapis.com/auth/drive.file"]
MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_PDF = "application/pdf"
TOKEN_URI = "https://oauth2.googleapis.com/token"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-]+")


def _truthy(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _falsey(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in {"0", "false", "no", "off"}


def _folder_id() -> str:
    return (os.environ.get("GOOGLE_DRIVE_FOLDER_ID") or "").strip()


def _service_account_json_raw() -> str:
    return (os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON") or "").strip()


def _oauth_client_id() -> str:
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()


def _oauth_client_secret() -> str:
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()


def _oauth_refresh_token() -> str:
    return (os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN") or "").strip()


def oauth_ready() -> bool:
    return bool(
        _oauth_client_id() and _oauth_client_secret() and _oauth_refresh_token()
    )


def service_account_ready() -> bool:
    return bool(_service_account_json_raw())


def drive_creds_present() -> bool:
    return oauth_ready() or service_account_ready()


def drive_enabled() -> bool:
    """Drive activo si hay folder + creds, salvo GOOGLE_DRIVE_ENABLED=false."""
    flag = (os.environ.get("GOOGLE_DRIVE_ENABLED") or "").strip()
    if _falsey(flag):
        return False
    if not _folder_id():
        return False
    if not drive_creds_present():
        return False
    # Auto-enable when env present; explicit true also OK.
    return True


def auth_mode() -> Optional[str]:
    """'oauth' | 'service_account' | None — preferencia OAuth."""
    if not drive_enabled():
        return None
    if oauth_ready():
        return "oauth"
    if service_account_ready():
        return "service_account"
    return None


def safe_filename(name: str, *, default: str = "documento.pdf") -> str:
    """Sanitiza nombre de archivo para Drive (sin path traversal)."""
    base = (name or "").strip() or default
    base = base.replace("\\", "_").replace("/", "_")
    base = _SAFE_NAME_RE.sub("_", base).strip("._") or "documento"
    if not base.lower().endswith(".pdf"):
        base = f"{base}.pdf"
    if len(base) > 180:
        stem, _, ext = base.rpartition(".")
        base = f"{stem[:170]}.{ext}"
    return base


def build_actuacion_filename(
    *,
    fecha: Any = None,
    tipo: Any = None,
    id_actuacion: Any = None,
    nombre_doc: Any = None,
) -> str:
    """Nombre tipo `{fecha}_{tipo}_{id}.pdf` o con nombre del documento."""
    if nombre_doc and str(nombre_doc).strip():
        return safe_filename(str(nombre_doc).strip())
    parts = [
        str(fecha or "sin-fecha").strip()[:10],
        str(tipo or "actuacion").strip()[:40],
        str(id_actuacion or "doc").strip()[:32],
    ]
    return safe_filename("_".join(p for p in parts if p))


class GoogleDriveClient:
    """Sube PDFs bajo la carpeta raíz (`GOOGLE_DRIVE_FOLDER_ID`)."""

    def __init__(
        self,
        *,
        folder_id: Optional[str] = None,
        service: Any = None,
        service_account_json: Optional[str] = None,
    ) -> None:
        self._folder_id = (folder_id or _folder_id()).strip()
        if not self._folder_id and service is None:
            raise ValueError("GOOGLE_DRIVE_FOLDER_ID no configurada")

        self._service = service
        self._folder_cache: dict[tuple[str, str], str] = {}
        self._auth_mode = "injected" if service is not None else None

        if self._service is None:
            mode = auth_mode()
            if mode == "oauth":
                self._service = self._build_service_oauth()
                self._auth_mode = "oauth"
            elif mode == "service_account" or service_account_json:
                raw = (service_account_json or _service_account_json_raw()).strip()
                if not raw:
                    raise ValueError(
                        "Sin OAuth ni GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON"
                    )
                self._service = self._build_service_sa(raw)
                self._auth_mode = "service_account"
            else:
                raise ValueError(
                    "Configura OAuth (GOOGLE_OAUTH_*) o service account JSON"
                )

    @property
    def mode(self) -> Optional[str]:
        return self._auth_mode

    @staticmethod
    def _build_service_oauth() -> Any:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Faltan dependencias google-auth / google-api-python-client"
            ) from exc

        creds = Credentials(
            token=None,
            refresh_token=_oauth_refresh_token(),
            token_uri=TOKEN_URI,
            client_id=_oauth_client_id(),
            client_secret=_oauth_client_secret(),
            scopes=DRIVE_SCOPES,
        )
        creds.refresh(Request())
        logger.info("Drive: autenticado vía OAuth refresh token")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _build_service_sa(raw_json: str) -> Any:
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Faltan dependencias google-auth / google-api-python-client"
            ) from exc

        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON no es JSON válido"
            ) from exc
        if not isinstance(info, dict):
            raise ValueError(
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON debe ser un objeto JSON"
            )

        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=[DRIVE_SCOPE],
        )
        logger.info("Drive: autenticado vía service account (legacy)")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def ensure_subfolder(self, parent_id: str, name: str) -> str:
        """Crea (si no existe) una carpeta hija y retorna su id."""
        name = (name or "").strip() or "sin-nombre"
        cache_key = (parent_id, name)
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        safe_q_name = name.replace("'", "\\'")
        query = (
            f"name = '{safe_q_name}' and '{parent_id}' in parents "
            f"and mimeType = '{MIME_FOLDER}' and trashed = false"
        )
        listed = (
            self._service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=5,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = listed.get("files") or []
        if files:
            folder_id = str(files[0]["id"])
            self._folder_cache[cache_key] = folder_id
            return folder_id

        meta = {
            "name": name,
            "mimeType": MIME_FOLDER,
            "parents": [parent_id],
        }
        created = (
            self._service.files()
            .create(
                body=meta,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        folder_id = str(created["id"])
        self._folder_cache[cache_key] = folder_id
        logger.info("Drive: carpeta creada name=%s id=%s", name, folder_id)
        return folder_id

    def ensure_path(self, *parts: str) -> str:
        """Asegura carpetas anidadas bajo la raíz; retorna id hoja."""
        parent = self._folder_id
        for part in parts:
            clean = (part or "").strip()
            if not clean:
                continue
            parent = self.ensure_subfolder(parent, clean)
        return parent

    def upload_pdf(
        self,
        content: bytes,
        *,
        radicado: str,
        filename: str,
        folder_segments: Optional[tuple[str, ...]] = None,
    ) -> dict[str, str]:
        """Sube PDF bajo `{radicado}/actuaciones/{filename}`.

        Retorna `{"file_id", "webViewLink", "name", "backend"}`.
        """
        if not content:
            raise ValueError("contenido PDF vacío")

        from googleapiclient.http import MediaIoBaseUpload

        rad = (radicado or "").strip() or "sin-radicado"
        segments = folder_segments or (rad, "actuaciones")
        parent_id = self.ensure_path(*segments)
        fname = safe_filename(filename)

        media = MediaIoBaseUpload(
            io.BytesIO(content),
            mimetype=MIME_PDF,
            resumable=False,
        )
        body = {
            "name": fname,
            "parents": [parent_id],
        }
        created = (
            self._service.files()
            .create(
                body=body,
                media_body=media,
                fields="id, name, webViewLink, webContentLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = str(created.get("id") or "")
        link = str(created.get("webViewLink") or "").strip()
        if not link and file_id:
            link = f"https://drive.google.com/file/d/{file_id}/view"
        logger.info(
            "Drive: PDF subido radicado=%s name=%s file_id=%s mode=%s",
            rad,
            fname,
            file_id,
            self._auth_mode,
        )
        return {
            "file_id": file_id,
            "webViewLink": link,
            "name": str(created.get("name") or fname),
            "backend": "google_drive",
        }
