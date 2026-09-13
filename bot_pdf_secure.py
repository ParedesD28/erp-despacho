"""Descarga pública temporal y firmada de PDFs generados para el bot."""

import hashlib
import hmac
import os
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

import main

_TOKEN_TTL_SECONDS = int(os.getenv("BOT_PDF_URL_TTL", "900"))
_SECRET = (os.getenv("BOT_PDF_PUBLIC_SECRET") or os.getenv("LIQUIDADOR_API_KEY") or "").strip().strip('"').strip("'")


def _sign(filename: str, expires: int) -> str:
    payload = f"{filename}|{expires}".encode("utf-8")
    return hmac.new(_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_signed_pdf_url(filename: str, base_url: str) -> str:
    if not _SECRET:
        raise RuntimeError("BOT_PDF_PUBLIC_SECRET/LIQUIDADOR_API_KEY no esta configurada")
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    token = _sign(filename, expires)
    return f"{base_url.rstrip('/')}/api/bot/pdf/{filename}?expires={expires}&token={token}"


def _serve_bot_pdf(filename: str, expires: int, token: str):
    if not _SECRET:
        raise HTTPException(status_code=503, detail="Servicio PDF no configurado")
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Enlace invalido")
    if expires_int < int(time.time()):
        raise HTTPException(status_code=410, detail="Enlace expirado")
    expected = _sign(filename, expires_int)
    if not token or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Enlace no autorizado")

    path = Path("static") / "pdfs" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=filename,
        headers={"Cache-Control": "private, max-age=900"},
    )


main.app.add_api_route(
    "/api/bot/pdf/{filename}",
    _serve_bot_pdf,
    methods=["GET"],
    include_in_schema=False,
)
print("[BOT PDF] Descarga publica firmada activada", flush=True)
