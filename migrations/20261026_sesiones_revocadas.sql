-- Sesiones revocadas (logout / re-login). El código también la crea de forma
-- perezosa con IF NOT EXISTS, por lo que aplicar este archivo es opcional.
CREATE TABLE IF NOT EXISTS sesiones_revocadas (
    sid TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expira_en TIMESTAMPTZ NOT NULL,
    revocada_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sesiones_revocadas_expira ON sesiones_revocadas (expira_en);
