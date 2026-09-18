ALTER TABLE tipos_proceso
  ADD COLUMN IF NOT EXISTS familia TEXT NOT NULL DEFAULT 'EJECUTIVO',
  ADD COLUMN IF NOT EXISTS subtipo TEXT;

UPDATE tipos_proceso
SET familia='EJECUTIVO',
    subtipo=CASE codigo
      WHEN 'CUOTAS_ADMINISTRACION' THEN 'CUOTAS_ADMINISTRACION'
      WHEN 'PAGARE' THEN 'PAGARE'
      WHEN 'LETRA_CAMBIO' THEN 'LETRA_CAMBIO'
      WHEN 'FACTURA' THEN 'FACTURA'
      ELSE 'OTRO'
    END;

CREATE INDEX IF NOT EXISTS idx_tipos_proceso_familia ON tipos_proceso(familia);
CREATE INDEX IF NOT EXISTS idx_tipos_proceso_subtipo ON tipos_proceso(subtipo);
