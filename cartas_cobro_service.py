"""Cartas de cobro prejurídico en Word (.docx).

Lista candidatos PH por conjunto, deduplica por inmueble, genera documentos
con python-docx. El saldo proviene únicamente de obligacion_saldo_service
(gran_total del liquidador PH). No modifica liquidador ni SMS.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime, timedelta
from typing import Any, Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Pt, Cm
from psycopg2.extras import RealDictCursor

import catalogos_service
import db
import obligacion_saldo_service

TELEFONO_DESPACHO = "310 6927812"
CORREO_DESPACHO = "notificacionesdiegoparedes@outlook.com"
FIRMANTE_NOMBRE = "Diego Alejandro Paredes García"
FIRMANTE_CARGO = "Abogado Apoderado"
CIUDAD = "Pereira"
DIAS_LIMITE_DEFAULT = 5
MAX_LOTE = 100
CARTERAS_VALIDAS = {"PREJURIDICO", "JURIDICO"}
_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

PLANTILLA_VARIABLES = (
    {"clave": "deudor_nombre", "etiqueta": "Nombre completo del deudor"},
    {"clave": "cedula", "etiqueta": "Cédula / identificación"},
    {"clave": "torre_apto", "etiqueta": "Torre - Apto"},
    {"clave": "conjunto", "etiqueta": "Nombre del conjunto"},
    {"clave": "nombre_ph", "etiqueta": "Nombre PH / acreedor"},
    {"clave": "monto", "etiqueta": "Gran total (formato COP)"},
    {"clave": "fecha_limite", "etiqueta": "Fecha límite de pago (texto)"},
    {"clave": "atn_codeudor", "etiqueta": "Línea Atn. codeudor (vacía si no hay)"},
    {"clave": "codeudor_nombre", "etiqueta": "Nombre del codeudor (sin prefijo)"},
    {"clave": "ciudad", "etiqueta": "Ciudad (Pereira)"},
    {"clave": "telefono_despacho", "etiqueta": "Tel/WhatsApp del despacho"},
    {"clave": "correo_despacho", "etiqueta": "Correo del despacho"},
    {"clave": "firmante_nombre", "etiqueta": "Nombre del abogado"},
    {"clave": "firmante_cargo", "etiqueta": "Cargo del firmante"},
)

DEFAULT_CUERPO_PREJURIDICO = """Señor(a)
{{deudor_nombre}}
C.C. {{cedula}}
Propietario(a) del ({{torre_apto}}) ({{conjunto}}) {{ciudad}}.
{{atn_codeudor}}
REF: REQUERIMIENTO DE PAGO PREJURÍDICO - {{nombre_ph}}

Respetado(a) señor(a),

Actuando en mi calidad de apoderado legal de {{nombre_ph}}, me dirijo respetuosamente a usted con el fin de requerirle el pago de las obligaciones pendientes a cargo de la unidad ({{torre_apto}}) del conjunto {{conjunto}}, ubicado en {{ciudad}}.

A la fecha, revisada la liquidación de cartera correspondiente, se advierte una mora que asciende a la suma total de {{monto}}.

El bienestar y mantenimiento de la copropiedad dependen del pago oportuno de las cuotas de administración y demás conceptos a cargo de cada propietario. Su colaboración es indispensable para preservar los servicios comunes y evitar mayores costos para la comunidad.

Le otorgamos un plazo máximo hasta el {{fecha_limite}} para cancelar la totalidad de la suma adeudada o para formalizar un acuerdo de pago viable con este despacho.

Para gestionar su pago, aclarar saldos o radicar propuestas de acuerdo, puede comunicarse a través de los siguientes canales:
• Teléfono / WhatsApp: {{telefono_despacho}}
• Correo electrónico: {{correo_despacho}}

Hacemos de su conocimiento que, de no recibirse el pago o una propuesta seria dentro del plazo indicado, se adelantarán las gestiones prejurídicas y jurídicas pertinentes, con cargo de intereses, costas y honorarios a que haya lugar.

Confiamos en su voluntad de normalizar esta situación de manera pronta y cordial, evitando mayores inconvenientes.

Atentamente,


{{firmante_nombre}}
{{firmante_cargo}}"""

DEFAULT_CUERPO_JURIDICO = """Señor(a)
{{deudor_nombre}}
C.C. {{cedula}}
Propietario(a) del ({{torre_apto}}) ({{conjunto}}) {{ciudad}}.
{{atn_codeudor}}
REF: REQUERIMIENTO DE PAGO JURÍDICO - {{nombre_ph}}

Respetado(a) señor(a),

Actuando en mi calidad de apoderado legal de {{nombre_ph}}, me permito reiterar el requerimiento de pago de las obligaciones a cargo de la unidad ({{torre_apto}}) del conjunto {{conjunto}}, por la suma total de {{monto}}.

Le otorgamos un plazo máximo hasta el {{fecha_limite}} para cancelar la totalidad adeudada o formalizar un acuerdo de pago.

Canales de contacto:
• Teléfono / WhatsApp: {{telefono_despacho}}
• Correo electrónico: {{correo_despacho}}

De persistir el incumplimiento se continuarán las gestiones judiciales correspondientes.

Atentamente,


{{firmante_nombre}}
{{firmante_cargo}}"""



def fecha_limite_default(hoy: Optional[date] = None) -> date:
    return (hoy or date.today()) + timedelta(days=DIAS_LIMITE_DEFAULT)


def _fmt_money(value: Any) -> str:
    try:
        return f"$ {float(value or 0):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "$ 0"


def _fmt_fecha_es(value: date) -> str:
    meses = (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    )
    return f"{value.day} de {meses[value.month - 1]} de {value.year}"


def _safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", (text or "").strip(), flags=re.UNICODE)
    return cleaned.strip("_")[:80] or "sin_nombre"


def listar_candidatos(
    *,
    conjunto_id: int,
    tipo_cartera: str = "PREJURIDICO",
    fecha_corte: Optional[date] = None,
    saldo_minimo: float = 0.0,
    conn=None,
) -> list[dict]:
    """Candidatos PH del conjunto con saldo verificado, uno por inmueble."""
    cartera = (tipo_cartera or "PREJURIDICO").upper().strip()
    if cartera and cartera not in CARTERAS_VALIDAS:
        raise ValueError("Tipo de cartera no válido.")

    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            conjunto = catalogos_service.obtener_conjunto(cur, int(conjunto_id))
            if not conjunto:
                raise ValueError("Conjunto no encontrado.")

            where = [
                "o.inmueble_id IS NOT NULL",
                "UPPER(COALESCE(o.fuente_saldo,''))='EXPENSAS_PH'",
                "UPPER(COALESCE(o.estado,'ACTIVA')) NOT IN ('CANCELADA','ANULADA','PAGADA','INACTIVO','INACTIVA')",
                "NOT EXISTS ("
                " SELECT 1 FROM proceso_obligaciones po_inact"
                " JOIN procesos p_inact ON p_inact.radicado_interno=po_inact.radicado_interno"
                " WHERE po_inact.obligacion_id=o.id"
                "   AND UPPER(COALESCE(p_inact.estado,'ACTIVO'))='INACTIVO'"
                ")",
                "(i.conjunto_id=%s OR ("
                " i.conjunto_id IS NULL"
                " AND UPPER(BTRIM(COALESCE(i.conjunto_residencial,'')))=UPPER(BTRIM(%s))"
                "))",
            ]
            params: list[Any] = [int(conjunto_id), conjunto["nombre"]]

            if cartera:
                # Sin proceso vinculado se trata como prejurídico (mismo criterio SMS).
                where.append(
                    "(COALESCE(p.tipo_cartera,'PREJURIDICO')=%s OR p.radicado_interno IS NULL)"
                    if cartera == "PREJURIDICO"
                    else "COALESCE(p.tipo_cartera,'PREJURIDICO')=%s"
                )
                params.append(cartera)

            cur.execute(
                f"""
                SELECT DISTINCT ON (i.id)
                    i.id AS inmueble_id,
                    i.conjunto_id,
                    COALESCE(i.conjunto_residencial, %s) AS conjunto_residencial,
                    i.torre_apto,
                    o.id AS obligacion_id,
                    deudor.contacto_id AS deudor_contacto_id,
                    deudor.nombre AS deudor_nombre,
                    deudor.identificacion AS deudor_identificacion,
                    codeudor.nombre AS codeudor_nombre,
                    codeudor.identificacion AS codeudor_identificacion,
                    COALESCE(p.tipo_cartera,'PREJURIDICO') AS tipo_cartera,
                    p.radicado_interno,
                    %s AS nombre_ph,
                    %s AS nit_ph
                FROM obligaciones o
                JOIN inmuebles_ph i ON i.id=o.inmueble_id
                JOIN LATERAL (
                    SELECT c.id AS contacto_id, c.nombre, c.identificacion
                    FROM obligacion_partes op
                    JOIN contactos c ON c.id=op.contacto_id
                    WHERE op.obligacion_id=o.id
                      AND op.rol='DEUDOR'
                    ORDER BY op.es_principal DESC, op.contacto_id ASC
                    LIMIT 1
                ) deudor ON TRUE
                LEFT JOIN LATERAL (
                    SELECT c.nombre, c.identificacion
                    FROM obligacion_partes op
                    JOIN contactos c ON c.id=op.contacto_id
                    WHERE op.obligacion_id=o.id
                      AND op.rol='CODEUDOR'
                    ORDER BY op.es_principal DESC, op.contacto_id ASC
                    LIMIT 1
                ) codeudor ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p0.tipo_cartera, p0.radicado_interno
                    FROM procesos p0
                    JOIN proceso_obligaciones po0
                      ON po0.radicado_interno=p0.radicado_interno
                     AND po0.obligacion_id=o.id
                    ORDER BY po0.es_principal DESC, po0.id
                    LIMIT 1
                ) p ON TRUE
                WHERE {' AND '.join(where)}
                ORDER BY i.id, o.id DESC
                """,
                [
                    conjunto["nombre"],
                    conjunto.get("persona_juridica") or conjunto["nombre"],
                    conjunto.get("nit") or "",
                    *params,
                ],
            )
            filas = [dict(r) for r in cur.fetchall()]
    finally:
        if owns_conn and conn is not None:
            conn.release()

    corte = fecha_corte or date.today()
    minimo = float(saldo_minimo or 0)
    salida: list[dict] = []
    cache: dict[int, dict] = {}

    for fila in filas:
        oid = int(fila["obligacion_id"])
        if oid not in cache:
            cache[oid] = obligacion_saldo_service.calcular_saldo_obligacion(
                oid, fecha_corte=corte
            )
        saldo = cache[oid]
        if not saldo.get("saldo_verificado"):
            continue
        try:
            total = float(saldo.get("saldo_total") or 0)
        except (TypeError, ValueError):
            continue
        if total < minimo:
            continue

        item = dict(fila)
        item["saldo_total"] = round(total, 2)
        item["saldo_verificado"] = True
        item["saldo_fuente"] = saldo.get("saldo_fuente")
        item["fecha_corte"] = corte
        salida.append(item)

    return salida


def dedupe_por_inmueble(candidatos: list[dict]) -> list[dict]:
    """Garantiza una sola entrada por inmueble_id (primera gana)."""
    vistos: set[int] = set()
    salida: list[dict] = []
    for item in candidatos:
        iid = int(item["inmueble_id"])
        if iid in vistos:
            continue
        vistos.add(iid)
        salida.append(item)
    return salida


def resolver_seleccion(
    candidatos: list[dict],
    inmueble_ids: list[int],
) -> list[dict]:
    """Filtra por selección del usuario y vuelve a deduplicar."""
    ids = []
    vistos: set[int] = set()
    for raw in inmueble_ids:
        iid = int(raw)
        if iid in vistos:
            continue
        vistos.add(iid)
        ids.append(iid)

    if len(ids) > MAX_LOTE:
        raise ValueError(f"Máximo {MAX_LOTE} cartas por lote.")

    por_inmueble = {int(c["inmueble_id"]): c for c in dedupe_por_inmueble(candidatos)}
    seleccionados = []
    faltantes = []
    for iid in ids:
        item = por_inmueble.get(iid)
        if not item:
            faltantes.append(iid)
            continue
        seleccionados.append(item)
    if faltantes:
        raise ValueError(
            "Inmuebles no disponibles o sin saldo verificado: "
            + ", ".join(str(x) for x in faltantes[:10])
        )
    if not seleccionados:
        raise ValueError("Debe seleccionar al menos un inmueble.")
    return seleccionados


def ensure_cartas_plantillas_table(conn=None) -> None:
    """Crea la tabla y siembra plantillas sistema si faltan (idempotente)."""
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cartas_cobro_plantillas (
                        id BIGSERIAL PRIMARY KEY,
                        nombre TEXT NOT NULL,
                        tipo_cartera VARCHAR(20) NOT NULL
                            CHECK (tipo_cartera IN ('PREJURIDICO', 'JURIDICO')),
                        cuerpo TEXT NOT NULL,
                        activo BOOLEAN NOT NULL DEFAULT TRUE,
                        es_sistema BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CONSTRAINT uq_cartas_cobro_plantillas_nombre_tipo
                            UNIQUE (nombre, tipo_cartera)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_cartas_cobro_plantillas_tipo_activo
                        ON cartas_cobro_plantillas (tipo_cartera, activo)
                    """
                )
                cur.execute(
                    """
                    INSERT INTO cartas_cobro_plantillas
                        (nombre, tipo_cartera, cuerpo, activo, es_sistema)
                    VALUES
                        (%s, 'PREJURIDICO', %s, TRUE, TRUE),
                        (%s, 'JURIDICO', %s, TRUE, TRUE)
                    ON CONFLICT (nombre, tipo_cartera) DO NOTHING
                    """,
                    (
                        "Requerimiento prejurídico estándar",
                        DEFAULT_CUERPO_PREJURIDICO,
                        "Requerimiento jurídico estándar",
                        DEFAULT_CUERPO_JURIDICO,
                    ),
                )
    finally:
        if owns_conn and conn is not None:
            conn.release()


def listar_plantillas(
    *,
    tipo_cartera: Optional[str] = None,
    solo_activas: bool = True,
    conn=None,
) -> list[dict]:
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()
    try:
        ensure_cartas_plantillas_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where = ["TRUE"]
            params: list[Any] = []
            if solo_activas:
                where.append("activo=TRUE")
            if tipo_cartera:
                cartera = str(tipo_cartera).upper().strip()
                if cartera not in CARTERAS_VALIDAS:
                    raise ValueError("Tipo de cartera no válido.")
                where.append("tipo_cartera=%s")
                params.append(cartera)
            cur.execute(
                f"""
                SELECT id, nombre, tipo_cartera, cuerpo, activo, es_sistema,
                       created_at, updated_at
                FROM cartas_cobro_plantillas
                WHERE {' AND '.join(where)}
                ORDER BY tipo_cartera, es_sistema DESC, nombre
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        if owns_conn and conn is not None:
            conn.release()


def obtener_plantilla(plantilla_id: int, *, conn=None) -> Optional[dict]:
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()
    try:
        ensure_cartas_plantillas_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, nombre, tipo_cartera, cuerpo, activo, es_sistema,
                       created_at, updated_at
                FROM cartas_cobro_plantillas
                WHERE id=%s
                LIMIT 1
                """,
                (int(plantilla_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        if owns_conn and conn is not None:
            conn.release()


def guardar_plantilla(
    *,
    nombre: str,
    tipo_cartera: str,
    cuerpo: str,
    plantilla_id: Optional[int] = None,
    conn=None,
) -> dict:
    nombre_limpio = str(nombre or "").strip()
    cuerpo_limpio = str(cuerpo or "").strip()
    cartera = str(tipo_cartera or "").upper().strip()
    if not nombre_limpio:
        raise ValueError("El nombre de la plantilla es obligatorio.")
    if cartera not in CARTERAS_VALIDAS:
        raise ValueError("Tipo de cartera debe ser PREJURIDICO o JURIDICO.")
    if not cuerpo_limpio:
        raise ValueError("El cuerpo de la plantilla no puede estar vacío.")
    if len(cuerpo_limpio) > 50000:
        raise ValueError("El cuerpo de la plantilla es demasiado largo.")

    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()
    try:
        ensure_cartas_plantillas_table(conn)
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if plantilla_id:
                    cur.execute(
                        """
                        UPDATE cartas_cobro_plantillas
                        SET nombre=%s,
                            tipo_cartera=%s,
                            cuerpo=%s,
                            updated_at=NOW()
                        WHERE id=%s AND activo=TRUE
                        RETURNING id, nombre, tipo_cartera, cuerpo, activo, es_sistema
                        """,
                        (nombre_limpio, cartera, cuerpo_limpio, int(plantilla_id)),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise ValueError("Plantilla no encontrada.")
                    return dict(row)

                cur.execute(
                    """
                    INSERT INTO cartas_cobro_plantillas
                        (nombre, tipo_cartera, cuerpo, activo, es_sistema)
                    VALUES (%s, %s, %s, TRUE, FALSE)
                    RETURNING id, nombre, tipo_cartera, cuerpo, activo, es_sistema
                    """,
                    (nombre_limpio, cartera, cuerpo_limpio),
                )
                return dict(cur.fetchone())
    except Exception as exc:
        msg = str(exc).lower()
        if "uq_cartas_cobro_plantillas_nombre_tipo" in msg or "unique" in msg:
            raise ValueError(
                "Ya existe una plantilla con ese nombre para el tipo de cartera."
            ) from exc
        raise
    finally:
        if owns_conn and conn is not None:
            conn.release()


def desactivar_plantilla(plantilla_id: int, *, conn=None) -> None:
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_connection()
    try:
        ensure_cartas_plantillas_table(conn)
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT es_sistema FROM cartas_cobro_plantillas
                    WHERE id=%s LIMIT 1
                    """,
                    (int(plantilla_id),),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("Plantilla no encontrada.")
                if row.get("es_sistema"):
                    raise ValueError("No se puede eliminar una plantilla de sistema.")
                cur.execute(
                    """
                    UPDATE cartas_cobro_plantillas
                    SET activo=FALSE, updated_at=NOW()
                    WHERE id=%s
                    """,
                    (int(plantilla_id),),
                )
    finally:
        if owns_conn and conn is not None:
            conn.release()


def construir_contexto_variables(
    candidato: dict,
    *,
    fecha_limite: date,
) -> dict[str, str]:
    codeudor = (candidato.get("codeudor_nombre") or "").strip()
    conjunto = (candidato.get("conjunto_residencial") or "SIN CONJUNTO").strip()
    return {
        "deudor_nombre": (candidato.get("deudor_nombre") or "SIN NOMBRE").strip(),
        "cedula": (candidato.get("deudor_identificacion") or "SIN CÉDULA").strip(),
        "torre_apto": (candidato.get("torre_apto") or "SIN UNIDAD").strip(),
        "conjunto": conjunto,
        "nombre_ph": (candidato.get("nombre_ph") or conjunto).strip(),
        "monto": _fmt_money(candidato.get("saldo_total")),
        "fecha_limite": _fmt_fecha_es(fecha_limite),
        "codeudor_nombre": codeudor,
        "atn_codeudor": f"Atn. {codeudor}" if codeudor else "",
        "ciudad": CIUDAD,
        "telefono_despacho": TELEFONO_DESPACHO,
        "correo_despacho": CORREO_DESPACHO,
        "firmante_nombre": FIRMANTE_NOMBRE,
        "firmante_cargo": FIRMANTE_CARGO,
    }


def renderizar_cuerpo(cuerpo: str, contexto: dict[str, str]) -> str:
    def _repl(match: re.Match) -> str:
        clave = match.group(1)
        if clave not in contexto:
            return match.group(0)
        return str(contexto.get(clave) or "")

    return _VAR_PATTERN.sub(_repl, cuerpo or "")


def _aplicar_fuente_arial(run, *, size_pt: float = 11, bold: bool = False) -> None:
    """Fuerza Arial en ascii/hAnsi/eastAsia para que Word no sustituya la fuente."""
    run.bold = bold
    run.font.name = "Arial"
    run.font.size = Pt(size_pt)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), "Arial")
    rfonts.set(qn("w:hAnsi"), "Arial")
    rfonts.set(qn("w:cs"), "Arial")
    rfonts.set(qn("w:eastAsia"), "Arial")


def construir_carta_docx(
    candidato: dict,
    *,
    fecha_limite: date,
    fecha_carta: Optional[date] = None,
    cuerpo_plantilla: Optional[str] = None,
) -> bytes:
    """Genera un .docx en memoria (Arial 11, justificado) desde plantilla de texto."""
    doc = Document()
    for section in doc.sections:
        section.top_margin = Cm(2.5)
        section.bottom_margin = Cm(2.5)
        section.left_margin = Cm(3)
        section.right_margin = Cm(3)

    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)
    style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.space_before = Pt(0)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), "Arial")
    rfonts.set(qn("w:hAnsi"), "Arial")
    rfonts.set(qn("w:cs"), "Arial")
    rfonts.set(qn("w:eastAsia"), "Arial")

    _ = fecha_carta or date.today()
    contexto = construir_contexto_variables(candidato, fecha_limite=fecha_limite)
    cuerpo = cuerpo_plantilla if cuerpo_plantilla is not None else DEFAULT_CUERPO_PREJURIDICO
    texto = renderizar_cuerpo(cuerpo, contexto)
    lineas = texto.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def add_para(
        text: str = "",
        *,
        bold: bool = False,
        align=WD_ALIGN_PARAGRAPH.LEFT,
        space_after: float = 0,
    ):
        p = doc.add_paragraph()
        run = p.add_run(text)
        _aplicar_fuente_arial(run, bold=bold)
        p.alignment = align
        p.paragraph_format.space_after = Pt(space_after)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
        return p

    en_cuerpo = False
    for idx, linea in enumerate(lineas):
        strip = linea.strip()
        siguiente = lineas[idx + 1].strip() if idx + 1 < len(lineas) else ""

        if strip.startswith("Respetado"):
            en_cuerpo = True
        if strip.startswith("Atentamente"):
            en_cuerpo = False

        if not strip:
            # Conserva espacios del bloque de firma; comprime vacíos del encabezado.
            if en_cuerpo or siguiente.startswith("Atentamente") or not siguiente:
                add_para("", space_after=0)
            continue

        if strip.startswith("REF:"):
            add_para(strip, bold=True, space_after=12)
            continue

        if strip.startswith("•") or strip.startswith("- "):
            add_para(strip, align=WD_ALIGN_PARAGRAPH.LEFT, space_after=0)
            continue

        if en_cuerpo and not strip.startswith("Atentamente"):
            add_para(strip, align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=12)
        elif strip.startswith("Atentamente"):
            add_para(strip, space_after=24)
        else:
            add_para(strip, space_after=0)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def nombre_archivo_carta(candidato: dict, fecha: Optional[date] = None) -> str:
    dia = (fecha or date.today()).strftime("%Y%m%d")
    conjunto = _safe_filename(candidato.get("conjunto_residencial") or "conjunto")
    unidad = _safe_filename(candidato.get("torre_apto") or "unidad")
    return f"Carta_Prejuridica_{conjunto}_{unidad}_{dia}.docx"


def generar_paquete_docx(
    seleccionados: list[dict],
    *,
    fecha_limite: date,
    fecha_carta: Optional[date] = None,
    cuerpo_plantilla: Optional[str] = None,
) -> tuple[bytes, str, str]:
    """Devuelve (contenido, filename, media_type). ZIP si hay más de una carta."""
    fecha = fecha_carta or date.today()
    cuerpo = cuerpo_plantilla
    if len(seleccionados) == 1:
        contenido = construir_carta_docx(
            seleccionados[0],
            fecha_limite=fecha_limite,
            fecha_carta=fecha,
            cuerpo_plantilla=cuerpo,
        )
        return contenido, nombre_archivo_carta(seleccionados[0], fecha), (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        usados: set[str] = set()
        for item in seleccionados:
            nombre = nombre_archivo_carta(item, fecha)
            if nombre in usados:
                stem = nombre.rsplit(".", 1)[0]
                nombre = f"{stem}_{item['inmueble_id']}.docx"
            usados.add(nombre)
            zf.writestr(
                nombre,
                construir_carta_docx(
                    item,
                    fecha_limite=fecha_limite,
                    fecha_carta=fecha,
                    cuerpo_plantilla=cuerpo,
                ),
            )
    conjunto = _safe_filename(seleccionados[0].get("conjunto_residencial") or "conjunto")
    zip_name = f"Cartas_Prejuridicas_{conjunto}_{fecha.strftime('%Y%m%d')}.zip"
    return zip_buffer.getvalue(), zip_name, "application/zip"


def parse_fecha(value: Optional[str], *, default: date) -> date:
    texto = str(value or "").strip()
    if not texto:
        return default
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Fecha inválida. Use formato YYYY-MM-DD.") from exc
