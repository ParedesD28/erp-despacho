-- Historial auditable de inactivacion/reactivacion de expedientes.
-- Reemplaza el efecto de 20260919_fase15_estado_expedientes, que nunca se
-- aplico en Neon. Idempotente y aditivo (solo DDL).

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
