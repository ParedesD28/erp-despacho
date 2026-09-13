"""Parche final del workflow de expedientes: etapas y duplicados."""
import unicodedata
import expediente_workflow_patch as workflow

CANONICAL = set(workflow.CANONICAL_STAGES)


def _norm(value):
    text = str(value or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _safe_stage_from_act(etapa, descripcion, tipificacion):
    explicit = str(etapa or "").strip()
    if explicit in CANONICAL:
        return explicit
    n = _norm(" ".join(str(x or "") for x in (etapa, tipificacion, descripcion)))
    if any(k in n for k in ("terminacion del proceso", "archivo definitivo", "paz y salvo procesal")):
        return "Terminación del Proceso"
    if any(k in n for k in ("sentencia ejecutoriada", "sentencia", "fallo", "condena")):
        return "7. Sentencia"
    if "desistimiento tacito" in n:
        return "8. Desistimiento tácito"
    if any(k in n for k in ("excepcion", "excepciones", "contestacion a excepciones", "traslado de excepciones")):
        return "6. Excepciones"
    if any(k in n for k in ("notificacion", "notificacion personal", "citacion", "emplazamiento")):
        return "5. Notificación"
    if any(k in n for k in ("medida cautelar", "medidas cautelares", "embargo", "secuestro", "oficio de embargo", "libramiento de embargo")):
        return "4. Medidas Cautelares"
    # Inadmisión debe probarse antes de Admisión.
    if any(k in n for k in ("inadmis", "subsanacion")):
        return "2. Inadmisión"
    if any(k in n for k in ("admision", "auto admite", "mandamiento ejecutivo", "libra mandamiento", "solicitud de oficios")):
        return "3. Admisión"
    if any(k in n for k in ("presentacion de la demanda", "radicacion", "reparto", "reparto y radicacion", "inicio")):
        return "1. Presentación de la demanda"
    return None


def _safe_get_demandantes(cur, proceso):
    seen = set()
    out = []
    for row in workflow._get_demandantes_original(cur, proceso):
        ident = str(row.get("identificacion") or "").strip()
        if ident and ident not in seen:
            seen.add(ident)
            out.append(row)
    return out


def _safe_get_demandados(cur, radicado):
    seen = set()
    out = []
    for row in workflow._get_demandados_original(cur, radicado):
        ident = str(row.get("identificacion") or "").strip()
        if ident and ident not in seen:
            seen.add(ident)
            out.append(row)
    return out

# Conservar referencias originales para no crear recursión.
if not hasattr(workflow, "_stage_from_act_original"):
    workflow._stage_from_act_original = workflow._stage_from_act
if not hasattr(workflow, "_get_demandantes_original"):
    workflow._get_demandantes_original = workflow._get_demandantes
if not hasattr(workflow, "_get_demandados_original"):
    workflow._get_demandados_original = workflow._get_demandados

workflow._stage_from_act = _safe_stage_from_act
workflow._get_demandantes = _safe_get_demandantes
workflow._get_demandados = _safe_get_demandados

print("[EXPEDIENTE_STAGE_HARDENING] Etapas deterministas y partes deduplicadas", flush=True)
