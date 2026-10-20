-- Sync diaria Rama Judicial: actuaciones externas + alertas in-ERP.
-- Solo DDL; no modifica datos de negocio existentes.
-- Idempotente (IF NOT EXISTS).

-- Actuaciones sincronizadas desde la API legal (distintas de `actuaciones` manuales del ERP).
CREATE TABLE IF NOT EXISTS actuaciones_proceso (
    id BIGSERIAL PRIMARY KEY,
    radicado_interno TEXT NOT NULL,
    radicado_rama TEXT,
    id_actuacion TEXT,
    content_hash TEXT NOT NULL,
    fecha_actuacion DATE,
    fecha_registro TIMESTAMPTZ,
    tipo_actuacion TEXT,
    descripcion TEXT,
    con_documentos BOOLEAN,
    -- Términos procesales (baratos; reutilizables para vencimientos futuros)
    inicio_termino DATE,
    fin_termino DATE,
    tipo_termino TEXT,
    vencido BOOLEAN,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_actuaciones_proceso_hash
    ON actuaciones_proceso (radicado_interno, content_hash);

CREATE UNIQUE INDEX IF NOT EXISTS uq_actuaciones_proceso_id_ext
    ON actuaciones_proceso (radicado_interno, id_actuacion)
    WHERE id_actuacion IS NOT NULL AND length(trim(id_actuacion)) > 0;

CREATE INDEX IF NOT EXISTS idx_actuaciones_proceso_radicado_fecha
    ON actuaciones_proceso (radicado_interno, fecha_actuacion DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_actuaciones_proceso_fin_termino
    ON actuaciones_proceso (fin_termino)
    WHERE fin_termino IS NOT NULL;

-- Alertas de seguimiento (solo ERP; sin SMS ni canales externos).
CREATE TABLE IF NOT EXISTS alertas_seguimiento (
    id BIGSERIAL PRIMARY KEY,
    radicado_interno TEXT NOT NULL,
    actuacion_proceso_id BIGINT REFERENCES actuaciones_proceso(id) ON DELETE SET NULL,
    asignado_a BIGINT,
    titulo TEXT NOT NULL,
    detalle TEXT,
    leida BOOLEAN NOT NULL DEFAULT FALSE,
    atendida BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    leida_at TIMESTAMPTZ,
    atendida_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_alertas_seguimiento_unread
    ON alertas_seguimiento (leida, created_at DESC)
    WHERE leida = FALSE;

CREATE INDEX IF NOT EXISTS idx_alertas_seguimiento_radicado
    ON alertas_seguimiento (radicado_interno, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_alertas_seguimiento_asignado
    ON alertas_seguimiento (asignado_a, leida)
    WHERE asignado_a IS NOT NULL;

-- Flag barato en expediente para revisión pendiente.
ALTER TABLE procesos
    ADD COLUMN IF NOT EXISTS requiere_revision BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_procesos_requiere_revision
    ON procesos (requiere_revision)
    WHERE requiere_revision = TRUE;

INSERT INTO schema_migrations(version)
VALUES ('20261020_sync_rama_actuaciones_alertas')
ON CONFLICT DO NOTHING;
