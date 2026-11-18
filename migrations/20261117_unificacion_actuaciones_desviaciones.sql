-- Unificación actuaciones + plazos + desviaciones.
-- Extiende actuaciones_proceso (fuente de verdad), crea desviaciones_procesales,
-- backfill de actuaciones manuales y dual-write. No borra `actuaciones`.
-- Idempotente (IF NOT EXISTS / ON CONFLICT / CREATE OR REPLACE).

-- ---------------------------------------------------------------------------
-- 1. Columnas nuevas en actuaciones_proceso
-- ---------------------------------------------------------------------------
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS es_manual BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS etapa_asociada TEXT;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS codigo_plazo TEXT;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS responsable TEXT;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS termino_legal_dias INTEGER;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS termino_legal_unidad TEXT;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS fin_termino_esperado DATE;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS desviacion_dias INTEGER;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS desviacion_atribuible TEXT;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS justificacion TEXT;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS calculado_at TIMESTAMPTZ;
ALTER TABLE actuaciones_proceso ADD COLUMN IF NOT EXISTS origen TEXT;

UPDATE actuaciones_proceso
   SET origen = 'sync'
 WHERE origen IS NULL
   AND COALESCE(es_manual, FALSE) = FALSE;

UPDATE actuaciones_proceso
   SET origen = 'manual'
 WHERE origen IS NULL
   AND es_manual = TRUE;

ALTER TABLE actuaciones_proceso ALTER COLUMN origen SET DEFAULT 'sync';

DO $$
BEGIN
    ALTER TABLE actuaciones_proceso
        ADD CONSTRAINT ck_actuaciones_proceso_responsable
        CHECK (responsable IS NULL OR responsable IN ('Parte', 'Juzgado', 'Tercero', 'Mixto', 'Justificada'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE actuaciones_proceso
        ADD CONSTRAINT ck_actuaciones_proceso_origen
        CHECK (origen IS NULL OR origen IN ('sync', 'manual', 'migracion'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE actuaciones_proceso
        ADD CONSTRAINT ck_actuaciones_proceso_termino_unidad
        CHECK (
            termino_legal_unidad IS NULL
            OR termino_legal_unidad IN ('dias_habiles', 'dias_calendario', 'meses')
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

-- Índices parciales (consultas de reloj, plazos y KPIs).
CREATE INDEX IF NOT EXISTS idx_ap_es_manual
    ON actuaciones_proceso (radicado_interno, fecha_actuacion DESC NULLS LAST)
    WHERE es_manual = TRUE;

CREATE INDEX IF NOT EXISTS idx_ap_responsable
    ON actuaciones_proceso (responsable, radicado_interno)
    WHERE responsable IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ap_codigo_plazo
    ON actuaciones_proceso (codigo_plazo)
    WHERE codigo_plazo IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ap_fin_esperado
    ON actuaciones_proceso (fin_termino_esperado)
    WHERE fin_termino_esperado IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ap_desviacion
    ON actuaciones_proceso (desviacion_dias DESC NULLS LAST)
    WHERE desviacion_dias IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ap_origen
    ON actuaciones_proceso (origen)
    WHERE origen IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ap_etapa_asociada
    ON actuaciones_proceso (radicado_interno, etapa_asociada)
    WHERE etapa_asociada IS NOT NULL AND length(trim(etapa_asociada)) > 0;

CREATE INDEX IF NOT EXISTS idx_ap_calculado_pendiente
    ON actuaciones_proceso (radicado_interno)
    WHERE calculado_at IS NULL;

-- ---------------------------------------------------------------------------
-- 2. Tabla de desviaciones para informes / KPIs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS desviaciones_procesales (
    id BIGSERIAL PRIMARY KEY,
    radicado_interno TEXT NOT NULL,
    actuacion_proceso_id BIGINT NOT NULL REFERENCES actuaciones_proceso(id) ON DELETE CASCADE,
    codigo_plazo TEXT,
    etapa_asociada TEXT,
    responsable TEXT,
    fecha_actuacion DATE,
    fin_termino_esperado DATE,
    fin_termino_real DATE,
    desviacion_dias INTEGER,
    desviacion_atribuible TEXT,
    justificacion TEXT,
    estado TEXT NOT NULL DEFAULT 'abierta',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_desviaciones_actuacion
    ON desviaciones_procesales (actuacion_proceso_id);

CREATE INDEX IF NOT EXISTS idx_desviaciones_radicado
    ON desviaciones_procesales (radicado_interno, desviacion_dias DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_desviaciones_responsable
    ON desviaciones_procesales (responsable)
    WHERE responsable IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_desviaciones_parte_abiertas
    ON desviaciones_procesales (fin_termino_esperado)
    WHERE responsable = 'Parte' AND fin_termino_esperado IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Dual-write: cada insert en actuaciones se espeja en actuaciones_proceso.
--    `actuaciones` se conserva. Prejurídico sigue bloqueado por el trigger existente.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_ap_parse_fecha_text(raw text)
RETURNS date
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    IF raw IS NULL OR btrim(raw) = '' THEN
        RETURN NULL;
    END IF;
    BEGIN
        IF raw ~ '^\d{4}-\d{2}-\d{2}' THEN
            RETURN substr(raw, 1, 10)::date;
        END IF;
        IF raw ~ '^\d{2}/\d{2}/\d{4}' THEN
            RETURN to_date(substr(raw, 1, 10), 'DD/MM/YYYY');
        END IF;
        RETURN NULL;
    EXCEPTION
        WHEN others THEN
            RETURN NULL;
    END;
END;
$$;

CREATE OR REPLACE FUNCTION fn_dual_write_actuaciones_proceso()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_fecha date;
    v_hash text;
BEGIN
    IF to_regclass('public.actuaciones_proceso') IS NULL THEN
        RETURN NEW;
    END IF;

    v_hash := 'manual:' || NEW.id::text;
    v_fecha := fn_ap_parse_fecha_text(NEW.fecha);

    INSERT INTO actuaciones_proceso (
        radicado_interno,
        content_hash,
        fecha_actuacion,
        tipo_actuacion,
        descripcion,
        es_manual,
        etapa_asociada,
        origen
    ) VALUES (
        NEW.radicado_interno,
        v_hash,
        v_fecha,
        NULLIF(btrim(COALESCE(NEW.tipificacion_sugerida, '')), ''),
        NULLIF(btrim(COALESCE(NEW.descripcion, '')), ''),
        TRUE,
        NULLIF(btrim(COALESCE(NEW.etapa, '')), ''),
        'manual'
    )
    ON CONFLICT (radicado_interno, content_hash) DO NOTHING;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_dual_write_actuaciones_proceso ON actuaciones;
CREATE TRIGGER trg_dual_write_actuaciones_proceso
    AFTER INSERT ON actuaciones
    FOR EACH ROW
    EXECUTE FUNCTION fn_dual_write_actuaciones_proceso();

-- Al borrar una actuación manual, quitar el espejo (no toca filas de sync).
CREATE OR REPLACE FUNCTION fn_borrar_espejo_actuacion_manual()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF to_regclass('public.actuaciones_proceso') IS NULL THEN
        RETURN OLD;
    END IF;
    DELETE FROM actuaciones_proceso
     WHERE radicado_interno = OLD.radicado_interno
       AND content_hash IN ('manual:' || OLD.id::text, 'migracion:' || OLD.id::text)
       AND COALESCE(es_manual, FALSE) = TRUE;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_borrar_espejo_actuacion_manual ON actuaciones;
CREATE TRIGGER trg_borrar_espejo_actuacion_manual
    AFTER DELETE ON actuaciones
    FOR EACH ROW
    EXECUTE FUNCTION fn_borrar_espejo_actuacion_manual();

-- ---------------------------------------------------------------------------
-- 4. Backfill: copiar actuaciones relevantes a actuaciones_proceso
-- ---------------------------------------------------------------------------
INSERT INTO actuaciones_proceso (
    radicado_interno,
    content_hash,
    fecha_actuacion,
    tipo_actuacion,
    descripcion,
    es_manual,
    etapa_asociada,
    origen
)
SELECT
    a.radicado_interno,
    'migracion:' || a.id::text,
    fn_ap_parse_fecha_text(a.fecha),
    NULLIF(btrim(COALESCE(a.tipificacion_sugerida, '')), ''),
    NULLIF(btrim(COALESCE(a.descripcion, '')), ''),
    TRUE,
    NULLIF(btrim(COALESCE(a.etapa, '')), ''),
    'migracion'
FROM actuaciones a
WHERE a.radicado_interno IS NOT NULL
  AND length(btrim(a.radicado_interno)) > 0
  AND NOT EXISTS (
        SELECT 1
          FROM actuaciones_proceso ap
         WHERE ap.radicado_interno = a.radicado_interno
           AND ap.content_hash IN ('migracion:' || a.id::text, 'manual:' || a.id::text)
  )
ON CONFLICT (radicado_interno, content_hash) DO NOTHING;

INSERT INTO schema_migrations(version)
VALUES ('20261117_unificacion_actuaciones_desviaciones')
ON CONFLICT DO NOTHING;
