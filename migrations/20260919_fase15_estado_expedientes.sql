-- Fase 15: historial auditable de inactivacion/reactivacion de expedientes.
-- Solo DDL; no modifica datos de negocio existentes.
CREATE TABLE IF NOT EXISTS proceso_inactivaciones (
    id BIGSERIAL PRIMARY KEY,
    radicado_interno TEXT NOT NULL,
    accion TEXT NOT NULL CHECK (accion IN ('INACTIVAR','ACTIVAR')),
    motivo TEXT,
    usuario TEXT,
    fecha TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_proceso_inactivacion_motivo
        CHECK (
            accion = 'ACTIVAR'
            OR length(trim(COALESCE(motivo,''))) >= 5
        )
);

CREATE INDEX IF NOT EXISTS idx_proceso_inactivaciones_radicado_fecha
    ON proceso_inactivaciones (radicado_interno, fecha DESC);

INSERT INTO schema_migrations(version)
VALUES ('20260919_fase15_estado_expedientes')
ON CONFLICT DO NOTHING;
