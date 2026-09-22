-- Perfiles humanos del ERP + columnas en abogados.
-- Idempotente. El bot NO usa esta tabla (sigue con API key).
-- Rollout: usuarios existentes sin perfil reciben ADMIN.

CREATE TABLE IF NOT EXISTS perfiles (
    id SERIAL PRIMARY KEY,
    codigo VARCHAR(32) NOT NULL UNIQUE,
    nombre TEXT NOT NULL,
    descripcion TEXT,
    activo BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO perfiles (codigo, nombre, descripcion, activo) VALUES
    ('ADMIN', 'Administrador', 'Todo el ERP: usuarios, menús, configuración y acciones fuertes.', TRUE),
    ('ABOGADO', 'Abogado', 'Expedientes, cartera, cobro completo, acuerdos, cartas y SMS.', TRUE),
    ('AUXILIAR_COBRO', 'Auxiliar de cobro', 'Seguimiento diario de cobro (CRM, SMS, cartas, vencimientos); sin borrar lo crítico ni administrar usuarios.', TRUE),
    ('CONSULTA', 'Solo consulta', 'Ver cartera e informes; no editar ni enviar SMS/cartas.', TRUE)
ON CONFLICT (codigo) DO UPDATE
SET nombre = EXCLUDED.nombre,
    descripcion = EXCLUDED.descripcion,
    activo = TRUE;

ALTER TABLE abogados ADD COLUMN IF NOT EXISTS perfil_id INTEGER REFERENCES perfiles(id);
ALTER TABLE abogados ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE abogados
SET perfil_id = (SELECT id FROM perfiles WHERE codigo = 'ADMIN' LIMIT 1)
WHERE perfil_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_abogados_perfil_id ON abogados (perfil_id);
CREATE INDEX IF NOT EXISTS idx_abogados_activo ON abogados (activo);

INSERT INTO schema_migrations(version)
VALUES ('20260922_perfiles_permisos')
ON CONFLICT (version) DO NOTHING;
