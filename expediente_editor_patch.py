"""Edicion segura de expedientes y normalizacion de demandados.

Se reemplaza el endpoint de detalle existente despues de que las rutas base
hayan sido cargadas. El radicado_interno se conserva como identificador tecnico;
se editan los metadatos de negocio sin alterar las relaciones historicas.
"""
from decimal import Decimal, InvalidOperation
from fastapi import Request, Form
from fastapi.responses import RedirectResponse
from psycopg2.extras import RealDictCursor
import main

app = main.app
templates = main.templates


def _conn():
    return main.db_pool.getconn()


def _release(conn):
    if conn is not None:
        main.db_pool.putconn(conn)


def _cols(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def _dedupe_pairs(ids_text: str, names_text: str):
    ids = [x.strip() for x in str(ids_text or "").split("|") if x.strip()]
    names = [x.strip() for x in str(names_text or "").split("|") if x.strip()]
    pairs = []
    seen = set()
    for idx, ident in enumerate(ids):
        nombre = names[idx] if idx < len(names) else ""
        key = (ident, " ".join(nombre.upper().split()))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((ident, nombre))
    for idx in range(len(ids), len(names)):
        nombre = names[idx]
        key = ("", " ".join(nombre.upper().split()))
        if key not in seen:
            seen.add(key)
            pairs.append(("", nombre))
    unique_ids = []
    unique_names = []
    for ident, nombre in pairs:
        if ident and ident not in unique_ids:
            unique_ids.append(ident)
            unique_names.append(nombre)
        elif not ident and nombre:
            if nombre not in unique_names:
                unique_names.append(nombre)
    return unique_ids, unique_names


def _lookup_proceso(cur, radicado):
    cur.execute(
        "SELECT p.*, a.nombre AS abogado_asignado "
        "FROM procesos p LEFT JOIN abogados a ON p.abogado_id=a.id "
        "WHERE p.radicado_interno=%s LIMIT 1",
        (radicado,),
    )
    return cur.fetchone()


def _build_context(cur, proceso):
    proceso = dict(proceso)
    # Demandante principal.
    demandante = proceso.get("id_cliente") or ""
    cur.execute(
        "SELECT nombre FROM contactos WHERE identificacion=%s LIMIT 1",
        (str(demandante).split("|")[0].strip(),),
    )
    row = cur.fetchone()
    proceso["demandante_db"] = row[0] if row else demandante

    # Demandados: consolidar preferentemente desde la relacion de litisconsorcio.
    ids = []
    nombres = []
    if "procesos_litisconsorcio" in _tables(cur):
        cur.execute(
            "SELECT DISTINCT TRIM(pl.identificacion_demandado) AS ident, TRIM(c.nombre) AS nombre "
            "FROM procesos_litisconsorcio pl "
            "LEFT JOIN contactos c ON c.identificacion=pl.identificacion_demandado "
            "WHERE pl.radicado_interno=%s AND COALESCE(TRIM(pl.identificacion_demandado),'')<>'' "
            "ORDER BY ident",
            (proceso.get("radicado_interno"),),
        )
        for row in cur.fetchall():
            ident, nombre = row
            if ident not in ids:
                ids.append(ident)
                nombres.append(nombre or ident)
    if not ids:
        ids, nombres = _dedupe_pairs(proceso.get("id_demandado", ""), proceso.get("demandado", ""))
    proceso["id_demandado"] = " | ".join(ids)
    proceso["demandado"] = " | ".join(nombres)

    # Actuaciones.
    actuaciones = []
    if "actuaciones" in _tables(cur):
        cur.execute(
            "SELECT * FROM actuaciones WHERE radicado_interno=%s ORDER BY fecha DESC, id DESC",
            (proceso.get("radicado_interno"),),
        )
        actuaciones = [dict(r) for r in cur.fetchall()]

    # Medidas relacionadas, si hay tabla especifica; de lo contrario se usa el campo del proceso.
    medidas_detalle = []
    for table in ("medidas_cautelares", "medidas"):
        if table in _tables(cur):
            cols = _cols(cur, table)
            if "radicado_interno" in cols:
                select = [c for c in ("id", "tipo", "descripcion", "estado", "fecha", "inmueble", "identificacion") if c in cols]
                if select:
                    cur.execute(
                        f"SELECT {', '.join(select)} FROM {table} WHERE radicado_interno=%s ORDER BY "
                        + ("fecha DESC" if "fecha" in cols else "id DESC" if "id" in cols else "1"),
                        (proceso.get("radicado_interno"),),
                    )
                    medidas_detalle = [dict(r) for r in cur.fetchall()]
                    break
    abogados = []
    if "abogados" in _tables(cur):
        cur.execute("SELECT id,nombre FROM abogados ORDER BY nombre")
        abogados = [dict(r) for r in cur.fetchall()]
    return proceso, actuaciones, medidas_detalle, abogados


def _tables(cur):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    return {r[0] for r in cur.fetchall()}


def expediente_editor(request: Request, radicado: str):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            proceso = _lookup_proceso(cur, radicado)
            if not proceso:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="Expediente no encontrado")
            proceso, actuaciones, medidas_detalle, abogados = _build_context(cur, proceso)
            return templates.TemplateResponse(
                request=request,
                name="detalle_expediente.html",
                context={
                    "request": request,
                    "proceso": proceso,
                    "actuaciones": actuaciones,
                    "medidas_detalle": medidas_detalle,
                    "abogados": abogados,
                },
            )
    finally:
        _release(conn)


@app.post("/expediente/editar")
def editar_expediente(
    radicado_interno: str = Form(...),
    radicado_rama: str = Form(""),
    naturaleza: str = Form(""),
    juzgado: str = Form(""),
    etapa_actual: str = Form(""),
    estado: str = Form("Activo"),
    pretensiones: str = Form("0"),
    medidas_cautelares: str = Form(""),
    abogado_id: str = Form(""),
    demandados_nombres: str = Form(""),
    demandados_ids: str = Form(""),
):
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cols = _cols(cur, "procesos")
                if not cols:
                    raise RuntimeError("No existe la tabla procesos")
                try:
                    pret = Decimal(str(pretensiones).replace(".", ".").replace(",", ""))
                except (InvalidOperation, ValueError):
                    pret = Decimal("0")
                ids, names = _dedupe_pairs(demandados_ids, demandados_nombres)
                updates = {
                    "radicado_rama": radicado_rama.strip(),
                    "naturaleza": naturaleza.strip(),
                    "juzgado": juzgado.strip(),
                    "etapa_actual": etapa_actual.strip(),
                    "estado": estado.strip() or "Activo",
                    "pretensiones": pret,
                    "medidas_cautelares": medidas_cautelares.strip(),
                    "abogado_id": int(abogado_id) if abogado_id.strip().isdigit() else None,
                    "id_demandado": " | ".join(ids),
                    "demandado": " | ".join(names),
                }
                usable = [k for k in updates if k in cols]
                if not usable:
                    raise RuntimeError("No hay campos editables disponibles")
                cur.execute(
                    f"UPDATE procesos SET {', '.join(f'{k}=%s' for k in usable)} WHERE radicado_interno=%s",
                    [updates[k] for k in usable] + [radicado_interno],
                )

                # Normalizar litisconsorcio para eliminar duplicados persistentes.
                if "procesos_litisconsorcio" in _tables(cur):
                    lcols = _cols(cur, "procesos_litisconsorcio")
                    if "radicado_interno" in lcols and "identificacion_demandado" in lcols:
                        cur.execute("DELETE FROM procesos_litisconsorcio WHERE radicado_interno=%s", (radicado_interno,))
                        for ident in ids:
                            cur.execute(
                                "INSERT INTO procesos_litisconsorcio (radicado_interno, identificacion_demandado) VALUES (%s,%s)",
                                (radicado_interno, ident),
                            )
        return RedirectResponse(url=f"/expediente/{radicado_interno}?mensaje=Expediente+actualizado", status_code=303)
    except Exception as exc:
        print(f"[EXPEDIENTES][EDICION] Error actualizando {radicado_interno}: {exc!r}", flush=True)
        return RedirectResponse(url=f"/expediente/{radicado_interno}?error=No+fue+posible+actualizar+el+expediente", status_code=303)
    finally:
        _release(conn)


# Reemplazar el handler existente cuando el módulo se carga al arranque.
for _route in getattr(app, "routes", []):
    if getattr(_route, "path", None) == "/expediente/{radicado}" and getattr(_route, "methods", None):
        _route.endpoint = expediente_editor
        _route.dependant.call = expediente_editor
        print("[EXPEDIENTE] Editor de expediente y deduplicacion activa", flush=True)
        break
else:
    app.get("/expediente/{radicado}", include_in_schema=False)(expediente_editor)
