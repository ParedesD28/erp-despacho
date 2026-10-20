"""Almacenamiento de PDFs de actuaciones: Google Drive (preferido) o disco ERP.

Prioridad:
  1. Google Drive si hay folder + (OAuth o SA)
  2. Disco local ERP si `EXPEDIENTE_DOCS_ENABLED` o `EXPEDIENTE_DOCS_DIR`
  3. None → sync no sube PDFs

Estructura (ambos backends):
  {radicado}/actuaciones/{safe_filename}.pdf
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional, Protocol

from google_drive_client import (
    GoogleDriveClient,
    build_actuacion_filename,
    drive_enabled,
    safe_filename,
)

logger = logging.getLogger(__name__)

_RADICADO_SAFE_RE = re.compile(r"[^A-Za-z0-9._\-]+")


def _falsey(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in {"0", "false", "no", "off"}


def _truthy(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def local_docs_root() -> Path:
    raw = (os.environ.get("EXPEDIENTE_DOCS_DIR") or "").strip()
    if raw:
        return Path(raw)
    # Bajo private_pdfs para quedar fuera de StaticFiles.
    from pdf_storage import ensure_pdf_dir

    return ensure_pdf_dir() / "expedientes"


def local_docs_enabled() -> bool:
    flag = (os.environ.get("EXPEDIENTE_DOCS_ENABLED") or "").strip()
    if _falsey(flag):
        return False
    if _truthy(flag):
        return True
    # Auto si hay directorio explícito.
    return bool((os.environ.get("EXPEDIENTE_DOCS_DIR") or "").strip())


def safe_radicado_segment(radicado: str) -> str:
    text = (radicado or "").strip() or "sin-radicado"
    text = text.replace("\\", "_").replace("/", "_")
    return _RADICADO_SAFE_RE.sub("_", text).strip("._") or "sin-radicado"


class DocumentStorage(Protocol):
    def upload_pdf(
        self,
        content: bytes,
        *,
        radicado: str,
        filename: str,
        folder_segments: Optional[tuple[str, ...]] = None,
    ) -> dict[str, str]:
        ...


class LocalExpedienteDocsStorage:
    """Guarda PDFs en disco del ERP (auth vía ruta /expediente-docs/…)."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else local_docs_root()

    @property
    def mode(self) -> str:
        return "local"

    def upload_pdf(
        self,
        content: bytes,
        *,
        radicado: str,
        filename: str,
        folder_segments: Optional[tuple[str, ...]] = None,
    ) -> dict[str, str]:
        if not content:
            raise ValueError("contenido PDF vacío")

        rad = safe_radicado_segment(radicado)
        segments = folder_segments or (rad, "actuaciones")
        clean_parts = [safe_radicado_segment(p) for p in segments if str(p).strip()]
        fname = safe_filename(filename)
        dest_dir = self.root.joinpath(*clean_parts)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / fname
        dest.write_bytes(content)

        # Ruta relativa servida por main.servir_expediente_doc
        rel = "/".join([*clean_parts, fname])
        view = f"/expediente-docs/{rel}"
        logger.info("Local docs: guardado %s (%s bytes)", dest, len(content))
        return {
            "file_id": rel,
            "webViewLink": view,
            "name": fname,
            "backend": "local",
        }


def get_document_storage() -> Optional[DocumentStorage]:
    """Factory: Drive si hay creds; si no, local ERP si está habilitado."""
    if drive_enabled():
        try:
            client = GoogleDriveClient()
            logger.info("Document storage: Google Drive (%s)", client.mode)
            return client
        except Exception as exc:
            logger.warning(
                "Drive configurado pero no inicializable (%s: %s); "
                "probando almacenamiento local",
                type(exc).__name__,
                exc,
            )
    if local_docs_enabled():
        storage = LocalExpedienteDocsStorage()
        logger.info("Document storage: local ERP root=%s", storage.root)
        return storage
    return None


def storage_backend_name() -> str:
    if drive_enabled():
        from google_drive_client import auth_mode

        return f"google_drive:{auth_mode() or '?'}"
    if local_docs_enabled():
        return "local"
    return "none"


# Re-export helpers usados por sync.
__all__ = [
    "DocumentStorage",
    "LocalExpedienteDocsStorage",
    "build_actuacion_filename",
    "get_document_storage",
    "local_docs_enabled",
    "local_docs_root",
    "safe_filename",
    "storage_backend_name",
]
