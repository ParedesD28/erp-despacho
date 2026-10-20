-- PDFs de actuaciones sincronizados a Google Drive.
-- Columnas en actuaciones_proceso (idempotente).

ALTER TABLE actuaciones_proceso
    ADD COLUMN IF NOT EXISTS drive_url TEXT;

ALTER TABLE actuaciones_proceso
    ADD COLUMN IF NOT EXISTS drive_file_id TEXT;

CREATE INDEX IF NOT EXISTS idx_actuaciones_proceso_drive_file
    ON actuaciones_proceso (drive_file_id)
    WHERE drive_file_id IS NOT NULL AND length(trim(drive_file_id)) > 0;

INSERT INTO schema_migrations(version)
VALUES ('20261020_actuaciones_proceso_drive')
ON CONFLICT DO NOTHING;
