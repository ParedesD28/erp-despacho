-- Backfill seguro: titulares del inmueble desde datos ya radicados.
-- No borra ni actualiza filas existentes (ON CONFLICT DO NOTHING).
-- Cubre procesos nuevos que crearon proceso_partes / inmuebles_ph.contacto_id
-- pero no escribieron inmueble_propietarios (p. ej. EXP-0065 y similares).

-- 1) Dueño canónico del inmueble (columna legado contacto_id).
INSERT INTO inmueble_propietarios (inmueble_id, contacto_id, es_principal)
SELECT i.id, i.contacto_id, TRUE
FROM inmuebles_ph i
WHERE i.contacto_id IS NOT NULL
ON CONFLICT (inmueble_id, contacto_id) DO NOTHING;

-- 2) Demandados de procesos vinculados a un inmueble.
INSERT INTO inmueble_propietarios (inmueble_id, contacto_id, es_principal)
SELECT DISTINCT p.inmueble_id, pp.contacto_id, COALESCE(pp.es_principal, FALSE)
FROM procesos p
JOIN proceso_partes pp
  ON pp.radicado_interno = p.radicado_interno
WHERE p.inmueble_id IS NOT NULL
  AND UPPER(pp.rol) = 'DEMANDADO'
ON CONFLICT (inmueble_id, contacto_id) DO NOTHING;
