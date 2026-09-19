-- FASE 7: normalización de invariantes de partes
-- Objetivo:
-- 1) Cada proceso puede tener múltiples personas por rol.
-- 2) Cada proceso/rol debe tener como máximo un principal.
-- 3) Si existe obligación principal, se prioriza su acreedor/deudor principal.
-- 4) No se crean ni eliminan personas; solo se corrige la marca es_principal.
-- 5) Idempotente.

BEGIN;

-- 1. Cuando hay más de un principal en un mismo proceso/rol,
-- primero se desmarcan todos los principales afectados.
UPDATE proceso_partes pp
SET es_principal=FALSE
WHERE pp.es_principal=TRUE
  AND EXISTS (
      SELECT 1
      FROM proceso_partes pp2
      WHERE pp2.radicado_interno=pp.radicado_interno
        AND pp2.rol=pp.rol
        AND pp2.es_principal=TRUE
      GROUP BY pp2.radicado_interno, pp2.rol
      HAVING COUNT(*)>1
  );

-- 2. Se elige un único principal para cada grupo afectado.
-- Para DEMANDANTE: acreedor de la obligación principal.
-- Para DEMANDADO: deudor principal de obligacion_partes de la obligación principal.
-- Si no existe correspondencia económica, se conserva el menor contacto_id
-- como criterio determinista y no se inventa una relación.
WITH afectados AS (
    SELECT DISTINCT radicado_interno, rol
    FROM proceso_partes
    WHERE es_principal=FALSE
),
candidatos AS (
    SELECT
        pp.radicado_interno,
        pp.rol,
        pp.contacto_id,
        ROW_NUMBER() OVER (
            PARTITION BY pp.radicado_interno, pp.rol
            ORDER BY
                CASE
                    WHEN pp.rol='DEMANDANTE'
                         AND o.acreedor_contacto_id=pp.contacto_id
                    THEN 0
                    WHEN pp.rol='DEMANDADO'
                         AND op.contacto_id=pp.contacto_id
                         AND op.es_principal=TRUE
                    THEN 0
                    ELSE 1
                END,
                pp.contacto_id
        ) AS rn
    FROM proceso_partes pp
    JOIN afectados a
      ON a.radicado_interno=pp.radicado_interno
     AND a.rol=pp.rol
    LEFT JOIN proceso_obligaciones pox
      ON pox.radicado_interno=pp.radicado_interno
     AND pox.es_principal=TRUE
    LEFT JOIN obligaciones o
      ON o.id=pox.obligacion_id
    LEFT JOIN obligacion_partes op
      ON op.obligacion_id=o.id
     AND op.es_principal=TRUE
)
UPDATE proceso_partes pp
SET es_principal=TRUE
FROM candidatos c
WHERE c.radicado_interno=pp.radicado_interno
  AND c.rol=pp.rol
  AND c.contacto_id=pp.contacto_id
  AND c.rn=1;

INSERT INTO schema_migrations(version)
VALUES ('20260919_fase7_normalizar_principales_partes')
ON CONFLICT (version) DO NOTHING;

COMMIT;
