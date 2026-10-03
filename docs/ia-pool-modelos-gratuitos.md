# Pool de modelos IA gratuitos (estados de cuenta)

Cascada configurable para el fallback PDF sin texto nativo.

**Orden default:** `gemini → groq → openrouter → deepseek → anthropic`  
(Claude al final; solo se usan proveedores con API key.)

| Proveedor | Free | Entrada | Modelo default |
|---|---|---|---|
| Gemini | Sí | PDF nativo | `gemini-2.5-flash` |
| Groq | Sí | PDF→JPEG (máx. 3 imgs/req) | `qwen/qwen3.8-27b` |
| OpenRouter | Sí | PDF→JPEG | `openrouter/free` |
| DeepSeek | **No** (barato) | PDF→JPEG | `deepseek-flash` |
| Anthropic | No | PDF nativo | `claude-sonnet-5-5` |

```bash
# Render — mínimo recomendado
GEMINI_API_KEY=...
GROQ_API_KEY=...
OPENROUTER_API_KEY=...   # 3.er free
# ANTHROPIC_API_KEY=...  # último recurso
PDF_IA_PROVIDERS=gemini,groq,openrouter,deepseek,anthropic
```

- COLON nativo = parser local (nunca IA).
- Bolsa Global / Art. 1653 = nunca IA.
- Keys solo en env, nunca en repo.

Detalle (inventario Claude, cuotas, limitaciones Groq/DeepSeek): ver doc del store del proyecto `docs/ia-pool-modelos-gratuitos.md`.
