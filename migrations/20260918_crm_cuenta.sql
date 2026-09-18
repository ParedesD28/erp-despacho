ALTER TABLE gestiones_crm
  ADD COLUMN IF NOT EXISTS radicado_interno TEXT;

CREATE INDEX IF NOT EXISTS idx_gestiones_crm_radicado
  ON gestiones_crm (radicado_interno, fecha DESC);
