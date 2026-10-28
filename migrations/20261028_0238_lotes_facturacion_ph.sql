-- Auditoría de lotes del cargador de facturación mensual COLON.
-- Idempotencia de re-subida: mismo sha256 + conjunto + período.

CREATE TABLE IF NOT EXISTS lotes_facturacion_ph (
    id              bigserial PRIMARY KEY,
    sha256          text NOT NULL,
    conjunto_id     integer NOT NULL,
    periodo_anio    integer NOT NULL,
    periodo_mes     integer NOT NULL,
    nombre_archivo  text,
    usuario_id      text,
    facturas_pdf    integer NOT NULL DEFAULT 0,
    unidades_nuestras integer NOT NULL DEFAULT 0,
    unidades_aplicadas integer NOT NULL DEFAULT 0,
    unidades_bloqueadas integer NOT NULL DEFAULT 0,
    detalle         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamp without time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_lotes_facturacion_ph_hash_periodo
    ON lotes_facturacion_ph (sha256, conjunto_id, periodo_anio, periodo_mes);

CREATE INDEX IF NOT EXISTS idx_lotes_facturacion_ph_conjunto_periodo
    ON lotes_facturacion_ph (conjunto_id, periodo_anio, periodo_mes, created_at DESC);
