-- Settings cifrados (p. ej. Google OAuth refresh token).
-- Idempotente.

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_encrypted TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO schema_migrations(version)
VALUES ('20261020_app_settings')
ON CONFLICT DO NOTHING;
