# Modelo IA del fallback PDF (estados de cuenta)

**Estado:** IMPLEMENTADO  
**Repo:** [ParedesD28/erp-despacho](https://github.com/ParedesD28/erp-despacho)  
**UI:** `/herramientas/estado-cuenta`  
**Relacionado:** diseño original Claude (doc del store del proyecto)

---

## Elección

| Criterio | Decisión |
|---|---|
| Proveedor default | **Google Gemini** (`PDF_IA_PROVIDER=gemini`) |
| Modelo default | **`gemini-2.5-flash`** (multimodal, PDF nativo, free tier) |
| Alternativa | Anthropic Claude (`PDF_IA_PROVIDER=anthropic`) — opcional |
| Descartados | Groq y similares: no ofrecen visión/PDF robusta para este OCR de estados de cuenta |

**Por qué Gemini Flash:** cuota free generosa en [Google AI Studio](https://aistudio.google.com/), acepta `application/pdf` inline, suficiente para extraer cabecera + movimientos FAC/RDC. Claude queda disponible si se prefiere calidad/pagado, pero no es el default.

## Límites free tier (orientativos)

Los cupos exactos cambian en la consola de Google; revisar [pricing](https://ai.google.dev/gemini-api/docs/pricing) y [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

- **Free tier:** tokens de entrada/salida sin cargo en modelos Flash elegibles; hay **RPM / RPD / TPM** por proyecto.
- Para este ERP: 1 PDF imagen a la vez (semáforo) → el cuello de botella suele ser RPD, no concurrencia.
- Si aparece 429 / `resource_exhausted`: reintentar más tarde o subir de tier en AI Studio.
- `gemini-2.0-flash` fue deprecado/apagado (2026); no usarlo. Override con `ESTADO_CUENTA_IA_MODEL` o `GEMINI_MODEL` si Google publica un Flash más nuevo (p. ej. 3.x).

## Variables de entorno (Render)

| Variable | Rol |
|---|---|
| `GEMINI_API_KEY` | **Recomendada.** Secret; nunca en repo. |
| `PDF_IA_PROVIDER` | `gemini` (default) \| `anthropic`. Sin setear: auto (Gemini si hay key, si no Anthropic). |
| `GEMINI_MODEL` | Opcional; default `gemini-2.5-flash`. |
| `ESTADO_CUENTA_IA_MODEL` | Override de modelo (cualquier proveedor). |
| `ESTADO_CUENTA_IA_FALLBACK` | `0` desactiva; con key del proveedor → ON por defecto. |
| `ESTADO_CUENTA_IA_TIMEOUT_S` | Timeout por PDF (default 120). |
| `ESTADO_CUENTA_IA_MAX_TOKENS` | Max salida (default 32000). |
| `ANTHROPIC_API_KEY` | Solo si se usa Claude. |
| `ANTHROPIC_MODEL` | Solo Claude; default `claude-sonnet-5-5`. |
| `ESTADO_CUENTA_IA_USE_TOOLS` | Solo Anthropic (opt-in tools). |

## Cómo obtener `GEMINI_API_KEY` gratis

1. Entrar a [Google AI Studio → API keys](https://aistudio.google.com/apikey).
2. Crear o seleccionar un proyecto de Google Cloud / AI Studio.
3. **Create API key** → copiar el valor.
4. En Render: servicio ERP → **Environment** → Add `GEMINI_API_KEY` = (secret) → Redeploy.
5. Local: `export GEMINI_API_KEY=...` (no commitear).

Opcional Claude: `PDF_IA_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`.

## Comportamiento (sin cambio de pipeline)

```
PDF → pypdf ¿texto?
  ├─ sí  → parser COLON local (nunca IA)
  └─ no  → ¿IA habilitada?
            ├─ sí → Gemini (o Claude) → JSON → validar → rows → Excel / Bolsa
            └─ no → error claro (re-export o configure key)
```

- Bolsa Global / Art. 1653 = **solo código**.
- Salida JSON idéntica (`cabecera` + `movimientos` → `rows`).
- `fuente_parseo=ia_vision`.

## Pruebas

```bash
pytest tests/test_estado_cuenta_ia_vision.py -q

# Smoke real (opcional)
export GEMINI_API_KEY=...
export ESTADO_CUENTA_IA_SMOKE=1
pytest tests/test_estado_cuenta_ia_vision.py::SmokeIa1204Tests -s
```
