-- Vincula automáticamente los conjuntos existentes con su contacto jurídico cuando el nombre coincide.
UPDATE conjuntos_residenciales c
SET contacto_id=ct.id
FROM contactos ct
WHERE c.contacto_id IS NULL
  AND LOWER(BTRIM(ct.nombre))=LOWER(BTRIM(c.nombre))
  AND ct.tipo='Cliente';