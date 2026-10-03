"""Capa de acceso a Neon para Certificados de Deuda.

Driver real: psycopg2 + ThreadedConnectionPool (`db.get_connection()`).
No hay SQLAlchemy ni asyncpg: las consultas son síncronas y deben
envolverse con `asyncio.to_thread()` desde endpoints FastAPI.
"""
from __future__ import annotations

import re
import unicodedata
from itertools import product
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
    r"PROPIEDAD\s+HORIZONTAL|"
    r"P\.?\s*H\.?|"  # P.H. / PH / P H (punto final se limpia aparte)
    r"PH|"
    r"URBANIZACI[OÓ]N|URB\.?|CONJUNTO\s+RESIDENCIAL|"
    r"CONJUNTO|EDIFICIO|RESIDENCIAL|UNIDAD"
    r")\b\.?",
    re.IGNORECASE,
)
# Prefijos de catálogo PDF tipo "D - MIRADOR…", "A- …"
_CONJUNTO_LETRA_PREFIJO_RE = re.compile(r"^[A-Z]\s*[-–—:.]+\s*", re.IGNORECASE)
_CONJUNTO_ETAPA_RE = re.compile(r"\bETAPA\s+\d+\b", re.IGNORECASE)
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


def _strip_ceros_token(token: str) -> str:
    """Quita ceros a la izquierda de la parte numérica: 02→2, 042→42, T02→T2."""
    m = re.match(r"^([A-Z]*)(0*)(\d+)([A-Z]*)$", token)
    if not m:
        return token
    letters, _zeros, digits, suffix = m.groups()
    return f"{letters}{str(int(digits))}{suffix}"


def _clave_unidad(texto: str = "", *, bloque: str = "", apartamento: str = "") -> str:
    """
    Clave de matching bloque-apto (puede conservar ceros; ver variantes).

    Para el formato maestro al guardar usar `clave_canonica_unidad` / `normalizar_torre_apto`.
    Ejemplos → `1-201` / `02-042` (las variantes unifican ceros):
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


def clave_canonica_unidad(
    texto: str = "",
    *,
    bloque: str = "",
    apartamento: str = "",
) -> str:
    """
    Clave canónica de unidad para maestros Neon: `torre-apto` **sin ceros a la izquierda**.

    `TORRE 02 APTO 042` / `02-042` / bloque=02+apto=042 → `2-42`.
    """
    clave = _clave_unidad(texto, bloque=bloque, apartamento=apartamento)
    if not clave:
        return ""
    partes = [_strip_ceros_token(p) for p in clave.split("-") if p]
    return "-".join(partes) if partes else ""


def normalizar_torre_apto(
    texto: str = "",
    *,
    bloque: str = "",
    apartamento: str = "",
) -> str:
    """Normaliza `torre_apto` al formato canónico antes de INSERT/UPDATE."""
    return clave_canonica_unidad(texto, bloque=bloque, apartamento=apartamento)


def componer_torre_apto_form(
    *,
    bloque: str = "",
    apartamento: str = "",
    texto_legacy: str = "",
) -> str:
    """
    Compone la clave canónica desde formularios UI.

    Prioridad: bloque+apartamento separados → texto libre legacy (`apto` / `torre_apto`).
    """
    b = str(bloque or "").strip()
    a = str(apartamento or "").strip()
    legacy = str(texto_legacy or "").strip()
    if b and a:
        return normalizar_torre_apto("", bloque=b, apartamento=a) or f"{b}-{a}".strip("-")
    if legacy:
        return normalizar_torre_apto(legacy) or legacy
    if a:
        return normalizar_torre_apto(a) or a
    if b:
        return normalizar_torre_apto(b) or b
    return ""


def partir_bloque_apto(clave_o_texto: str = "") -> tuple[str, str]:
    """Devuelve (bloque, apto) canónicos sin ceros; vacío si no hay dos tokens."""
    canon = clave_canonica_unidad(clave_o_texto)
    if "-" not in canon:
        return "", ""
    partes = canon.split("-")
    if len(partes) < 2:
        return "", ""
    return partes[0], partes[-1]


def _variantes_clave_unidad(clave: str) -> set[str]:
    """
    Variantes canónicas de una clave bloque-apto.

    `02-042` → también `2-42`, `02-42`, `2-042` (+ compactos dígitos).
    """
    if not clave:
        return set()
    out: set[str] = {clave}
    partes = clave.split("-")
    # Alias: quitar letras líderes del primer token (T1-201 → 1-201)
    if partes:
        first = re.sub(r"^[A-Z]+", "", partes[0]) or partes[0]
        out.add("-".join([first] + partes[1:]))

    # Combinaciones con/sin ceros a la izquierda por segmento.
    opciones: list[set[str]] = []
    for p in partes:
        stripped = _strip_ceros_token(p)
        opciones.append({p, stripped} if stripped != p else {p})
    for combo in product(*opciones):
        out.add("-".join(combo))

    for c in list(out):
        solo_digitos = re.sub(r"\D+", "", c)
        if len(solo_digitos) >= 3:
            out.add(solo_digitos)
        # Compacto también sin ceros líderes globales
        if solo_digitos.isdigit() and solo_digitos:
            out.add(str(int(solo_digitos)))
    return {c for c in out if c}


def _tupla_numerica_unidad(clave: str) -> Optional[tuple[int, ...]]:
    """(2, 42) desde `02-042` / `2-42` / `T02-042` para comparar numéricamente."""
    if "-" not in (clave or ""):
        return None
    nums: list[int] = []
    for parte in clave.split("-"):
        m = re.search(r"(\d+)", parte)
        if not m:
            return None
        nums.append(int(m.group(1)))
    return tuple(nums) if len(nums) >= 2 else None


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
        claves.update(_variantes_clave_unidad(clave))

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


def _nucleo_conjunto(nombre: str) -> str:
    """Quita prefijos/sufijos legales (D -, PH, ETAPA N, URBANIZACIÓN, …)."""
    texto = _norm_texto(nombre)
    # "D - MIRADOR…" / "A- FOO"
    texto = _CONJUNTO_LETRA_PREFIJO_RE.sub("", texto)
    texto = _CONJUNTO_NOISE_RE.sub(" ", texto)
    texto = _CONJUNTO_ETAPA_RE.sub(" ", texto)
    # Puntos/guiones sueltos que deja P.H. u otros
    texto = re.sub(r"[.\-–,;:]+", " ", texto)
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
            pj.direccion AS copropiedad_direccion,
            pj.telefono AS copropiedad_telefono,
            pj.email AS copropiedad_email,
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
        "copropiedad_direccion": (row.get("copropiedad_direccion") or "").strip() or None,
        "copropiedad_telefono": (row.get("copropiedad_telefono") or "").strip() or None,
        "copropiedad_email": (row.get("copropiedad_email") or "").strip() or None,
        "ciudad": (row.get("ciudad") or "").strip() or None,
        "titular_nombre": titular_nombre,
        "titular_cedula": titular_cedula,
    }


def _unidad_coincide(torre_neon: str, claves_busqueda: set[str]) -> bool:
    """
    Match exacto de unidad (token/clave canónica), no substring.

    Acepta alias de formato (`TORRE 1 APTO 201` ≡ `1-201` ≡ `T1-201`) y
    equivalencia por ceros (`02-042` ≡ `2-42`), pero **no** sufijos ni
    contención: `1124` ≠ `124`, `1101` ≠ `101`, `1-124` ≠ `1-24`.
    """
    if not torre_neon or not claves_busqueda:
        return False
    claves_neon = _claves_unidad(torre_neon)
    if claves_neon & claves_busqueda:
        return True
    # Comparación numérica torre/apto: 02-042 ≡ 2-42 ≡ 02-42
    nums_busqueda = {
        t for kb in claves_busqueda if (t := _tupla_numerica_unidad(kb)) is not None
    }
    nums_neon = {
        t for kn in claves_neon if (t := _tupla_numerica_unidad(kn)) is not None
    }
    if nums_busqueda and nums_neon and (nums_busqueda & nums_neon):
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


def listar_propietarios_inmueble(
    inmueble_id: int,
    *,
    conn=None,
) -> list[dict[str, Any]]:
    """
    Propietarios del inmueble (Neon). Orden: principal primero, luego id.

    Regla producto: **un Word por unidad** que relaciona a TODOS; el principal
    llena `titular_*` de la plantilla. No hay rol codeudor aquí (gap: solo
    co-propietarios de `inmueble_propietarios`).
    """
    owns = conn is None
    if owns:
        conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    ip.contacto_id,
                    ip.es_principal,
                    c.nombre,
                    c.identificacion AS cedula
                FROM inmueble_propietarios ip
                JOIN contactos c ON c.id = ip.contacto_id
                WHERE ip.inmueble_id = %s
                ORDER BY ip.es_principal DESC, c.id ASC
                """,
                (int(inmueble_id),),
            )
            filas = []
            for r in cur.fetchall():
                es_prin = bool(r.get("es_principal"))
                filas.append(
                    {
                        "contacto_id": int(r["contacto_id"]),
                        "nombre": (r.get("nombre") or "").strip() or None,
                        "cedula": (r.get("cedula") or "").strip() or None,
                        "es_principal": es_prin,
                        # Gap: Neon no distingue codeudor; rol documental de PH.
                        "rol": "principal" if es_prin else "co_propietario",
                    }
                )
            return filas
    finally:
        if owns and conn is not None:
            conn.release()


def titular_principal_de_propietarios(
    propietarios: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Elige el titular principal (o el primero) para el certificado."""
    if not propietarios:
        return None
    for p in propietarios:
        if p.get("es_principal"):
            return p
    return propietarios[0]


def diagnosticar_propietarios(
    propietarios: list[dict[str, Any]],
    *,
    titular_pdf: str = "",
) -> dict[str, Any]:
    """
    Señales de calidad para preview (no bloquean por sí solas).

    Cédula faltante en secundario → advertencia; en principal la valida
    `validar_datos_criticos` al emitir.
    """
    props = list(propietarios or [])
    principales = [p for p in props if p.get("es_principal")]
    principal = titular_principal_de_propietarios(props)
    advertencias: list[str] = []
    if props and not principales:
        advertencias.append(
            "Ningún propietario marcado es_principal; se usa el primero de la lista."
        )
    if len(principales) > 1:
        advertencias.append(
            f"Varios propietarios marcados es_principal ({len(principales)}); "
            "se usa el primero por id."
        )
    for p in props:
        if p is principal:
            continue
        if not (p.get("cedula") or "").strip():
            advertencias.append(
                f"Cédula faltante en co-propietario: {p.get('nombre') or '—'}"
            )
    pdf_norm = _norm_texto(titular_pdf)
    prin_norm = _norm_texto(str((principal or {}).get("nombre") or ""))
    titular_pdf_distinto = False
    if pdf_norm and prin_norm and len(pdf_norm) >= 4:
        if not (
            pdf_norm == prin_norm
            or pdf_norm in prin_norm
            or prin_norm in pdf_norm
        ):
            titular_pdf_distinto = True
            advertencias.append(
                "Titular del PDF no coincide con el principal Neon; "
                "el Word usa el principal y lista a todos."
            )
    return {
        "n_propietarios": len(props),
        "n_principales": len(principales),
        "sin_principal_marcado": bool(props) and not principales,
        "varios_principales": len(principales) > 1,
        "titular_pdf_distinto_de_principal": titular_pdf_distinto,
        "advertencias": advertencias,
        "principal": principal,
    }


def enriquecer_con_propietarios(
    datos: Optional[dict[str, Any]],
    *,
    conn=None,
    titular_pdf: str = "",
) -> Optional[dict[str, Any]]:
    """Añade lista de deudores y titular_principal al dict de match Neon."""
    if not datos or datos.get("inmueble_id") is None:
        return datos
    out = dict(datos)
    propietarios = listar_propietarios_inmueble(int(out["inmueble_id"]), conn=conn)
    if not propietarios and (out.get("titular_nombre") or out.get("titular_cedula")):
        # Fallback legacy: contacto_id del inmueble ya proyectado en titular_*
        propietarios = [
            {
                "contacto_id": None,
                "nombre": out.get("titular_nombre"),
                "cedula": out.get("titular_cedula"),
                "es_principal": True,
                "rol": "principal",
            }
        ]
    diag = diagnosticar_propietarios(propietarios, titular_pdf=titular_pdf)
    principal = diag.get("principal")
    out["propietarios"] = propietarios
    out["deudores"] = propietarios
    out["titular_principal"] = principal
    out["diagnostico_propietarios"] = diag
    out["advertencias"] = list(diag.get("advertencias") or [])
    if principal:
        # Asegura que el certificado use el principal aunque el LATERAL fallara.
        if principal.get("nombre"):
            out["titular_nombre"] = principal["nombre"]
        if principal.get("cedula"):
            out["titular_cedula"] = principal["cedula"]
    # Torre canónica para respuesta UI
    out["clave_canonica"] = clave_canonica_unidad(str(out.get("torre_apto") or ""))
    return out


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
    incluir_propietarios: bool = False,
) -> Optional[dict[str, Any]]:
    """Resuelve datos maestros: prioriza inmueble_id, luego conjunto+unidad."""
    hallado: Optional[dict[str, Any]] = None
    if inmueble_id is not None:
        hallado = obtener_por_inmueble_id(int(inmueble_id), conn=conn)
    if hallado is None:
        hallado = buscar_por_conjunto_y_unidad(
            conjunto_id=conjunto_id,
            conjunto_nombre=conjunto_nombre,
            torre_apto=torre_apto,
            bloque=bloque,
            apartamento=apartamento,
            titular=titular,
            conn=conn,
        )
    if incluir_propietarios and hallado:
        return enriquecer_con_propietarios(
            hallado, conn=conn, titular_pdf=titular
        )
    return hallado
