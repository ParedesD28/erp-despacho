-- FASE 5: migración controlada de datos
-- Reglas:
-- 1) No inventa obligaciones ni capitales.
-- 2) Solo crea vínculos cuando existe una relación inequívoca.
-- 3) Registra excepciones históricas sin destruir ni sobreescribir información.
-- 4) Es idempotente.

BEGIN;

CREATE TABLE IF NOT EXISTS obligacion_partes (
    id BIGSERIAL PRIMARY KEY,
    obligacion_id BIGINT NOT NULL REFERENCES obligaciones(id) ON DELETE CASCADE,
    contacto_id BIGINT NOT NULL REFERENCES contactos(id) ON DELETE RESTRICT,
    rol VARCHAR(30) NOT NULL DEFAULT 'DEUDOR',
    es_principal BOOLEAN NOT NULL DEFAULT FALSE,
    fecha_vinculacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_obligacion_partes_rol
        CHECK (rol IN ('DEUDOR','CODEUDOR','GARANTE')),
    CONSTRAINT uq_obligacion_partes_rol
        UNIQUE (obligacion_id, contacto_id, rol)
);

CREATE INDEX IF NOT EXISTS idx_obligacion_partes_obligacion
    ON obligacion_partes(obligacion_id);

CREATE INDEX IF NOT EXISTS idx_obligacion_partes_contacto
    ON obligacion_partes(contacto_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obligacion_parte_principal
    ON obligacion_partes(obligacion_id)
    WHERE es_principal=TRUE;

CREATE TABLE IF NOT EXISTS data_migration_exceptions (
    id BIGSERIAL PRIMARY KEY,
    migration_version TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    notes TEXT,
    CONSTRAINT ck_data_migration_exception_status
        CHECK (status IN ('PENDING_REVIEW','RESOLVED','IGNORED')),
    CONSTRAINT uq_data_migration_exception
        UNIQUE (migration_version, entity_type, entity_key)
);

-- 1. Backfill de la relación obligación -> deudor existente.
INSERT INTO obligacion_partes (
    obligacion_id, contacto_id, rol, es_principal
)
SELECT
    o.id,
    o.deudor_contacto_id,
    'DEUDOR',
    TRUE
FROM obligaciones o
WHERE o.deudor_contacto_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM obligacion_partes op
      WHERE op.obligacion_id=o.id
        AND op.contacto_id=o.deudor_contacto_id
        AND op.rol='DEUDOR'
  );

-- 2. Garantizar que los procesos existentes tengan partes normalizadas
-- cuando los campos legacy permiten reconstruirlas sin ambigüedad.
INSERT INTO proceso_partes (
    radicado_interno, contacto_id, rol, es_principal, fecha_vinculacion
)
SELECT p.radicado_interno, c.id, 'DEMANDANTE', TRUE, CURRENT_TIMESTAMP
FROM procesos p
JOIN contactos c
  ON c.identificacion=BTRIM(SPLIT_PART(COALESCE(p.id_cliente,''),'|',1))
WHERE NULLIF(BTRIM(SPLIT_PART(COALESCE(p.id_cliente,''),'|',1)),'') IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM proceso_partes pp
      WHERE pp.radicado_interno=p.radicado_interno
        AND pp.contacto_id=c.id
        AND pp.rol='DEMANDANTE'
  );

INSERT INTO proceso_partes (
    radicado_interno, contacto_id, rol, es_principal, fecha_vinculacion
)
SELECT p.radicado_interno, c.id, 'DEMANDADO', TRUE, CURRENT_TIMESTAMP
FROM procesos p
JOIN contactos c
  ON c.identificacion=BTRIM(SPLIT_PART(COALESCE(p.id_demandado,''),'|',1))
WHERE NULLIF(BTRIM(SPLIT_PART(COALESCE(p.id_demandado,''),'|',1)),'') IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM proceso_partes pp
      WHERE pp.radicado_interno=p.radicado_interno
        AND pp.contacto_id=c.id
        AND pp.rol='DEMANDADO'
  );

-- 3. Vinculación financiera inequívoca por expediente cuando existe exactamente una obligación.
-- Acuerdos de pago NO se incluyen aquí porque la tabla histórica no contiene
-- radicado_interno. Se conservan como excepciones si carecen de obligation_id.
UPDATE gestiones_crm g
SET obligacion_id=x.obligacion_id
FROM (
    SELECT g2.id,MIN(po.obligacion_id) AS obligacion_id
    FROM gestiones_crm g2
    JOIN proceso_obligaciones po
      ON po.radicado_interno=g2.radicado_interno
    WHERE g2.obligacion_id IS NULL
    GROUP BY g2.id
    HAVING COUNT(DISTINCT po.obligacion_id)=1
) x
WHERE g.id=x.id
  AND g.obligacion_id IS NULL;

UPDATE vencimientos v
SET obligacion_id=x.obligacion_id
FROM (
    SELECT v2.id,MIN(po.obligacion_id) AS obligacion_id
    FROM vencimientos v2
    JOIN proceso_obligaciones po
      ON po.radicado_interno=v2.radicado_interno
    WHERE v2.obligacion_id IS NULL
    GROUP BY v2.id
    HAVING COUNT(DISTINCT po.obligacion_id)=1
) x
WHERE v.id=x.id
  AND v.obligacion_id IS NULL;

-- 4. PH: una expensa solo puede ligarse automáticamente cuando existe
-- una única obligación PH para el mismo inmueble.
UPDATE expensas_ph e
SET obligation_id = x.obligation_id
FROM (
    SELECT e2.id, MIN(o.id) AS obligation_id
    FROM expensas_ph e2
    JOIN obligaciones o
      ON o.inmueble_id=e2.inmueble_id
     AND UPPER(COALESCE(o.fuente_saldo,''))='EXPENSAS_PH'
    WHERE e2.obligation_id IS NULL
    GROUP BY e2.id
    HAVING COUNT(DISTINCT o.id)=1
) x
WHERE e.id=x.id
  AND e.obligation_id IS NULL;

-- 5. Registrar excepciones históricas que deliberadamente NO se reconstruyen.
INSERT INTO data_migration_exceptions (
    migration_version, entity_type, entity_key, reason
)
SELECT
    '20260918_fase5_migracion_controlada',
    'PROCESO_SIN_OBLIGACION',
    p.radicado_interno,
    'Proceso EJECUTIVO histórico sin obligación canónica inequívoca. No crear obligación a partir de pretensiones.'
FROM procesos p
LEFT JOIN proceso_obligaciones po
  ON po.radicado_interno=p.radicado_interno
WHERE UPPER(COALESCE(p.naturaleza,''))='EJECUTIVO'
GROUP BY p.radicado_interno
HAVING COUNT(po.obligacion_id)=0
ON CONFLICT DO NOTHING;

INSERT INTO data_migration_exceptions (
    migration_version, entity_type, entity_key, reason
)
SELECT
    '20260918_fase5_migracion_controlada',
    'ACUERDO_SIN_OBLIGACION',
    a.id::TEXT,
    'Acuerdo histórico sin obligación ni vínculo procesal inequívoco.'
FROM acuerdos_pago a
WHERE a.obligacion_id IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO data_migration_exceptions (
    migration_version, entity_type, entity_key, reason
)
SELECT
    '20260918_fase5_migracion_controlada',
    'VENCIMIENTO_SIN_OBLIGACION',
    v.id::TEXT,
    'Vencimiento histórico sin obligación inequívoca.'
FROM vencimientos v
WHERE v.obligacion_id IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO data_migration_exceptions (
    migration_version, entity_type, entity_key, reason
)
SELECT
    '20260918_fase5_migracion_controlada',
    'CRM_SIN_OBLIGACION',
    g.id::TEXT,
    'Gestión CRM histórica sin obligación inequívoca.'
FROM gestiones_crm g
WHERE g.obligacion_id IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations(version)
VALUES ('20260918_fase5_migracion_controlada')
ON CONFLICT (version) DO NOTHING;

COMMIT;
