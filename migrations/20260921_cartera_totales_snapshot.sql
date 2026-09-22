-- Snapshot de totales de cartera para el dashboard (sin liquidar en cada refresh).
CREATE TABLE IF NOT EXISTS cartera_totales_snapshot (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    capital NUMERIC(18,2) NOT NULL DEFAULT 0,
    intereses NUMERIC(18,2) NOT NULL DEFAULT 0,
    honorarios NUMERIC(18,2) NOT NULL DEFAULT 0,
    valor_cartera NUMERIC(18,2) NOT NULL DEFAULT 0,
    obligaciones_incluidas INTEGER NOT NULL DEFAULT 0,
    obligaciones_sin_deuda INTEGER NOT NULL DEFAULT 0,
    obligaciones_error INTEGER NOT NULL DEFAULT 0,
    fecha_corte DATE,
    calculado_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_por TEXT
);
