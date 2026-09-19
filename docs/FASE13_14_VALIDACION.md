# FASES 13–14 — TRANSICIÓN PRODUCTIVA Y ELIMINACIÓN LEGACY

Fecha de cierre: 2026-09-19

## Fase 13 — transición productiva

Estado: CERRADA EN GITHUB Y NEON.

- PR #2 quedó fusionado en `main`.
- Commit de producción: `84d3e707ddbad495fd1e8da67c2f9bbda1d84eb1`.
- GitHub Actions en `main`: run #384, compilación + pruebas de regresión exitosas.
- Se incorporó control multicanal de Ley 2300: un contacto de cobranza registrado en los últimos 7 días bloquea un nuevo contacto SMS por otro canal.
- Se mantienen los controles de horario, domingos y festivos, además del bloqueo de reenvío SMS de 24 horas.
- El saldo comunicado sigue dependiendo de `obligacion_saldo_service` / liquidador y no de `procesos.pretensiones`.

La Ley 2300 de 2023 establece que, una vez establecido contacto directo, no se debe contactar al consumidor mediante varios canales dentro de la misma semana ni más de una vez en el mismo día, y fija los horarios y exclusión de domingos y festivos. Fuente normativa: Función Pública. 

## Fase 14 — eliminación legacy

Estado: CERRADA TÉCNICAMENTE Y APLICADA EN PRODUCCIÓN.

Se retiraron del código:

- `procesos.id_cliente`
- `procesos.demandante` si existía
- `procesos.id_demandado`
- `procesos.demandado`
- `procesos_litisconsorcio`
- `obligaciones.deudor_contacto_id`
- adaptadores runtime `proceso_partes_runtime.py` y `proceso_partes_service.py`

La migración `20260919_fase14_eliminar_legacy_proceso_obligacion.sql` fue probada primero en branch temporal y luego aplicada a `production`.

Verificación posterior:

- tabla legacy `procesos_litisconsorcio`: inexistente;
- columnas legacy de `procesos`: inexistentes;
- columna `obligaciones.deudor_contacto_id`: inexistente;
- índice legacy `idx_obligaciones_deudor_contacto`: inexistente;
- triggers legacy de sincronización: inexistentes;
- marcador `20260919_fase14_eliminar_legacy_proceso_obligacion`: presente;
- procesos sin `proceso_partes`: 0;
- obligaciones sin `obligacion_partes`: 0;
- VERBAL con obligación: 0.

Los datos canónicos no fueron recreados ni reemplazados. La migración elimina únicamente las estructuras duplicadas ya sustituidas.

## Estado final de GitHub

- `main`: `84d3e707ddbad495fd1e8da67c2f9bbda1d84eb1`
- PR #2: cerrado y fusionado.
- Rama `refactor/proceso-obligacion-v2`: conserva el historial del refactor.
- La suite CI de `main` quedó verde en run #384.

## Estado final de Neon

Producción: `br-purple-waterfall-axjfbhds`

Las invariantes canónicas se verificaron nuevamente después de la migración Fase 14.

## Render

No existe conector Render disponible en la cuenta actual, por lo que este documento confirma el merge a `main` y el CI de GitHub, pero no certifica desde API el estado visual del deployment de Render. El código de producción esperado corresponde al commit `84d3e707...`.
