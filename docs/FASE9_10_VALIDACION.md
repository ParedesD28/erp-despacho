# Validación Fases 9 y 10 — ERP Despacho

## Fase 9 — Obligaciones / Liquidador

Implementación en `refactor/proceso-obligacion-v2`:
- El saldo genérico usa `capital_inicial + obligacion_movimientos`.
- El liquidador PH acepta `obligacion_id` y filtra `expensas_ph.obligation_id`.
- La autocausación, edición y carga masiva conservan `obligation_id`.
- PDF/Excel y bot utilizan la obligación concreta.
- Recaudo PH recalcula usando la obligación concreta.
- La deduplicación de expensas distingue `obligation_id`.

## Fase 10 — CRM / Recaudos / Acuerdos

- CRM nuevo permite guardar `obligacion_id` y valida pertenencia al proceso.
- CRM consulta `proceso_partes` y `proceso_obligaciones` como fuentes canónicas.
- Acuerdos aceptan cualquier deudor registrado en `obligacion_partes` con rol `DEUDOR`.
- Vencimientos y cuotas de acuerdos permanecen vinculados a la obligación.
- Recaudos PH se registran en `expensas_ph` + `recaudos_contabilidad`.
- Recaudos genéricos usan `obligacion_movimientos` + `recaudos_contabilidad`.
- El reporte de recaudo puede validar el deudor contra `obligacion_partes`.
- `schema_preflight.py` verifica tablas/columnas de agenda, acuerdos, recaudos y paz y salvo.

## Estado Neon validado

- obligaciones: 19
- obligaciones PH: 19
- obligaciones genéricas históricas: 0
- expensas PH: 430
- expensas sin obligation_id: 0
- expensas con referencia inexistente: 0
- CRM con obligación: 39
- CRM históricos sin obligación: 3
- acuerdos con obligación: 0
- acuerdos históricos sin obligación: 3
- vencimientos con obligación: 6
- vencimientos históricos sin obligación: 3
- SMS con obligación: 52
- SMS sin obligación: 0
- huérfanos entre consumidores y obligaciones: 0

Los 3 CRM, 3 acuerdos y 3 vencimientos sin obligación permanecen como excepciones históricas registradas en `data_migration_exceptions`. No se fabricaron obligaciones.

## Producción

Las Fases 9 y 10 permanecen en la rama de trabajo. No se hizo merge a `main` ni despliegue a Render. La transición acumulada sigue reservada para la Fase 13.
