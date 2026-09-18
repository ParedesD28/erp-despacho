ALTER TABLE sms_cola_envios
  ADD COLUMN IF NOT EXISTS saldo_fuente TEXT,
  ADD COLUMN IF NOT EXISTS saldo_verificado BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS saldo_calculado_en TIMESTAMP,
  ADD COLUMN IF NOT EXISTS mensaje_template TEXT;
CREATE INDEX IF NOT EXISTS idx_sms_cola_saldo_verificado ON sms_cola_envios(estado,saldo_verificado);