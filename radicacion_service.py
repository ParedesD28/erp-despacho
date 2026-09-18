"""Servicio canónico de radicación de procesos.

La ruta HTTP transforma el formulario en datos normalizados y este servicio
ejecuta las validaciones de negocio y la transacción completa:
Proceso + Partes + Obligación cuando corresponde + vínculos.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg2.extras import RealDictCursor

import db
import catalogos_service
import expedientes_service
import obligaciones_service


def _generar_radicado_interno(cur) -> str:
    """Genera EXP-xxxx sin confundirlo con el radicado de la Rama Judicial."""
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (71302541,))
    cur.execute(
        """
        SELECT COALESCE(
            MAX(CAST(SUBSTRING(radicado_interno FROM 5) AS BIGINT)),
            0
        ) AS max
        FROM procesos
        WHERE radicado_interno ~ '^EXP-[0-9]+$'
        """
    )
    row = cur.fetchone()
    ultimo = int(row["max"] or 0) if row else 0
    return f"EXP-{ultimo + 1:04d}"


def _crear_actuacion_inicial(cur, radicado_interno: str) -> None:
    if not expedientes_service._table_exists(cur, "actuaciones"):
        return

    act_cols = expedientes_service._cols(cur, "actuaciones")
    payload = {
        "radicado_interno": radicado_interno,
        "fecha": date.today(),
        "etapa": "Inicio",
        "descripcion": "Presentación inicial de la demanda",
        "usuario": "Sistema",
        "tipificacion_sugerida": "Radicación",
    }
    use = [c for c in payload if c in act_cols]
    if not use:
        raise RuntimeError(
            "La tabla actuaciones no contiene columnas compatibles para la actuación inicial"
        )

    cur.execute(
        f"""
        INSERT INTO actuaciones ({', '.join(use)})
        VALUES ({', '.join(['%s'] * len(use))})
        """,
        [payload[c] for c in use],
    )


def radicar_proceso(
    *,
    naturaleza: str,
    tipo_obligacion_codigo: str,
    tipo_cartera: str,
    radicado_rama: str,
    juzgado: str | None,
    apto: str,
    conjunto_id_raw: str,
    conjunto_nombre: str,
    abogado_id: int | None,
    medidas: str,
    pretensiones: Any,
    capital_titulo: Any,
    documento_referencia: str,
    fecha_exigibilidad: str,
    demandantes: list[str],
    nuevos_dem: list[tuple[str, str]],
    demandados: list[str],
    nuevos_ddo: list[tuple[str, str]],
) -> dict:
    """Radica un proceso y, si corresponde, crea su obligación en una sola transacción."""
    naturaleza = str(naturaleza or "").strip().upper()
    tipo_obligacion_codigo = str(tipo_obligacion_codigo or "").strip().upper()
    tipo_cartera = str(tipo_cartera or "").strip().upper()
    radicado_rama = str(radicado_rama or "").strip().upper()
    juzgado = str(juzgado or "").strip() or None
    apto = str(apto or "").strip()
    conjunto_id_raw = str(conjunto_id_raw or "").strip()
    conjunto_nombre = str(conjunto_nombre or "").strip()
    documento_referencia = str(documento_referencia or "").strip()
    fecha_exigibilidad = str(fecha_exigibilidad or "").strip()
    medidas = str(medidas or "").strip()

    demandantes = [str(x).strip() for x in (demandantes or []) if str(x).strip()]
    demandados = [str(x).strip() for x in (demandados or []) if str(x).strip()]
    nuevos_dem = [(str(i).strip(), str(n).strip()) for i, n in (nuevos_dem or [])]
    nuevos_ddo = [(str(i).strip(), str(n).strip()) for i, n in (nuevos_ddo or [])]

    if naturaleza not in {"EJECUTIVO", "VERBAL"}:
        raise ValueError("La naturaleza debe ser EJECUTIVO o VERBAL")

    if tipo_cartera not in {"JURIDICO", "PREJURIDICO"}:
        raise ValueError("Tipo de cartera no válido")

    if naturaleza == "VERBAL" and tipo_obligacion_codigo:
        raise ValueError("Un proceso verbal no crea automáticamente una obligación")

    if naturaleza == "VERBAL" and tipo_cartera == "PREJURIDICO":
        raise ValueError("Un proceso VERBAL no puede quedar PREJURÍDICO")

    for identificacion, nombre in nuevos_dem + nuevos_ddo:
        if not identificacion or not nombre:
            raise ValueError("Hay nuevos contactos con identificación y nombre incompletos")

    interseccion = (set(demandantes) & set(demandados))
    interseccion.update({i for i, _ in nuevos_dem} & {i for i, _ in nuevos_ddo})
    if interseccion:
        raise ValueError("Una misma persona no puede ser demandante y demandado en el mismo proceso")

    if not demandantes and not nuevos_dem:
        raise ValueError("Debe existir al menos un demandante")

    if not demandados and not nuevos_ddo:
        raise ValueError("Debe existir al menos un demandado")

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                tipo_proceso = catalogos_service.obtener_tipo_proceso(cur, naturaleza)
                if not tipo_proceso:
                    raise ValueError("El procedimiento seleccionado no está configurado")

                tipo_obligacion = None
                if naturaleza == "EJECUTIVO":
                    if not tipo_obligacion_codigo:
                        raise ValueError("Todo proceso ejecutivo debe indicar el tipo de obligación")
                    tipo_obligacion = catalogos_service.obtener_tipo_obligacion(
                        cur,
                        tipo_obligacion_codigo,
                    )
                    if not tipo_obligacion:
                        raise ValueError("El tipo de obligación seleccionado no está configurado")

                conjunto = None
                if tipo_obligacion and tipo_obligacion["requiere_conjunto"]:
                    if demandantes or nuevos_dem:
                        raise ValueError(
                            "En cuotas de administración el acreedor se toma del conjunto; "
                            "no se debe ingresar demandante manualmente"
                        )

                    if conjunto_id_raw:
                        try:
                            conjunto = catalogos_service.obtener_conjunto(
                                cur,
                                int(conjunto_id_raw),
                            )
                        except (TypeError, ValueError):
                            raise ValueError("El conjunto residencial seleccionado no es válido")
                    elif conjunto_nombre:
                        cur.execute(
                            """
                            SELECT id
                            FROM conjuntos_residenciales
                            WHERE activo=TRUE
                              AND UPPER(BTRIM(nombre))=UPPER(BTRIM(%s))
                            LIMIT 1
                            """,
                            (conjunto_nombre,),
                        )
                        row = cur.fetchone()
                        if row:
                            conjunto = catalogos_service.obtener_conjunto(cur, int(row["id"]))

                    if not conjunto:
                        raise ValueError("Debes seleccionar un conjunto residencial válido")

                    if not conjunto.get("contacto_id"):
                        raise ValueError(
                            "El conjunto debe tener configurada su persona jurídica antes de radicar"
                        )

                    cur.execute(
                        """
                        SELECT id, identificacion, nombre
                        FROM contactos
                        WHERE id=%s
                        LIMIT 1
                        """,
                        (conjunto["contacto_id"],),
                    )
                    acreedor_row = cur.fetchone()
                    if not acreedor_row or not acreedor_row["identificacion"]:
                        raise ValueError(
                            "La persona jurídica del conjunto no tiene identificación configurada"
                        )
                    demandantes = [str(acreedor_row["identificacion"])]

                if tipo_obligacion and tipo_obligacion["requiere_inmueble"] and not apto:
                    raise ValueError("Debes indicar la torre/apartamento del inmueble")

                if tipo_obligacion and tipo_obligacion["requiere_documento"]:
                    if not documento_referencia:
                        raise ValueError("Debes indicar el número o referencia del documento")
                    if float(capital_titulo or 0) <= 0:
                        raise ValueError("El capital de la obligación debe ser mayor que cero")
                elif tipo_obligacion and tipo_obligacion["codigo"] != "CUOTAS_ADMINISTRACION":
                    if float(capital_titulo or 0) <= 0:
                        raise ValueError("El capital inicial de la obligación debe ser mayor que cero")

                if tipo_cartera == "PREJURIDICO":
                    radicado_rama_db = None
                    estado_rama = "NO_APLICA"
                    juzgado_db = None
                else:
                    radicado_rama_db = radicado_rama or None
                    estado_rama = "ASIGNADO" if radicado_rama_db else "PENDIENTE_REPARTO"
                    juzgado_db = juzgado

                    if radicado_rama_db and not juzgado_db:
                        raise ValueError(
                            "Cuando ya existe radicado Rama debes indicar el juzgado de conocimiento"
                        )

                if radicado_rama_db:
                    cur.execute(
                        """
                        SELECT 1
                        FROM procesos
                        WHERE radicado_rama=%s
                          AND radicado_rama IS NOT NULL
                        LIMIT 1
                        """,
                        (radicado_rama_db,),
                    )
                    if cur.fetchone():
                        raise ValueError("El radicado Rama Judicial ya existe")

                for ident, nombre in nuevos_dem:
                    cur.execute(
                        """
                        INSERT INTO contactos (identificacion,nombre,tipo,ciudad)
                        VALUES (%s,%s,'Cliente','PEREIRA')
                        ON CONFLICT (identificacion)
                        DO UPDATE SET nombre=EXCLUDED.nombre
                        """,
                        (ident, nombre),
                    )

                for ident, nombre in nuevos_ddo:
                    cur.execute(
                        """
                        INSERT INTO contactos (identificacion,nombre,tipo,ciudad)
                        VALUES (%s,%s,'Contraparte','PEREIRA')
                        ON CONFLICT (identificacion)
                        DO UPDATE SET nombre=EXCLUDED.nombre
                        """,
                        (ident, nombre),
                    )

                demandantes += [x[0] for x in nuevos_dem]
                demandados += [x[0] for x in nuevos_ddo]

                if not demandantes or not demandados:
                    raise ValueError("Debe existir al menos un demandante y un demandado")

                all_ids = list(dict.fromkeys(demandantes + demandados))
                placeholders = ",".join(["%s"] * len(all_ids))
                cur.execute(
                    f"""
                    SELECT id, identificacion, nombre
                    FROM contactos
                    WHERE identificacion IN ({placeholders})
                    """,
                    all_ids,
                )
                contacts = {str(r["identificacion"]): dict(r) for r in cur.fetchall()}
                faltantes = [ident for ident in all_ids if ident not in contacts]
                if faltantes:
                    raise ValueError("Hay partes seleccionadas que no existen en Contactos")

                # Repetir la protección después de crear contactos nuevos.
                if set(demandantes) & set(demandados):
                    raise ValueError("Una misma persona no puede ser demandante y demandado en el mismo proceso")

                radicado_interno = _generar_radicado_interno(cur)

                inmueble_id = None
                if tipo_obligacion and tipo_obligacion["requiere_inmueble"]:
                    conjunto_id = int(conjunto["id"])
                    cur.execute(
                        """
                        SELECT id
                        FROM inmuebles_ph
                        WHERE conjunto_id=%s
                          AND torre_apto=%s
                        LIMIT 1
                        """,
                        (conjunto_id, apto),
                    )
                    inmueble = cur.fetchone()

                    if inmueble:
                        inmueble_id = inmueble["id"]
                    else:
                        demandado_principal = contacts[demandados[0]]
                        cur.execute(
                            """
                            INSERT INTO inmuebles_ph
                                (contacto_id,conjunto_residencial,conjunto_id,torre_apto)
                            VALUES (%s,%s,%s,%s)
                            RETURNING id
                            """,
                            (
                                demandado_principal["id"],
                                conjunto["nombre"],
                                conjunto_id,
                                apto,
                            ),
                        )
                        inmueble_id = cur.fetchone()["id"]

                data = {
                    "radicado_interno": radicado_interno,
                    "radicado_rama": radicado_rama_db,
                    "estado_rama": estado_rama,
                    "tipo_cartera": tipo_cartera,
                    "tipo_proceso_id": tipo_proceso["id"],
                    "naturaleza": naturaleza,
                    "etapa_actual": (
                        "1. Presentación de la demanda"
                        if tipo_cartera == "JURIDICO"
                        else None
                    ),
                    "juzgado": juzgado_db,
                    "estado": "Activo",
                    "id_demandado": " | ".join(demandados),
                    "demandado": " | ".join(contacts[x]["nombre"] for x in demandados),
                    "inmueble_id": inmueble_id,
                    "id_cliente": " | ".join(demandantes),
                    "demandante": " | ".join(contacts[x]["nombre"] for x in demandantes),
                    "pretensiones": pretensiones,
                    "medidas_cautelares": medidas,
                    "abogado_id": abogado_id,
                }

                cols = expedientes_service._cols(cur, "procesos")
                usable = [c for c in data if c in cols]
                cur.execute(
                    f"""
                    INSERT INTO procesos ({', '.join(usable)})
                    VALUES ({', '.join(['%s'] * len(usable))})
                    """,
                    [data[c] for c in usable],
                )

                if not expedientes_service._table_exists(cur, "proceso_partes"):
                    raise RuntimeError(
                        "La tabla proceso_partes es obligatoria para una nueva radicación"
                    )

                for idx, ident in enumerate(demandantes):
                    contacto = contacts[ident]
                    cur.execute(
                        """
                        INSERT INTO proceso_partes
                            (radicado_interno,contacto_id,rol,es_principal,fecha_vinculacion)
                        VALUES (%s,%s,'DEMANDANTE',%s,CURRENT_TIMESTAMP)
                        ON CONFLICT (radicado_interno,contacto_id,rol)
                        DO UPDATE SET es_principal=EXCLUDED.es_principal
                        """,
                        (radicado_interno, contacto["id"], idx == 0),
                    )

                for idx, ident in enumerate(demandados):
                    contacto = contacts[ident]
                    cur.execute(
                        """
                        INSERT INTO proceso_partes
                            (radicado_interno,contacto_id,rol,es_principal,fecha_vinculacion)
                        VALUES (%s,%s,'DEMANDADO',%s,CURRENT_TIMESTAMP)
                        ON CONFLICT (radicado_interno,contacto_id,rol)
                        DO UPDATE SET es_principal=EXCLUDED.es_principal
                        """,
                        (radicado_interno, contacto["id"], idx == 0),
                    )

                obligacion_id = None
                if tipo_obligacion:
                    deudor_principal = contacts[demandados[0]]
                    acreedor = contacts[demandantes[0]]
                    capital = (
                        0
                        if tipo_obligacion["codigo"] == "CUOTAS_ADMINISTRACION"
                        else capital_titulo
                    )

                    obligacion_id = obligaciones_service.crear_obligacion(
                        cur,
                        radicado_interno=radicado_interno,
                        tipo_proceso_id=tipo_proceso["id"],
                        tipo_obligacion=tipo_obligacion,
                        deudor=deudor_principal,
                        acreedor=acreedor,
                        inmueble_id=inmueble_id,
                        numero_documento=documento_referencia,
                        capital_inicial=capital,
                        fecha_exigibilidad=fecha_exigibilidad or None,
                    )

                    obligaciones_service.vincular_partes_obligacion(
                        cur,
                        obligacion_id=obligacion_id,
                        contactos_deudores=[contacts[x] for x in demandados],
                    )

                    obligaciones_service.vincular_obligacion_a_proceso(
                        cur,
                        radicado_interno=radicado_interno,
                        obligacion_id=obligacion_id,
                    )

                if expedientes_service._table_exists(cur, "procesos_litisconsorcio"):
                    lit_cols = expedientes_service._cols(cur, "procesos_litisconsorcio")
                    for ident in demandados:
                        payload = {
                            "radicado_interno": radicado_interno,
                            "identificacion_demandado": ident,
                        }
                        use = [c for c in payload if c in lit_cols]
                        if use:
                            cur.execute(
                                f"""
                                INSERT INTO procesos_litisconsorcio ({', '.join(use)})
                                VALUES ({', '.join(['%s'] * len(use))})
                                ON CONFLICT (radicado_interno,identificacion_demandado)
                                DO NOTHING
                                """,
                                [payload[c] for c in use],
                            )

                if tipo_cartera == "JURIDICO":
                    _crear_actuacion_inicial(cur, radicado_interno)

                return {
                    "radicado_interno": radicado_interno,
                    "obligacion_id": obligacion_id,
                    "naturaleza": naturaleza,
                    "tipo_cartera": tipo_cartera,
                    "tipo_obligacion": (
                        tipo_obligacion["codigo"] if tipo_obligacion else None
                    ),
                    "estado_rama": estado_rama,
                }
    finally:
        conn.release()
