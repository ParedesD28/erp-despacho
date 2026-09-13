# Checklist de produccion

## Variables obligatorias en Render

ERP:
- `DATABASE_URL`
- `ERP_SESSION_SECRET` — secreto aleatorio largo, no reutilizar contrasenas ni claves publicas.
- `LIQUIDADOR_API_KEY`
- `PUBLIC_BASE_URL` — URL publica del servicio ERP, por ejemplo `https://gestionjudicial.onrender.com`.

Agente:
- `DATABASE_URL`
- `ANTHROPIC_API_KEY`
- `TOKEN_META`
- `TOKEN_VERIFICACION`
- `ID_NUMERO_TELEFONO`
- `LIQUIDADOR_API_URL` — base del ERP, sin `/api/bot/liquidar`.
- `LIQUIDADOR_API_KEY` — exactamente el mismo secreto configurado en el ERP.
- `META_APP_SECRET` — recomendado y necesario para validar la firma de Meta cuando el despliegue este listo para produccion.

## Comandos

ERP Start Command:

```text
python start.py
```

El arranque endurece la sesion del ERP, mantiene libre solo el endpoint machine-to-machine del liquidador y expone `/health` para health checks.

## Seguridad aplicada

- La sesion web ya no acepta un simple ID de usuario: `start.py` exige un token HMAC con expiracion.
- La cookie de sesion usa `HttpOnly`, `Secure` y `SameSite=Lax`.
- Las contrasenas del ERP se aceptan unicamente como hashes bcrypt; no se admite fallback a texto plano.
- El endpoint del agente usa `X-API-Key` y comparacion en tiempo constante.
- La fecha de corte del liquidador no puede ser futura.
- Los PDFs generados por el bot usan nombres criptograficamente aleatorios en lugar de incluir el `inmueble_id` en la URL.
- La creacion de expedientes valida identificaciones, correspondencia entre cedulas/nombres y evita repetir un `radicado_interno` ya existente.
- Se agregan headers HTTP basicos contra MIME sniffing, framing y fuga innecesaria de referrer.

## Importante sobre la base de datos

Este repositorio no tiene acceso directo a las restricciones reales de Neon. Antes de declarar el esquema 100% cerrado, conviene verificar en Neon que existan las claves foraneas, `UNIQUE` e indices apropiados para `contactos.identificacion`, `procesos.radicado_interno`, `procesos.inmueble_id`, `procesos_litisconsorcio.radicado_interno` y las tablas de auditoria/CRM.

El motor financiero no se modifica en este endurecimiento: sigue siendo la unica fuente de verdad para la liquidacion.
