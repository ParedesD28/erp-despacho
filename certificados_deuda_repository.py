"""Capa de acceso a Neon para Certificados de Deuda.

Driver real: psycopg2 + ThreadedConnectionPool (`db.get_connection()`).
No hay SQLAlchemy ni asyncpg: las consultas son síncronas y deben
envolverse con `asyncio.to_thread()` desde endpoints FastAPI.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from psycopg2.extras import RealDictCursor

import db

# Ruido típico en etiquetas de unidad (PDF COLON / maestros).
_UNIT_NOISE_RE = re.compile(
    r"\b("
    r"TORRE|BLOQUE|BL|MZ|MANZANA|APTO|APARTAMENTO|APT|AP|"
    r"NRO|NUMERO|NÚMERO|NO|N0|N"
    r")\b",
    re.IGNORECASE,
)
_CONJUNTO_NOISE_RE = re.compile(
    r"\b("
    r"PROPIEDAD\s+HORIZONTAL|P\.?\s*H\.?|PH|"
    r"URBANIZACI[OÓ]N|URB\.?|CONJUNTO\s+RESIDENCIAL|"
    r"CONJUNTO|EDIFICIO|RESIDENCIAL|UNIDAD"
    r")\b",
    re.IGNORECASE,
)
_TOKEN_UNIDAD_RE = re.compile(r"[A-Z]*\d+[A-Z]*")


def _sin_acentos(texto: str) -> str:
    base = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in base if not unicodedata.combining(c))


def _norm_texto(texto: str) -> str:
    """Mayúsculas, sin acentos, espacios colapsados."""
    return re.sub(r"\s+", " ", _sin_acentos(texto).upper()).strip()


def _norm_key(texto: str) -> str:
    """Normaliza para comparar torre/apto (alfanumérico mayúsculas, sin acentos)."""
    return re.sub(r"[^A-Z0-9]+", "", _norm_texto(texto))


def _tokens_unidad(texto: str) -> list[str]:
    """Extrae tokens alfanuméricos con dígitos tras quitar etiquetas TORRE/APTO/etc."""
    limpio = _norm_texto(texto)
    # "N0.502" / "No. 502" → espacio + 502
    limpio = re.sub(r"\bN[O0]\.?\s*", " ", limpio)
    limpio = _UNIT_NOISE_RE.sub(" ", limpio)
    limpio = re.sub(r"[^A-Z0-9]+", " ", limpio)
    return _TOKEN_UNIDAD_RE.findall(limpio)


def _clave_unidad(texto: str = "", *, bloque: str = "", apartamento: str = "") -> str:
    """
    Clave canónica bloque-apto comparable entre formatos.

    Ejemplos → misma clave `1-201`:
      - "TORRE 1 APTO 201"
      - "1-201"
      - bloque=1, apartamento=201
      - "T1-201" (también genera alias numérico vía `_claves_unidad`)
    """
    tokens = _tokens_unidad(texto)
    if bloque and apartamento:
        tokens = _tokens_unidad(f"{bloque} {apartamento}") or [
            _norm_key(bloque),
            _norm_key(apartamento),
        ]
    elif bloque and not tokens:
        tokens = [_norm_key(bloque)]
    elif apartamento and not tokens:
        tokens = [_norm_key(apartamento)]
    tokens = [t for t in tokens if t]
    return "-".join(tokens) if tokens else ""


def _claves_unidad(
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
) -> set[str]:
    """Conjunto de claves canónicas (y alias) para una unidad."""
    claves: set[str] = set()

    def _agregar(raw: str, *, b: str = "", a: str = "") -> None:
        clave = _clave_unidad(raw, bloque=b, apartamento=a)
        if not clave:
            return
        claves.add(clave)
        # Alias: quitar letras líderes del primer token (T1-201 → 1-201)
        partes = clave.split("-")
        if partes:
            first = re.sub(r"^[A-Z]+", "", partes[0]) or partes[0]
            alt = "-".join([first] + partes[1:])
            if alt:
                claves.add(alt)
        # Alias compacto solo dígitos (evitar choques cortos se filtra en match)
        solo_digitos = re.sub(r"\D+", "", clave)
        if len(solo_digitos) >= 3:
            claves.add(solo_digitos)

    _agregar(torre_apto)
    if bloque and apartamento:
        _agregar("", b=str(bloque), a=str(apartamento))
        _agregar(f"{bloque}-{apartamento}")
        _agregar(f"TORRE {bloque} APTO {apartamento}")
        _agregar(f"BLOQUE {bloque} APTO {apartamento}")
    elif apartamento:
        _agregar(str(apartamento))
    elif bloque:
        _agregar(str(bloque))
    return {c for c in claves if c}


def _variantes_torre_apto(
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
) -> list[str]:
    """Candidatos de unidad a partir de torre_apto o bloque+apartamento del PDF."""
    out: list[str] = []
    for raw in (
        torre_apto,
        f"{bloque}-{apartamento}" if bloque and apartamento else "",
        f"{bloque} {apartamento}" if bloque and apartamento else "",
        f"TORRE {bloque} APTO {apartamento}" if bloque and apartamento else "",
        f"BLOQUE {bloque} APTO {apartamento}" if bloque and apartamento else "",
        apartamento or "",
        bloque or "",
    ):
        limpio = " ".join(str(raw or "").strip().split())
        if limpio and limpio not in out:
            out.append(limpio)
    return out


def _nucleo_conjunto(nombre: str) -> str:
    """Quita prefijos/sufijos legales (PH, URBANIZACIÓN, …) para comparar."""
    texto = _norm_texto(nombre)
    texto = _CONJUNTO_NOISE_RE.sub(" ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _score_conjunto(candidato: str, buscado: str) -> int:
    """
    Score de similitud de nombres de conjunto (mayor = mejor).
    100 exacto normalizado; 90 núcleo exacto; 70 contención; 0 no match.
    """
    a = _norm_texto(candidato)
    b = _norm_texto(buscado)
    if not a or not b:
        return 0
    if a == b:
        return 100
    na, nb = _nucleo_conjunto(candidato), _nucleo_conjunto(buscado)
    if na and nb and na == nb:
        return 90
    if na and nb and (na in nb or nb in na) and min(len(na), len(nb)) >= 4:
        return 70
    if a in b or b in a:
        return 50
    return 0


def _titular_desde_fila(row: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    nombre = (row.get("titular_nombre") or "").strip() or None
    cedula = (row.get("titular_cedula") or "").strip() or None
    if not nombre:
        nombre = (row.get("contacto_nombre") or "").strip() or None
    if not cedula:
        cedula = (row.get("contacto_cedula") or "").strip() or None
    return nombre, cedula


def _sql_base() -> str:
    return """
        SELECT
            i.id AS inmueble_id,
            i.torre_apto,
            i.conjunto_id,
            COALESCE(NULLIF(BTRIM(i.conjunto_residencial), ''), cr.nombre) AS conjunto_nombre,
            cr.nombre AS conjunto_catalogo,
            cr.contacto_id AS copropiedad_contacto_id,
            pj.nombre AS copropiedad_nombre,
            pj.identificacion AS copropiedad_nit,
            COALESCE(NULLIF(BTRIM(pj.ciudad), ''), NULLIF(BTRIM(c_legacy.ciudad), '')) AS ciudad,
            tit.nombre AS titular_nombre,
            tit.identificacion AS titular_cedula,
            c_legacy.nombre AS contacto_nombre,
            c_legacy.identificacion AS contacto_cedula
        FROM inmuebles_ph i
        LEFT JOIN conjuntos_residenciales cr ON cr.id = i.conjunto_id
        LEFT JOIN contactos pj ON pj.id = cr.contacto_id
        LEFT JOIN contactos c_legacy ON c_legacy.id = i.contacto_id
        LEFT JOIN LATERAL (
            SELECT c.nombre, c.identificacion
            FROM inmueble_propietarios ip
            JOIN contactos c ON c.id = ip.contacto_id
            WHERE ip.inmueble_id = i.id
            ORDER BY ip.es_principal DESC, c.id ASC
            LIMIT 1
        ) tit ON TRUE
    """


def _map_row(row: dict[str, Any]) -> dict[str, Any]:
    titular_nombre, titular_cedula = _titular_desde_fila(row)
    return {
        "inmueble_id": int(row["inmueble_id"]),
        "torre_apto": (row.get("torre_apto") or "").strip() or None,
        "conjunto_id": row.get("conjunto_id"),
        "conjunto_nombre": (
            (row.get("conjunto_nombre") or row.get("conjunto_catalogo") or "").strip()
            or None
        ),
        "copropiedad_nombre": (row.get("copropiedad_nombre") or "").strip() or None,
        "copropiedad_nit": (row.get("copropiedad_nit") or "").strip() or None,
        "ciudad": (row.get("ciudad") or "").strip() or None,
        "titular_nombre": titular_nombre,
        "titular_cedula": titular_cedula,
    }


def _unidad_coincide(torre_neon: str, claves_busqueda: set[str]) -> bool:
    if not torre_neon or not claves_busqueda:
        return False
    claves_neon = _claves_unidad(torre_neon)
    if claves_neon & claves_busqueda:
        return True
    # Contención solo entre claves con estructura bloque-apto (evita falsos + con "1")
    for kn in claves_neon:
        if "-" not in kn and not kn.isdigit():
            continue
        for kb in claves_busqueda:
            if len(kn) >= 3 and len(kb) >= 3 and (kn in kb or kb in kn):
                return True
    return False


def _titular_coincide(fila: dict[str, Any], titular: str) -> bool:
    if not (titular or "").strip():
        return False
    buscado = _norm_texto(titular)
    if len(buscado) < 4:
        return False
    for campo in ("titular_nombre", "contacto_nombre"):
        cand = _norm_texto(str(fila.get(campo) or ""))
        if cand and (buscado == cand or buscado in cand or cand in buscado):
            return True
    return False


def obtener_por_inmueble_id(inmueble_id: int, *, conn=None) -> Optional[dict[str, Any]]:
    """Busca inmueble + copropiedad + titular por PK."""
    owns = conn is None
    if owns:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                _sql_base() + " WHERE i.id = %s LIMIT 1",
                (int(inmueble_id),),
            )
            row = cur.fetchone()
            return _map_row(dict(row)) if row else None
    finally:
        if owns and conn is not None:
            conn.release()


def _fetch_candidatos_conjunto(
    cur,
    *,
    conjunto_id: Optional[int],
    conjunto_nombre: str,
) -> list[dict[str, Any]]:
    """Trae filas de inmuebles filtradas por conjunto (id o nombre flexible)."""
    clauses: list[str] = []
    params: list[Any] = []

    if conjunto_id:
        clauses.append("(i.conjunto_id = %s OR cr.id = %s)")
        params.extend([int(conjunto_id), int(conjunto_id)])

    nombre = (conjunto_nombre or "").strip()
    if nombre:
        # ILIKE con núcleo sin acentos/ruido legal; score fino en Python.
        nucleo = _nucleo_conjunto(nombre) or _norm_texto(nombre)
        patron = f"%{nucleo}%"
        clauses.append(
            "("
            " UPPER(BTRIM(COALESCE(i.conjunto_residencial, ''))) = UPPER(BTRIM(%s))"
            " OR UPPER(BTRIM(COALESCE(cr.nombre, ''))) = UPPER(BTRIM(%s))"
            " OR COALESCE(i.conjunto_residencial, '') ILIKE %s"
            " OR COALESCE(cr.nombre, '') ILIKE %s"
            " OR COALESCE(pj.nombre, '') ILIKE %s"
            ")"
        )
        params.extend([nombre, nombre, patron, patron, patron])

    if not clauses:
        return []

    # OR entre id y nombre: el PDF puede traer nombre distinto al id resuelto.
    where = " OR ".join(clauses) if len(clauses) > 1 else clauses[0]
    cur.execute(_sql_base() + f" WHERE {where}", params)
    return [dict(r) for r in cur.fetchall()]


def _filtrar_por_conjunto(
    filas: list[dict[str, Any]],
    *,
    conjunto_id: Optional[int],
    conjunto_nombre: str,
) -> list[dict[str, Any]]:
    """Refina candidatos: exige id o score de nombre > 0."""
    if conjunto_id:
        cid = int(conjunto_id)
        por_id = [
            f
            for f in filas
            if f.get("conjunto_id") is not None and int(f["conjunto_id"]) == cid
        ]
        if por_id:
            return por_id
    nombre = (conjunto_nombre or "").strip()
    if not nombre:
        return filas
    scored: list[tuple[int, dict[str, Any]]] = []
    for f in filas:
        s = max(
            _score_conjunto(str(f.get("conjunto_nombre") or ""), nombre),
            _score_conjunto(str(f.get("conjunto_catalogo") or ""), nombre),
            _score_conjunto(str(f.get("copropiedad_nombre") or ""), nombre),
        )
        if s > 0:
            scored.append((s, f))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    # Aceptar empatados en el mejor score (mismo conjunto, varias unidades).
    return [f for s, f in scored if s == best]


def _elegir_por_unidad(
    filas: list[dict[str, Any]],
    claves: set[str],
    *,
    titular: str = "",
) -> Optional[dict[str, Any]]:
    matches = [f for f in filas if _unidad_coincide(str(f.get("torre_apto") or ""), claves)]
    if not matches:
        return None
    if len(matches) == 1:
        return _map_row(matches[0])
    if titular:
        por_titular = [f for f in matches if _titular_coincide(f, titular)]
        if len(por_titular) == 1:
            return _map_row(por_titular[0])
        if por_titular:
            return _map_row(por_titular[0])
    return _map_row(matches[0])


def buscar_por_conjunto_y_unidad(
    *,
    conjunto_id: Optional[int] = None,
    conjunto_nombre: str = "",
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
    titular: str = "",
    conn=None,
) -> Optional[dict[str, Any]]:
    """
    Cruza conjunto (id o nombre flexible) + unidad (torre_apto / bloque+apto).

    `codigo_cuenta` COLON no existe en tablas maestras Neon; no se usa aquí.
    Si hay ambigüedad de unidad, `titular` (PDF) desambigua.
    """
    claves = _claves_unidad(torre_apto, bloque, apartamento)
    if not claves:
        return None
    if not conjunto_id and not (conjunto_nombre or "").strip() and not (titular or "").strip():
        return None

    owns = conn is None
    if owns:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            filas: list[dict[str, Any]] = []
            if conjunto_id or (conjunto_nombre or "").strip():
                filas = _fetch_candidatos_conjunto(
                    cur,
                    conjunto_id=conjunto_id,
                    conjunto_nombre=conjunto_nombre,
                )
                filas = _filtrar_por_conjunto(
                    filas,
                    conjunto_id=conjunto_id,
                    conjunto_nombre=conjunto_nombre,
                )
                hallado = _elegir_por_unidad(filas, claves, titular=titular)
                if hallado:
                    return hallado

            # Fallback: titular + unidad (cuando el nombre de conjunto del PDF
            # no cuadra con Neon, p.ej. razón social de la administradora).
            if (titular or "").strip():
                cur.execute(
                    _sql_base()
                    + """
                    WHERE (
                        UPPER(BTRIM(COALESCE(tit.nombre, ''))) = UPPER(BTRIM(%s))
                        OR COALESCE(tit.nombre, '') ILIKE %s
                        OR UPPER(BTRIM(COALESCE(c_legacy.nombre, ''))) = UPPER(BTRIM(%s))
                        OR COALESCE(c_legacy.nombre, '') ILIKE %s
                    )
                    """,
                    (
                        titular.strip(),
                        f"%{titular.strip()}%",
                        titular.strip(),
                        f"%{titular.strip()}%",
                    ),
                )
                por_titular = [dict(r) for r in cur.fetchall()]
                return _elegir_por_unidad(por_titular, claves, titular=titular)
            return None
    finally:
        if owns and conn is not None:
            conn.release()


def describir_busqueda(
    *,
    inmueble_id: Optional[int] = None,
    conjunto_id: Optional[int] = None,
    conjunto_nombre: str = "",
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
    titular: str = "",
    codigo_cuenta: str = "",
) -> str:
    """Texto legible de criterios usados (para 404 útiles)."""
    partes: list[str] = []
    if inmueble_id is not None:
        partes.append(f"inmueble_id={inmueble_id}")
    if conjunto_id:
        partes.append(f"conjunto_id={conjunto_id}")
    if (conjunto_nombre or "").strip():
        partes.append(f"conjunto={conjunto_nombre.strip()!r}")
    if (torre_apto or "").strip():
        partes.append(f"torre_apto={torre_apto.strip()!r}")
    if (bloque or "").strip() or (apartamento or "").strip():
        partes.append(f"bloque={bloque!r} apartamento={apartamento!r}")
    claves = sorted(_claves_unidad(torre_apto, bloque, apartamento))
    if claves:
        partes.append(f"claves_unidad={claves}")
    if (titular or "").strip():
        partes.append(f"titular={titular.strip()!r}")
    if (codigo_cuenta or "").strip():
        partes.append(
            f"codigo_cuenta={codigo_cuenta.strip()!r} (no indexa maestros Neon)"
        )
    return "; ".join(partes) if partes else "(sin criterios)"


def resolver_datos_certificado(
    *,
    inmueble_id: Optional[int] = None,
    conjunto_id: Optional[int] = None,
    conjunto_nombre: str = "",
    torre_apto: str = "",
    bloque: str = "",
    apartamento: str = "",
    titular: str = "",
    conn=None,
) -> Optional[dict[str, Any]]:
    """Resuelve datos maestros: prioriza inmueble_id, luego conjunto+unidad."""
    if inmueble_id is not None:
        hallado = obtener_por_inmueble_id(int(inmueble_id), conn=conn)
        if hallado:
            return hallado
    return buscar_por_conjunto_y_unidad(
        conjunto_id=conjunto_id,
        conjunto_nombre=conjunto_nombre,
        torre_apto=torre_apto,
        bloque=bloque,
        apartamento=apartamento,
        titular=titular,
        conn=conn,
    )
