-- Titulo ejecutivo: cada cobro conserva el concepto original del PDF.
-- El total mensual es una vista; no se guardan filas sinteticas.

CREATE TABLE IF NOT EXISTS etl_cargas_estado_cuenta (
    id bigserial PRIMARY KEY,
    creado_en timestamptz NOT NULL DEFAULT now(),
    archivo_origen text,
    usuario_id text
);

CREATE TABLE IF NOT EXISTS movimientos_titulo_ejecutivo (
    id bigserial PRIMARY KEY,
    carga_id bigint REFERENCES etl_cargas_estado_cuenta (id),
    archivo_origen text NOT NULL,
    titular text NOT NULL,
    bloque text,
    apartamento text,
    codigo_cuenta text,
    concepto_original text NOT NULL,
    clasificacion text NOT NULL CHECK (clasificacion IN ('CUOTA_ORDINARIA', 'EXTRAORDINARIO')),
    tipo_documento text NOT NULL,
    numero_documento text,
    fecha date NOT NULL,
    valor numeric(18, 2) NOT NULL,
    abono numeric(18, 2),
    saldo_inverso numeric(18, 2),
    diferencia_mora numeric(18, 2),
    fecha_inicio_mora date NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_titulo_titular_fecha
    ON movimientos_titulo_ejecutivo (titular, fecha);

CREATE OR REPLACE VIEW v_consolidado_mensual AS
SELECT
    archivo_origen,
    titular,
    codigo_cuenta,
    bloque,
    apartamento,
    date_trunc('month', fecha)::date AS periodo,
    fecha_inicio_mora,
    COALESCE(sum(valor) FILTER (WHERE clasificacion = 'CUOTA_ORDINARIA'), 0) AS valor_cuota,
    COALESCE(sum(valor) FILTER (WHERE clasificacion = 'EXTRAORDINARIO'), 0) AS valor_extras,
    COALESCE(sum(valor), 0) AS valor_total,
    string_agg(concepto_original, ' | ' ORDER BY concepto_original) AS conceptos_originales
FROM movimientos_titulo_ejecutivo
GROUP BY
    archivo_origen,
    titular,
    codigo_cuenta,
    bloque,
    apartamento,
    date_trunc('month', fecha)::date,
    fecha_inicio_mora;
