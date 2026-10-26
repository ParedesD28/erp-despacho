-- Vencimientos: reconcilia filas donde `estado` y `completado` se contradicen.
--
-- *** DECISION PENDIENTE: NO EJECUTAR EN PRODUCCION HASTA QUE SE APRUEBE LA REGLA. ***
--
-- Regla (propuesta): `estado` GANA.
--   1) estado se canoniza: 'Completado' si UPPER(BTRIM(estado))='COMPLETADO';
--      cualquier otro valor (NULL, vacío, desconocido) pasa a 'Pendiente'.
--   2) completado se alinea con estado (completado := estado='Completado').
--   Por tanto:  (estado='Pendiente', completado=TRUE)  -> completado=FALSE
--               (estado='Completado', completado=FALSE) -> completado=TRUE
--   Ninguna fila cambia su `estado` salvo la canonización del paso 1.
--
-- Idempotente: una segunda ejecución no toca filas. Si `completado` ya es
-- columna generada (20261026_0236 aplicada) no hay nada que reconciliar y sale.
-- Orden de despliegue: código nuevo -> esta migración -> 20261026_0236.

DO $$
DECLARE
    v_generada boolean;
    n_estado   integer;
    n_a_pend   integer;
    n_a_comp   integer;
BEGIN
    SELECT is_generated = 'ALWAYS'
      INTO v_generada
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'vencimientos'
       AND column_name = 'completado';

    IF v_generada IS NULL THEN
        RAISE EXCEPTION 'vencimientos.completado no existe';
    END IF;
    IF v_generada THEN
        RAISE NOTICE 'vencimientos.completado ya es derivada; nada que reconciliar';
        RETURN;
    END IF;

    UPDATE vencimientos
       SET estado = CASE WHEN UPPER(BTRIM(COALESCE(estado, ''))) = 'COMPLETADO'
                         THEN 'Completado' ELSE 'Pendiente' END
     WHERE estado IS DISTINCT FROM
           CASE WHEN UPPER(BTRIM(COALESCE(estado, ''))) = 'COMPLETADO'
                THEN 'Completado' ELSE 'Pendiente' END;
    GET DIAGNOSTICS n_estado = ROW_COUNT;

    UPDATE vencimientos
       SET completado = FALSE
     WHERE completado AND estado <> 'Completado';
    GET DIAGNOSTICS n_a_pend = ROW_COUNT;

    UPDATE vencimientos
       SET completado = TRUE
     WHERE NOT completado AND estado = 'Completado';
    GET DIAGNOSTICS n_a_comp = ROW_COUNT;

    RAISE NOTICE 'vencimientos reconciliados: estado canonizado=%, completado->FALSE=%, completado->TRUE=%',
        n_estado, n_a_pend, n_a_comp;
END
$$;

INSERT INTO schema_migrations(version)
VALUES ('20261026_0235_vencimientos_reconciliar_estado')
ON CONFLICT DO NOTHING;
