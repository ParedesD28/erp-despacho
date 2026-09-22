"""Almacenamiento privado de PDFs sensibles (fuera de StaticFiles)."""
from __future__ import annotations

import os
from pathlib import Path

PDF_STORAGE_DIR = Path(os.getenv("PDF_STORAGE_DIR", "private_pdfs"))


def ensure_pdf_dir() -> Path:
    PDF_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return PDF_STORAGE_DIR


def resolve_pdf(filename: str) -> Path:
    """Resuelve un nombre de archivo seguro dentro del directorio privado."""
    if not filename or Path(filename).name != filename:
        raise ValueError("nombre de archivo inválido")
    return ensure_pdf_dir() / filename
