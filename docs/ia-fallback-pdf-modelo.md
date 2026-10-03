# Modelo IA del fallback PDF (estados de cuenta)

**Estado:** IMPLEMENTADO (pool multi-proveedor)  
**Repo:** [ParedesD28/erp-despacho](https://github.com/ParedesD28/erp-despacho)  
**UI:** `/herramientas/estado-cuenta`  
**Doc canónica del pool:** ver store del proyecto → `docs/ia-pool-modelos-gratuitos.md`

---

## Elección

| Criterio | Decisión |
|---|---|
| Cascada default | **`gemini → groq → openrouter → deepseek → anthropic`** (solo los que tengan key) |
| Primero | **Gemini 2.5 Flash** — PDF nativo, free tier |
| Gratis adicionales | **Groq** (`qwen/qwen3.8-27b`, PDF→JPEG) y **OpenRouter** (`openrouter/free`) |
| Barato (no free) | **DeepSeek** (`deepseek-flash`, visión por imágenes) |
| Último | **Anthropic Claude** — opcional, de pago |

Env: `PDF_IA_PROVIDERS=gemini,groq,openrouter,deepseek,anthropic`  
Compat: `PDF_IA_PROVIDER=gemini` fuerza un solo proveedor.

## Variables de entorno (Render)

| Variable | Rol |
|---|---|
| `GEMINI_API_KEY` | Recomendada (gratis). |
| `GROQ_API_KEY` | Opcional gratis; visión por imágenes. |
| `OPENROUTER_API_KEY` | Opcional gratis; router free. |
| `DEEPSEEK_API_KEY` | Opcional barata (no free). |
| `ANTHROPIC_API_KEY` | Claude al final del pool. |
| `PDF_IA_PROVIDERS` | Cascada comma. |
| `PDF_IA_PROVIDER` | Un solo proveedor (compat). |
| `ESTADO_CUENTA_IA_FALLBACK` | `0` desactiva; con alguna key → ON. |
| `ESTADO_CUENTA_IA_TIMEOUT_S` | Timeout por PDF (default 120). |
| `ESTADO_CUENTA_IA_MAX_TOKENS` | Max salida (default 32000). |

## Comportamiento

```
PDF → pypdf ¿texto?
  ├─ sí  → parser COLON local (nunca IA)
  └─ no  → ¿IA habilitada?
            ├─ sí → pool en orden → JSON → validar → rows → Excel / Bolsa
            └─ no → error claro
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
