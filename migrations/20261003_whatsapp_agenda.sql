-- Agenda WhatsApp para supervisión: teléfono ↔ nombre legible.
-- Idempotente. También se asegura en runtime (agent_supervision.ensure_whatsapp_agenda).

CREATE TABLE IF NOT EXISTS whatsapp_agenda (
    id BIGSERIAL PRIMARY KEY,
    telefono_digits TEXT NOT NULL,
    telefono_display TEXT,
    nombre TEXT NOT NULL,
    notas TEXT,
    contacto_id BIGINT,
    updated_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_whatsapp_agenda_telefono_digits UNIQUE (telefono_digits)
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_agenda_nombre
    ON whatsapp_agenda (nombre);

CREATE INDEX IF NOT EXISTS idx_whatsapp_agenda_updated
    ON whatsapp_agenda (updated_at DESC);
