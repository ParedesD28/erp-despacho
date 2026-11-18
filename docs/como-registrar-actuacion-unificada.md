# Cómo registrar una actuación (unificada)

La **memoria del expediente** junta lo que llega de la Rama y lo que registra el abogado. Un solo ingreso. No hay que copiar el mismo hecho dos veces.

## Quién espera

Entre una actuación y la siguiente se cuenta el tiempo:

- **Despacho** (`responsable` Juzgado): la firma ya hizo lo suyo y espera al juzgado.
- **Abogado** (`responsable` Parte): la pelota está en la firma (subsanar, notificar, cumplir un término).

Si hay duda, se pinta **Abogado**.

## Cómo registrar a mano

En la ficha del expediente jurídico:

1. **＋ Actuación**
2. Fecha del hecho
3. Etapa del catálogo (la más alta sigue mandando en `procesos.etapa_actual`)
4. Qué pasó, en una frase

Guardar. El sistema:

- escribe en `actuaciones` (como siempre)
- espeja en `actuaciones_proceso` (`es_manual`, `origen=manual`)
- calcula plazo, dueño y, si el término es de la Parte, un vencimiento en la agenda

No borre la tabla `actuaciones`. El sync de Rama sigue llenando `actuaciones_proceso` solo.

## Qué no hacer

- No vuelva a digitar un auto que ya trajo la Rama: ya está en la línea de tiempo.
- Prejurídico no tiene reloj ni actuaciones judiciales.
- Los días restantes del término solo se muestran cuando el dueño es la Parte.
