-- Completa el catálogo de obligaciones con la referencia documental del título.
ALTER TABLE obligaciones
    ADD COLUMN IF NOT EXISTS numero_documento TEXT NULL;
CREATE INDEX IF NOT EXISTS idx_obligaciones_numero_documento
    ON obligaciones(numero_documento);