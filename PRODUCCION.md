# Checklist de produccion

## Variables obligatorias en Render

ERP:
- `DATABASE_URL`
- `ERP_SESSION_SECRET` — secreto aleatorio largo (mínimo 32 caracteres). Es obligatorio y no se usa como fallback ninguna otra variable.
- `LIQUIDADOR_API_KEY`
- `PUBLIC_BASE_URL` — URL pública del servicio ERP, por ejemplo `https://gestionjudicial.onrender.com`.

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

## Seguridad y arquitectura aplicadas

- La sesión web usa tokens HMAC versionados con expiración; la cookie no almacena el ID del usuario en texto plano.
- La cookie de sesión usa `HttpOnly`, `Secure`, `SameSite=Lax` y `Path=/`.
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
- `agent_supervision.py`: Panel y API de supervisión y toma de control humano para el agente de cobranza.
- `start.py`: Punto de entrada único para producción (`python start.py`).
