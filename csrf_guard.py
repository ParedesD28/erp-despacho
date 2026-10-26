"""Defensa CSRF para peticiones que cambian estado (POST/PUT/PATCH/DELETE).

Verifica el origen de la petición en lugar de añadir un token a cada formulario:
  1. Si hay `Origin`, su host debe coincidir con el host del ERP.
  2. Si no hay `Origin` pero hay `Referer`, aplica la misma regla.
  3. Si el navegador envía `Sec-Fetch-Site`, solo se aceptan `same-origin` y `none`.
  4. Sin ninguna de esas cabeceras (clientes no navegador: curl, bots) se permite;
     un ataque CSRF siempre proviene de un navegador, que sí las envía.

Complementa a la cookie SameSite=Lax. Hosts permitidos: el `Host` de la petición,
el de PUBLIC_BASE_URL/RENDER_EXTERNAL_URL y los de ERP_ALLOWED_ORIGINS (CSV).
"""
from __future__ import annotations

import os
from typing import Iterable, Optional
from urllib.parse import urlparse


def _host_de_url(valor: str) -> str:
    valor = (valor or "").strip()
    if not valor:
        return ""
    if "://" not in valor:
        valor = "//" + valor
    return (urlparse(valor).netloc or "").lower()


def hosts_permitidos(host_header: Optional[str]) -> set[str]:
    hosts = {(host_header or "").strip().lower()}
    for var in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
        hosts.add(_host_de_url(os.getenv(var, "")))
    for item in (os.getenv("ERP_ALLOWED_ORIGINS") or "").split(","):
        hosts.add(_host_de_url(item))
    hosts.discard("")
    return hosts


def origen_permitido(headers, extra_hosts: Optional[Iterable[str]] = None) -> bool:
    permitidos = hosts_permitidos(headers.get("host"))
    if extra_hosts:
        permitidos |= {h.lower() for h in extra_hosts}

    origin = headers.get("origin")
    if origin is not None:
        return _host_de_url(origin) in permitidos if origin.strip().lower() != "null" else False

    referer = headers.get("referer")
    if referer:
        return _host_de_url(referer) in permitidos

    sitio = (headers.get("sec-fetch-site") or "").strip().lower()
    if sitio and sitio not in ("same-origin", "none"):
        return False
    return True
