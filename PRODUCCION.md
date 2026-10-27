# Checklist de produccion

## Variables obligatorias en Render

ERP:
- `DATABASE_URL`
- `ERP_SESSION_SECRET` — secreto aleatorio largo (mínimo 32 caracteres). Es obligatorio y no se usa como fallback ninguna otra variable.
- `BOT_API_KEY`, `RECAUDOS_API_KEY`, `BOT_PDF_PUBLIC_SECRET`, `AGENT_SUPERVISION_KEY` — llaves M2M independientes (una por propósito). Ver runbook de llaves separadas.
- `GROK_BOT_API_KEY` — llave de "Grok Bot" (opcional, distinta de las demás; generar con `python -c "import secrets;print(secrets.token_urlsafe(48))"`). Se acepta **además** de las llaves actuales en `/api/bot/*` y `/api/recaudos/bot/*` (con escritura: liquidar, acuerdo, abono) y es la única que abre `/api/grok/*` (solo lectura). Nunca acepta la llave heredada. No abre administración de usuarios/perfiles/permisos, llaves, sesiones, `purgar-todos`, `/sms/api/`, `/api/v1/cron/` ni configuración. Si falta, `/api/grok/*` responde 503 y nada cambia para el bot de cobranza. Límite propio: `RATE_LIMIT_GROK_PER_MIN` (120/min en total). Cada llamada con esta llave deja un log `m2m_audit` (consumidor `grok`, método, ruta, estado, IP; sin cuerpo ni llaves). Revocar: borrar la variable en Render.
- `LIQUIDADOR_API_KEY` — llave compartida HEREDADA; solo durante la transición (el ERP la acepta junto a las nuevas). Retirarla borrándola o con `M2M_LEGACY_KEY_ENABLED=false`.
- `DRIVE_TOKEN_KEY` — llave propia (>= 32 caracteres) que cifra el refresh token de Drive en la base. Recomendada: permite rotar `ERP_SESSION_SECRET` sin reconectar Drive. Si falta, se usa la derivada de `ERP_SESSION_SECRET` (descifra con ambas y re-cifra con la propia al leer).
- `PUBLIC_BASE_URL` — URL pública del servicio ERP, por ejemplo `https://gestionjudicial.onrender.com`.
- `GEMINI_API_KEY` — recomendada; primer proveedor gratis del pool PDF (visión nativa) en `/herramientas/estado-cuenta` cuando no hay texto seleccionable. Key: https://aistudio.google.com/apikey.
- `GROQ_API_KEY` — opcional gratis; visión por imágenes (PDF→JPEG). https://console.groq.com/
- `OPENROUTER_API_KEY` — opcional gratis; router `openrouter/free` con visión. https://openrouter.ai/
- `DEEPSEEK_API_KEY` — opcional barata (NO free tier). https://platform.deepseek.com/
- `ANTHROPIC_API_KEY` — opcional; Claude al final del pool. Misma ruta de fallback PDF.
- `PDF_IA_PROVIDERS` — opcional; cascada comma, ej. `gemini,groq,openrouter,deepseek,anthropic`. Sin setear: auto con las keys presentes (Claude último).
- `PDF_IA_PROVIDER` — opcional; fuerza UN proveedor (compat). Preferir `PDF_IA_PROVIDERS`.
- Ver `.env.example`, `ESTADO_CUENTA_IA_FALLBACK`, docs `ia-pool-modelos-gratuitos.md`.

Agente:
- `DATABASE_URL`
- `ANTHROPIC_API_KEY`
- `TOKEN_META`
- `TOKEN_VERIFICACION`
- `ID_NUMERO_TELEFONO`
- `LIQUIDADOR_API_URL` — base del ERP, sin `/api/bot/liquidar`.
- `LIQUIDADOR_API_KEY` — exactamente el mismo secreto configurado en el ERP.
- `META_APP_SECRET` — necesario para validar la firma de Meta en producción.

## Comandos

ERP Start Command:

```text
python start.py
```

## Migraciones de base de datos

- Las migraciones viven en `migrations/*.sql` y se registran en `schema_migrations` (versión = nombre del archivo sin `.sql`).
- `python migrate.py status | check | apply [--dry-run]` — `apply` toma un advisory lock y ejecuta cada archivo en una transacción.
- `start.py` verifica las migraciones antes de abrir tráfico (`MIGRATIONS_MODE=check` por defecto): si hay pendientes, el arranque aborta y Render conserva la versión anterior.
  `MIGRATIONS_MODE=apply` hace que el arranque las aplique (con lock); `off` omite la guardia.
- Flujo para cambios de esquema: `python migrate.py apply --dry-run` y luego `apply` contra Neon ANTES de hacer merge.
- Migraciones nuevas: nombre `YYYYMMDD_descripcion.sql`, solo DDL/DML idempotente, sin `BEGIN/COMMIT`. Las anteriores a `20261006_usuario_conjuntos_portal_ph` son legacy (sustituidas por `migrations/baseline/`).
- CI aplica todo sobre una BD vacía dos veces y corre las pruebas con PostgreSQL real.

## Seguridad y arquitectura aplicadas

- La sesión web usa tokens HMAC `v2` (usuario, emisión, expiración y `sid` aleatorio) con verificación de firma en tiempo constante. Vigencia absoluta `ERP_SESSION_TTL` (por defecto 8 h, acotada entre 5 min y 24 h). Los tokens `v1` ya no se aceptan.
- La cookie de sesión usa `HttpOnly`, `Secure`, `SameSite=Lax` y `Path=/`. (`Strict` rompería el retorno del OAuth de Google Drive y los enlaces externos al ERP.)
- Cada login genera un `sid` nuevo y revoca la cookie previa; logout y re-login registran el `sid` en la tabla `sesiones_revocadas` (se crea sola con `CREATE TABLE IF NOT EXISTS` en el primer uso y en el arranque; no requiere migración previa, así que no activa la guardia de migraciones). Si Neon no responde, el ERP contesta 503 (no acepta sesiones sin verificar). Rotar `ERP_SESSION_SECRET` solo obliga a iniciar sesión una vez más.
- RBAC default-deny: toda ruta debe estar en `permisos.py` (pública o con permiso en `_ROUTE_RULES`); lo no clasificado se deniega a todos los perfiles. `tests/test_auth_hardening.py` falla si se añade una ruta sin clasificar.
- Fail-closed: usuario inexistente, inactivo o con perfil ausente/desconocido queda bloqueado (nunca ADMIN); un fallo de BD produce 503, no privilegios.
- Rate limiting en memoria del proceso (instancia única; con varias instancias cada una cuenta por separado y habría que moverlo a Neon): por IP `RATE_LIMIT_GLOBAL_PER_MIN` (600), endpoints con API key `RATE_LIMIT_API_PER_MIN` (240) y bloqueo tras `RATE_LIMIT_API_FAILS` (10) respuestas 401/403; login: fallos por IP (20), por usuario (10) y por IP+usuario (5) en 15 min con bloqueo de 60 s que se duplica hasta 15 min. Los contadores se reinician al reiniciar el servicio.
- IP del cliente: se usa `CF-Connecting-IP` (lo fija Cloudflare, delante de Render) solo si la conexión directa viene de una IP privada (el proxy). `X-Forwarded-For` no se usa salvo que se defina `ERP_TRUSTED_PROXY_COUNT` (nº de proxies de confianza contando desde la derecha). No definir `FORWARDED_ALLOW_IPS=*` en uvicorn.
- CSRF: las peticiones POST/PUT/PATCH/DELETE autenticadas (y `/login`) validan `Origin`/`Referer`/`Sec-Fetch-Site` contra el host del ERP (`PUBLIC_BASE_URL`, `ERP_ALLOWED_ORIGINS` como CSV adicional). No hay token por formulario (diferido: requiere tocar todas las plantillas y fetch de JS; se recomienda junto con CSP).
- `ERP_SESSION_SECRET` es obligatorio; no se deriva de `DATABASE_URL` ni de claves de otros servicios.
- Las contraseñas del ERP se verifican únicamente con bcrypt. En el arranque, cualquier valor heredado que no sea bcrypt se transforma a bcrypt dentro de una transacción controlada.
- Todo acceso a PostgreSQL pasa por `ThreadedConnectionPool`. Se reemplaza `psycopg2.connect()` por un proxy que devuelve la conexión al pool al cerrar o salir de un contexto.
- El pool histórico de `main.py` se cierra durante el arranque para evitar dos pools concurrentes.
- La extensión del ERP se registra desde `extensions.py`; se elimina el parche implícito por `sitecustomize.py`.
- La plantilla del expediente queda consolidada en una única vista definitiva: `templates/detalle_expediente_v4.html`.
- La apertura de `/liquidador?inmueble_id=...` conserva la causación automática intencional de expensas/cuotas hasta la fecha de corte, para mantener actualizado el registro real de administración. La interfaz permite posteriormente corregir los datos causados cuando exista un error.
- Los errores globales se registran como eventos JSON con `request_id` y traza en los logs del servidor; el usuario recibe únicamente mensajes genéricos sin SQL, rutas internas ni secretos.
- El endpoint machine-to-machine del agente continúa protegido mediante `X-API-Key` y permanece excluido del middleware de sesión web.
- Los PDFs generados para el bot utilizan URLs públicas temporales firmadas.

## Validaciones de despliegue

Antes de declarar producción estable, comprobar:
1. `GET /login` devuelve la página de acceso sin crear sesión.
2. Un login correcto devuelve `Set-Cookie` con `HttpOnly; Secure; SameSite=Lax` y un valor firmado `v1.*`.
3. Una cookie antigua que contenga solo un ID ya no autoriza ninguna ruta.
4. `/api/bot/liquidar` sigue respondiendo con `X-API-Key` sin exigir cookie del navegador.
5. `GET /liquidador?inmueble_id=...` conserva la causación automática y actualiza las expensas hasta la fecha de corte sin duplicar los periodos ya existentes.
6. `POST /liquidador` y `POST /liquidador/actualizar` conservan el cálculo y la actualización explícita.
7. Una excepción inesperada genera un `request_id` y una traza en el log, sin devolver el detalle técnico al navegador.

## Importante sobre la base de datos

Este repositorio no tiene acceso directo a las restricciones reales de Neon. Conviene verificar en Neon que existan las claves foráneas, `UNIQUE` e índices apropiados para `contactos.identificacion`, `procesos.radicado_interno`, `procesos.inmueble_id`, `procesos_litisconsorcio.radicado_interno` y las tablas de auditoría/CRM.

El motor financiero no se modifica en este endurecimiento: sigue siendo la fuente de verdad para la liquidación.

## Arquitectura Limpia Consolidada (Producción v2.0)

Se eliminaron 21 archivos de parches fragmentados y mono-dependientes que mutaban `main.py` en tiempo de arranque mediante `extensions.py`. Toda la funcionalidad fue absorbida en módulos limpios y cohesivos:
- `main.py`: Aplicación FastAPI unificada con todas las rutas y vistas nativas.
- `db.py`: Pool de conexiones `ThreadedConnectionPool` con interceptor transparente para `psycopg2.connect()`.
- `security.py`: Sesiones HMAC versionadas, hashing bcrypt y migración automática de credenciales.
- `observability.py`: Logging estructurado JSON y manejador global de excepciones sin fugas de datos sensibles.
- `tasas.py`: Descarga y sincronización con API oficial SFC (Superfinanciera) y caché validada en Neon.
- `liquidador.py`: Motor matemático judicial exacto, auto-causación hasta fecha de corte y deduplicación de expensas.
- `exportaciones.py`: Generador oficial ReportLab de PDF judicial en formato apaisado y reporte ejecutivo en Excel con OpenPyXL.
- `expedientes_service.py`: Deduplicación de partes/litisconsortes, trazabilidad/auditoría y cálculo automático determinista de etapas procesales.
- `bot_api.py`: Endpoint M2M protegido con API Key para el bot de WhatsApp y descarga segura con URLs temporales firmadas.
- `grok_api.py`: consultas de solo lectura `/api/grok/*` para Grok Bot (solo `GET`, transacciones `READ ONLY`, sin datos de usuarios del ERP ni secretos).
- `agent_supervision.py`: Panel y API de supervisión y toma de control humano para el agente de cobranza.
- `start.py`: Punto de entrada único para producción (`python start.py`).
