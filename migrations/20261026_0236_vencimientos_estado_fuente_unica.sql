-- Vencimientos: `estado` es la única columna escribible; `completado` pasa a ser
-- derivada (GENERATED ALWAYS ... STORED) y un CHECK limita el vocabulario de estado.
-- Tras esto estado y completado no pueden contradecirse.
--
-- Requiere haber ejecutado antes 20261026_0235_vencimientos_reconciliar_estado.
-- Si aún hay filas contradictorias o estados fuera de vocabulario, ABORTA sin
-- cambios (no descarta datos). Idempotente: si ya es derivada, no hace nada.
--
-- Orden de despliegue: código nuevo (no escribe `completado`) -> 0235 -> esta.
-- El código anterior fallaría contra este esquema (INSERT/UPDATE de completado).

DO $$
DECLARE
    v_generada boolean;
    n_malas    integer;
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

    IF NOT v_generada THEN
        SELECT COUNT(*) INTO n_malas
          FROM vencimientos
         WHERE estado IS NULL
            OR estado NOT IN ('Pendiente', 'Completado')
            OR completado IS DISTINCT FROM (estado = 'Completado');
        IF n_malas > 0 THEN
            RAISE EXCEPTION
                'Hay % vencimientos con estado/completado inconsistentes; ejecutar antes 20261026_0235_vencimientos_reconciliar_estado',
                n_malas;
        END IF;

        DROP INDEX IF EXISTS idx_vencimientos_fecha;
        ALTER TABLE vencimientos DROP COLUMN completado;
        ALTER TABLE vencimientos
            ADD COLUMN completado boolean
            GENERATED ALWAYS AS (estado = 'Completado') STORED NOT NULL;
    END IF;

    ALTER TABLE vencimientos ALTER COLUMN estado SET DEFAULT 'Pendiente';
    ALTER TABLE vencimientos ALTER COLUMN estado SET NOT NULL;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'vencimientos'::regclass
           AND conname = 'ck_vencimientos_estado'
    ) THEN
        ALTER TABLE vencimientos
            ADD CONSTRAINT ck_vencimientos_estado
            CHECK (estado IN ('Pendiente', 'Completado'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_vencimientos_fecha
    ON vencimientos (fecha_vencimiento, completado);

INSERT INTO schema_migrations(version)
VALUES ('20261026_0236_vencimientos_estado_fuente_unica')
ON CONFLICT DO NOTHING;
