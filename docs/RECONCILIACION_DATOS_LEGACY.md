# Reconciliación de datos legacy — Fase 6

Fecha de auditoría: 2026-09-18

## Registros no vinculables automáticamente

Producción contiene exactamente tres acuerdos_pago con obligacion_id IS NULL:

| Acuerdo | Identificación | Nombre | Valor | Fecha | Motivo |
|---:|---|---|---:|---|---|
| 1 | 1 | asq | 400 | 2026-09-15 | Sin inmueble, proceso ni obligación |
| 2 | tert | erter | 333 | 2026-09-15 | Sin inmueble, proceso ni obligación |
| 3 | 42151328 | NADIA BIBIANA BAÑOL GUARUMO | 666 | 2026-09-24 | Sin inmueble, proceso ni obligación |

Los registros CRM 5, 6 y 7 corresponden a esos mismos acuerdos.

Los vencimientos 8, 9 y 11 también corresponden a esos mismos acuerdos y usan radicado_interno='ACUERDO-PAGO'.

La identificación 42151328 sí existe en contactos, pero actualmente no tiene una obligación activa asociada; por tanto tampoco existe una relación inequívoca para reconstruir el acuerdo #3.

## Decisión de migración

No se debe asignar artificialmente obligacion_id, inmueble_id o radicado_interno a estos nueve registros.

Tampoco se deben eliminar en esta fase. Permanecen conservados como histórico/legacy hasta que exista una decisión explícita sobre su depuración.

## Estado actual de vínculos

- sms_cola_envios sin obligación: 0.
- recaudos_contabilidad sin obligación: 0.
- gestiones_crm sin obligación: 3.
- acuerdos_pago sin obligación: 3.
- vencimientos sin obligación: 3.

El lote de 3 + 3 + 3 representa una única fuente legacy: los acuerdos manuales antiguos.