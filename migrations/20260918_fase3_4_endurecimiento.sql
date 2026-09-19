-- FASE 4: endurecimiento de integridad Proceso + Obligación y eliminación de DDL runtime.
-- Aditiva/segura: no elimina datos ni tablas de negocio.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM schema_migrations
        WHERE version='20260918_proceso_obligacion_base'
    ) THEN
        RAISE EXCEPTION
            'Precondición incumplida: falta 20260918_proceso_obligacion_base.';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM proceso_obligaciones
        GROUP BY radicado_interno
        HAVING COUNT(*) FILTER (WHERE es_principal) > 1
    ) THEN
        RAISE EXCEPTION
            'Precondición incumplida: hay procesos con más de una obligación principal.';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM obligaciones
        WHERE capital_inicial IS NOT NULL
          AND capital_inicial < 0
    ) THEN
        RAISE EXCEPTION
            'Precondición incumplida: existen capitales iniciales negativos.';
    END IF;
END
$$;

-- 1. Elimina únicamente FKs duplicadas ya identificadas por auditoría.
ALTER TABLE inmuebles_ph
    DROP CONSTRAINT IF EXISTS inmuebles_ph_contacto_id_fkey;

ALTER TABLE expensas_ph
    DROP CONSTRAINT IF EXISTS expensas_ph_inmueble_id_fkey;

-- 2. Endurece el vínculo de obligación principal: máximo una principal
-- por expediente, sin impedir múltiples obligaciones secundarias.
CREATE UNIQUE INDEX IF NOT EXISTS uq_proceso_obligacion_principal
    ON proceso_obligaciones(radicado_interno)
    WHERE es_principal=TRUE;

-- 3. Capital inicial es monetariamente no negativo y por defecto cero
-- para obligaciones cuyo saldo proviene de otra fuente (p. ej. PH).
UPDATE obligaciones
SET capital_inicial=0
WHERE capital_inicial IS NULL;

ALTER TABLE obligaciones
    ALTER COLUMN capital_inicial SET DEFAULT 0,
    ADD CONSTRAINT ck_obligaciones_capital_inicial_no_negativo
    CHECK (capital_inicial >= 0);

INSERT INTO schema_migrations(version)
VALUES ('20260918_fase3_4_endurecimiento')
ON CONFLICT (version) DO NOTHING;

COMMIT;
