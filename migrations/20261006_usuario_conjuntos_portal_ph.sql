-- Portal Cliente PH: vínculo usuario (abogados) ↔ conjuntos habilitados.
-- Idempotente. Perfil CLIENTE_PH se asegura también desde usuarios_service.ensure_perfiles_schema.

INSERT INTO perfiles (codigo, nombre, descripcion, activo) VALUES
    (
        'CLIENTE_PH',
        'Cliente PH',
        'Portal PH: solo unidades (torre/apto + demandados) de los conjuntos que el Admin le habilite.',
        TRUE
    )
ON CONFLICT (codigo) DO UPDATE
SET nombre = EXCLUDED.nombre,
    descripcion = EXCLUDED.descripcion,
    activo = TRUE;

CREATE TABLE IF NOT EXISTS usuario_conjuntos (
    usuario_id INTEGER NOT NULL REFERENCES abogados(id) ON DELETE CASCADE,
    conjunto_id INTEGER NOT NULL REFERENCES conjuntos_residenciales(id) ON DELETE CASCADE,
    PRIMARY KEY (usuario_id, conjunto_id)
);

CREATE INDEX IF NOT EXISTS idx_usuario_conjuntos_conjunto
    ON usuario_conjuntos (conjunto_id);

INSERT INTO schema_migrations(version)
VALUES ('20261006_usuario_conjuntos_portal_ph')
ON CONFLICT (version) DO NOTHING;
