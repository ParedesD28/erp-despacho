# API de Grok Bot (`/api/grok/*`)

Consultas de **solo lectura** para "Grok Bot". Solo `GET`. Cabecera `X-API-Key: <GROK_BOT_API_KEY>`
(ninguna otra llave sirve aquí, ni la heredada). Sin llave o con llave mala: 401. Sin
`GROK_BOT_API_KEY` configurada: 503. Cada llamada cuenta para el límite propio de Grok
(`RATE_LIMIT_GROK_PER_MIN`, 120/min en total, 429 con `Retry-After`) y deja un log
`m2m_audit` (consumidor, método, ruta sin query, estado, IP; sin cuerpo ni llaves).

La misma llave abre también `/api/bot/*` y `/api/recaudos/bot/*` (con escritura, igual que el
bot de cobranza). No abre administración, sesiones, llaves, pantallas, `/sms/api/*` ni
`/api/v1/cron/*`. Operación y revocación: runbook `runbook-llave-grok-bot.md`.

Datos que devuelve: deudores, demandantes y propietarios (nombre, identificación, teléfono,
correo, dirección, ciudad), saldos, acuerdos, actuaciones. Nunca: contraseñas, hashes,
tokens, llaves ni datos de usuarios/abogados del ERP.

## Endpoints

| Ruta | Descripción |
|---|---|
| `/api/grok/ping` | Probar la llave |
| `/api/grok/procesos` | Lista de procesos (`estado`, `q`, `limit`, `offset`) |
| `/api/grok/procesos/{radicado}` | Detalle: partes, propietarios, actuaciones, obligaciones |
| `/api/grok/procesos/{radicado}/obligaciones` | Obligaciones del proceso |
| `/api/grok/obligaciones/{id}/saldo` | Saldo (`fecha_corte` opcional) |
| `/api/grok/vencimientos` | Vencimientos (`desde`, `hasta`, `radicado`, `incluir_completados`, `limit`) |
| `/api/grok/acuerdos` | Acuerdos de pago (`estado`, `inmueble_id`, `obligacion_id`, `limit`) |
| `/api/grok/conjuntos` | Conjuntos residenciales |
| `/api/grok/conjuntos/{id}/unidades` | Unidades de un conjunto |
| `/api/grok/liquidacion` | Liquidación calculada (JSON), sin autocausar |
| `/api/grok/liquidacion/pdf` | PDF de la liquidación (descarga directa), sin autocausar |
| `/api/grok/rama/consulta` | Detalle, actuaciones y sujetos en la Rama Judicial |
| `/api/grok/rama/novedades` | Actuaciones nuevas de los procesos activos en la Rama |

## Rama Judicial

Usan `LegalAPIClient` (`external_legal_client.py`), que se configura solo con variables de
entorno (`LEGAL_API_BASE_URL` obligatoria; `LEGAL_API_TIMEOUT_S`, `LEGAL_API_MAX_ATTEMPTS`,
etc.). El cliente reintenta timeouts y 502/503/504. **Nunca se devuelve ni se registra la
URL base ni el detalle de error del cliente**: la API responde solo mensajes cortos propios.
No escriben en la base de datos ni en la Rama (a diferencia de la sincronización diaria,
que sí guarda actuaciones y crea alertas).

### `GET /api/grok/rama/consulta?radicado=<23 dígitos | EXP-0001>[&limite_actuaciones=200]`

- Con 23 dígitos (se aceptan puntos/guiones/espacios) consulta directo. Con un radicado
  interno del ERP (p. ej. `EXP-0001`, sin distinguir mayúsculas) busca su `radicado_rama` en
  `procesos` (solo `SELECT`, transacción de solo lectura).
- Respuesta: `radicado`, `radicado_interno` (si venía EXP), `id_interno`, `detalle`,
  `total_actuaciones`, `actuaciones` (más recientes primero: `fecha_actuacion`, `actuacion`,
  `anotacion`, `inicio_termino`, `fin_termino`, `fecha_registro`, `id_actuacion`,
  `con_documentos`; fechas como AAAA-MM-DD) y `sujetos`.
- Errores: 404 si el EXP no existe o no tiene radicado de 23 dígitos, o si la Rama no
  conoce el radicado; 422 radicado inválido; 502 con mensaje corto si la Rama no responde o
  tarda más de `GROK_RAMA_TIMEOUT_S`; 503 si `LEGAL_API_BASE_URL` no está configurada.

### `GET /api/grok/rama/novedades?desde=AAAA-MM-DD&estado=ACTIVOS&limit=100&offset=0[&solo_no_registradas=true]`

- Toma los procesos con radicado de la Rama de 23 dígitos (`estado`: ACTIVOS, INACTIVOS,
  TODOS; paginado, `limit` máximo 100), consulta la Rama (2 llamadas por proceso) y devuelve,
  por proceso, solo las actuaciones **posteriores o iguales a `desde` o que el ERP aún no
  tiene registradas**. Con `solo_no_registradas=true` se exigen ambas condiciones (o solo
  "no registrada" si no se envía `desde`).
- "Ya registrada" = coincide por `id_actuacion` o hash en `actuaciones_proceso` (la tabla que
  llena la sincronización diaria) o, en `actuaciones` (registro manual), por fecha y texto.
- Cada actuación trae `registrada_en_erp` y `motivo` (`posterior_a_desde`, `no_registrada`).
  Cada proceso trae `radicado_interno`, `radicado_rama`, `conjunto`, `torre_apto`,
  `demandado`, `juzgado`.
- Control de carga: máximo `GROK_RAMA_CONCURRENCY` consultas simultáneas (3, tope 5), tope
  `GROK_RAMA_TIMEOUT_S` por consulta (30 s) y `GROK_RAMA_TOTAL_TIMEOUT_S` en total (100 s).
  Lo que falle o no alcance a consultarse va a la lista `errores`
  (`{radicado_interno, error}`) sin afectar el resto. Recomendado `limit` de 20–30 por llamada
  y paginar con `offset`.
- Respuesta: `desde`, `estado`, `total_procesos`, `limit`, `offset`, `consultados`,
  `con_novedades`, `novedades`, `errores`.
- Errores: 422 parámetros inválidos; 502 si falla la consulta completa (p. ej. no se pudo
  abrir el cliente); 503 si la Rama no está configurada.
