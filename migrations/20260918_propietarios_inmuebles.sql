-- Relación normalizada inmueble -> todos sus propietarios.
CREATE TABLE IF NOT EXISTS inmueble_propietarios (
    id BIGSERIAL PRIMARY KEY,
    inmueble_id INTEGER NOT NULL,
    contacto_id INTEGER NOT NULL,
    es_principal BOOLEAN NOT NULL DEFAULT FALSE,
    fecha_vinculacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_inmueble_propietarios_inmueble
        FOREIGN KEY (inmueble_id) REFERENCES inmuebles_ph(id) ON DELETE CASCADE,
    CONSTRAINT fk_inmueble_propietarios_contacto
        FOREIGN KEY (contacto_id) REFERENCES contactos(id) ON DELETE RESTRICT,
    CONSTRAINT uq_inmueble_propietario
        UNIQUE (inmueble_id, contacto_id)
);
CREATE INDEX IF NOT EXISTS idx_inmueble_propietarios_inmueble
    ON inmueble_propietarios(inmueble_id);
CREATE INDEX IF NOT EXISTS idx_inmueble_propietarios_contacto
    ON inmueble_propietarios(contacto_id);
INSERT INTO inmueble_propietarios (inmueble_id,contacto_id,es_principal)
SELECT i.id,i.contacto_id,TRUE
FROM inmuebles_ph i
WHERE i.contacto_id IS NOT NULL
ON CONFLICT (inmueble_id,contacto_id) DO NOTHING;
INSERT INTO inmueble_propietarios (inmueble_id,contacto_id,es_principal)
SELECT DISTINCT p.inmueble_id,pp.contacto_id,FALSE
FROM procesos p
JOIN proceso_partes pp
  ON pp.radicado_interno=p.radicado_interno
WHERE p.inmueble_id IS NOT NULL
  AND pp.rol='DEMANDADO'
ON CONFLICT (inmueble_id,contacto_id) DO NOTHING;