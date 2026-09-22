# FASES 11 Y 12 — VALIDACIÓN SMS / WHATSAPP / PDF + PRUEBAS INTEGRALES

## Fase 11 — SMS / WhatsApp / PDF

Estado: IMPLEMENTADA EN LA RAMA V2. No fusionada a `main` ni desplegada a Render.

### SMS

Se consolidó el flujo:

`selección UI → revalidación servidor → cálculo de saldo → cola → worker Android → reporte de resultado → auditoría CRM`

Cambios relevantes:

- `POST /sms/wizard/confirmar-cola` quedó implementado; el navegador no puede imponer por sí mismo saldo ni elegibilidad.
- El servidor reconstruye los candidatos, recalcula el saldo mediante `obligacion_saldo_service.calcular_saldo_obligacion()` y rechaza envíos con saldo no verificable.
- La confirmación exige `obligacion_id` indirectamente a través del candidato financiero y lo persiste en `sms_cola_envios`.
- Los candidatos de procesos requieren relación canónica `obligacion_partes(rol='DEUDOR')`; no basta con aparecer como DEMANDADO en `proceso_partes`.
- El worker Android continúa siendo el componente que realiza el envío físico desde la SIM. El ERP solo administra la cola.
- Se conserva el bloqueo de reenvío al mismo contacto durante 24 horas y el control de reclamos exclusivos por `FOR UPDATE SKIP LOCKED`.
- Se eliminaron dependencias de overrides inexistentes (`sms_candidates_override.py` / `sms_wizard_override.py`) del arranque; la integración canónica se instala desde `start.py`.
- La interfaz ya no presenta `pretensiones` como posible fuente de saldo.

### Horario SMS / Ley 2300

El validador incluye:

- lunes a viernes: 7:00 a. m. a 7:00 p. m.;
- sábado: 8:00 a. m. a 3:00 p. m.;
- bloqueo de domingos;
- bloqueo de días festivos colombianos, incluyendo los festivos trasladados al lunes y los derivados de Semana Santa.

La implementación del calendario se basa en el esquema de festivos de la legislación colombiana. Esto no significa que el ERP modele por sí solo todos los requisitos materiales de autorización de contacto o límites multicanal: permanece una validación funcional pendiente para escenarios SMS + WhatsApp sobre el mismo deudor.

### WhatsApp / Bot

El endpoint `/api/bot/liquidar`:

- exige `X-API-Key`;
- recibe o resuelve una obligación PH concreta;
- pasa `obligacion_id` al liquidador;
- no utiliza `procesos.pretensiones` como saldo;
- genera el PDF mediante el generador existente.

Los acuerdos del bot validan la identificación reportante contra `obligacion_partes` con rol `DEUDOR`.

### PDF

`exportaciones.generar_pdf_liquidacion()` genera archivos en `private_pdfs/` (fuera de StaticFiles).

El acceso se realiza mediante:

- URL firmada HMAC `/api/bot/pdf/...` (bot);
- ruta autenticada por sesión `/pdfs/{filename}` (UI / paz y salvo);

con:

- expiración (enlace firmado);
- validación de firma;
- rechazo de traversal mediante `Path(filename).name`;
- acceso restringido al directorio privado `private_pdfs`.

`/static/pdfs/` no es ruta pública (middleware).

No se reinventó el pipeline PDF existente.

---

## Fase 12 — Pruebas integrales

Estado: CERRADA TÉCNICAMENTE. No hay release de producción todavía.

### CI

Se añadió una suite `tests/test_fase11_12.py` y el workflow de GitHub ahora:

1. instala `requirements.txt`;
2. ejecuta `compileall`;
3. ejecuta `unittest discover -s tests -p "test_*.py" -v`.

Primer fallo de Fase 12: el workflow no instalaba dependencias; el fallo fue `ModuleNotFoundError: No module named 'fastapi'`. Se corrigió el workflow agregando instalación de `requirements.txt`.

Segundo fallo de Fase 12: dos regresiones de la propia prueba:
- un guardia textual detectaba la palabra `pretensiones` dentro de un comentario/documentación;
- la prueba de traversal del PDF no había configurado el secreto HMAC y alcanzaba primero la respuesta 503.

Ambos errores de prueba fueron corregidos en el código de tests. El workflow posterior a estas correcciones quedó verde en GitHub Actions: run #368.

### Pruebas incluidas

La suite cubre:

- normalización de teléfonos colombianos;
- calendario de festivos;
- saldo genérico por capital + movimientos;
- contrato del wizard SMS;
- firma HMAC del PDF;
- bloqueo de traversal;
- rechazo de fecha de corte futura.

### Validación SQL en Neon

Se ejecutaron las invariantes en los branches `production` y `erp-refactor-proceso-obligacion`.

Resultado: 0 fallos en todas las invariantes estructurales y financieras, excepto una compatibilidad histórica de SMS explicada abajo.

Comprobaciones en 0:

- proceso sin `proceso_partes`;
- más de un principal por rol;
- persona con dos roles en el mismo proceso;
- más de un principal de obligación;
- obligación huérfana;
- VERBAL con obligación;
- PREJURIDICO con datos judiciales;
- expensa sin obligación;
- expensa con referencia inexistente;
- CRM/acuerdo/vencimiento/recaudo con obligación inexistente;
- SMS con obligación inexistente;
- SMS pendiente/proceso sin saldo verificado;
- falta de tasa SFC validada para 2026-09.

Tasa verificada en production para 2026-09:

`modalidad = Consumo y ordinario`

`IBC E.A. = 0.1949`

`tasa_usura_ea = 0.29235`

`validado_sfc = true`

### Compatibilidad histórica SMS

Hay 9 filas históricas de `sms_cola_envios` que no encuentran al mismo contacto en `obligacion_partes` con rol DEUDOR.

Son registros anteriores al modelo canónico; varios ya están ENVIADO y uno CANCELADO.

No se fabricaron relaciones nuevas ni se alteró el histórico. El comportamiento actual sí exige la relación canónica para nuevos envíos.

### Datos SMS de producción

Al momento de la validación:

- `sms_cola_envios = 52`;
- `sms_plantillas = 3`;
- `gestiones_crm` con marcador SMS = 25;
- 45 ENVIADO;
- 6 CANCELADO;
- 1 FALLIDO;
- 0 filas PENDIENTE/EN_PROCESO con saldo no verificado;
- 0 referencias SMS a obligaciones inexistentes.

Los SMS históricos presentan `saldo_fuente`/ `saldo_verificado` sin poblar porque esas columnas fueron incorporadas posteriormente; no se interpretan como evidencia de que el flujo actual permita enviar sin saldo verificado.

---

## Estado GitHub

`main`:

`93eca39475ab96d0259e4ca501041ccb3db02f6f`

Rama v2 actual al documentar:

`773224a24d282177092cbc0a1a0666944cebfc7b`

Comparación contra `main`: 83 commits adelante, 0 atrás.

PR #2 permanece abierto y en borrador. No fusionar todavía.

---

## Estado Neon

Producción:

`br-purple-waterfall-axjfbhds`

Técnica:

`br-silent-lake-axmdu916`

No se hicieron cambios DDL ni correcciones de datos en production durante estas fases.

---

## Pendientes para Fase 13

Antes de pasar a la transición productiva:

1. Mantener el run #368 como referencia CI verde del HEAD documentado.
2. Ejecutar smoke test real del wizard SMS en ambiente técnico con una obligación existente, sin enviar desde SIM.
3. Ejecutar smoke test del endpoint WhatsApp `/api/bot/liquidar` y del enlace PDF firmado en ambiente técnico.
4. Ejecutar al menos una prueba de obligación genérica no PH; production actualmente no tiene obligaciones genéricas históricas, por lo que esa ruta aún no está ejercitada con datos reales.
5. Definir la regla de contacto multicanal SMS + WhatsApp de Ley 2300 y su persistencia, si el negocio requiere control semanal por todos los canales.
6. Preparar release acumulativo de Fases 8–12 para Fase 13. No fusionar ni desplegar antes de esa transición.

## Regla de release

Fases 11 y 12 permanecen implementadas exclusivamente en `refactor/proceso-obligacion-v2`.

La transición de producción de código v2 sigue reservada para Fase 13.
