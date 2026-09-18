-- FASE 5.1: separación definitiva entre PROCEDIMIENTO y OBLIGACIÓN.
-- Esta migración es aditiva y está diseñada para ejecutarse explícitamente.
-- No elimina tablas ni columnas legacy.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Catálogo de obligaciones: independiente del procedimiento.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tipos_obligacion (
    id BIGSERIAL PRIMARY KEY,
    codigo TEXT NOT NULL UNIQUE,
    nombre TEXT NOT NULL UNIQUE,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    requiere_documento BOOLEAN NOT NULL DEFAULT FALSE,
    requiere_conjunto BOOLEAN NOT NULL DEFAULT FALSE,
    requiere_inmueble BOOLEAN NOT NULL DEFAULT FALSE,
    fuente_saldo TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO tipos_obligacion
    (codigo,nombre,requiere_documento,requiere_conjunto,requiere_inmueble,fuente_saldo)
VALUES
    ('CUOTAS_ADMINISTRACION','Cuotas de administración',FALSE,TRUE,TRUE,'EXPENSAS_PH'),
    ('PAGARE','Pagaré',TRUE,FALSE,FALSE,'OBLIGACION'),
    ('LETRA_CAMBIO','Letra de cambio',TRUE,FALSE,FALSE,'OBLIGACION'),
    ('FACTURA','Factura',TRUE,FALSE,FALSE,'OBLIGACION'),
    ('OTRO','Otra obligación',FALSE,FALSE,FALSE,'OBLIGACION')
ON CONFLICT (codigo) DO UPDATE SET
    nombre=EXCLUDED.nombre,
    requiere_documento=EXCLUDED.requiere_documento,
    requiere_conjunto=EXCLUDED.requiere_conjunto,
    requiere_inmueble=EXCLUDED.requiere_inmueble,
    fuente_saldo=EXCLUDED.fuente_saldo,
    activo=TRUE;

-- ---------------------------------------------------------------------------
-- 2. Catálogo de procedimientos: solo EJECUTIVO / VERBAL.
-- Se conservan los catálogos antiguos por compatibilidad histórica.
-- ---------------------------------------------------------------------------
INSERT INTO tipos_proceso
    (codigo,nombre,activo,tipo_cartera_default,requiere_conjunto,requiere_inmueble,
     requiere_juzgado,requiere_documento,fuente_saldo,familia,subtipo)
VALUES
    ('EJECUTIVO','Proceso ejecutivo',TRUE,'JURIDICO',FALSE,FALSE,FALSE,FALSE,NULL,'PROCEDIMIENTO','EJECUTIVO'),
    ('VERBAL','Proceso verbal',TRUE,'JURIDICO',FALSE,FALSE,FALSE,FALSE,NULL,'PROCEDIMIENTO','VERBAL')
ON CONFLICT (codigo) DO UPDATE SET
    nombre=EXCLUDED.nombre,
    activo=TRUE,
    tipo_cartera_default=EXCLUDED.tipo_cartera_default,
    requiere_conjunto=EXCLUDED.requiere_conjunto,
    requiere_inmueble=EXCLUDED.requiere_inmueble,
    requiere_juzgado=EXCLUDED.requiere_juzgado,
    requiere_documento=EXCLUDED.requiere_documento,
    fuente_saldo=NULL,
    familia='PROCEDIMIENTO',
    subtipo=EXCLUDED.subtipo;

-- ---------------------------------------------------------------------------
-- 3. Nuevo vínculo flexible proceso <-> obligación.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proceso_obligaciones (
    id BIGSERIAL PRIMARY KEY,
    radicado_interno TEXT NOT NULL,
    obligacion_id INTEGER NOT NULL,
    es_principal BOOLEAN NOT NULL DEFAULT TRUE,
    fecha_vinculacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_proceso_obligacion UNIQUE (radicado_interno,obligacion_id),
    CONSTRAINT fk_proceso_obligacion_proceso
        FOREIGN KEY (radicado_interno) REFERENCES procesos(radicado_interno) ON DELETE CASCADE,
    CONSTRAINT fk_proceso_obligacion_obligacion
        FOREIGN KEY (obligacion_id) REFERENCES obligaciones(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_proceso_obligaciones_proceso
    ON proceso_obligaciones(radicado_interno);
CREATE INDEX IF NOT EXISTS idx_proceso_obligaciones_obligacion
    ON proceso_obligaciones(obligacion_id);

-- ---------------------------------------------------------------------------
-- 4. Extender obligaciones sin eliminar los nombres legacy.
-- ---------------------------------------------------------------------------
ALTER TABLE obligaciones
    ADD COLUMN IF NOT EXISTS tipo_obligacion_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS deudor_contacto_id INTEGER NULL,
    ADD COLUMN IF NOT EXISTS acreedor_contacto_id INTEGER NULL,
    ADD COLUMN IF NOT EXISTS inmueble_id INTEGER NULL,
    ADD COLUMN IF NOT EXISTS capital_inicial NUMERIC(14,2) NULL,
    ADD COLUMN IF NOT EXISTS fuente_saldo TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='fk_obligacion_tipo_obligacion'
    ) THEN
        ALTER TABLE obligaciones
            ADD CONSTRAINT fk_obligacion_tipo_obligacion
            FOREIGN KEY (tipo_obligacion_id) REFERENCES tipos_obligacion(id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='fk_obligacion_deudor_contacto'
    ) THEN
        ALTER TABLE obligaciones
            ADD CONSTRAINT fk_obligacion_deudor_contacto
            FOREIGN KEY (deudor_contacto_id) REFERENCES contactos(id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='fk_obligacion_acreedor_contacto'
    ) THEN
        ALTER TABLE obligaciones
            ADD CONSTRAINT fk_obligacion_acreedor_contacto
            FOREIGN KEY (acreedor_contacto_id) REFERENCES contactos(id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='fk_obligacion_inmueble'
    ) THEN
        ALTER TABLE obligaciones
            ADD CONSTRAINT fk_obligacion_inmueble
            FOREIGN KEY (inmueble_id) REFERENCES inmuebles_ph(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_obligaciones_tipo_obligacion
    ON obligaciones(tipo_obligacion_id);
CREATE INDEX IF NOT EXISTS idx_obligaciones_deudor_contacto
    ON obligaciones(deudor_contacto_id);
CREATE INDEX IF NOT EXISTS idx_obligaciones_acreedor_contacto
    ON obligaciones(acreedor_contacto_id);
CREATE INDEX IF NOT EXISTS idx_obligaciones_inmueble
    ON obligaciones(inmueble_id);

-- ---------------------------------------------------------------------------
-- 5. Estado del radicado Rama: evita usar "PREJURIDICO" o "EN REPARTO"
-- como si fueran radicados judiciales.
-- ---------------------------------------------------------------------------
ALTER TABLE procesos
    ADD COLUMN IF NOT EXISTS estado_rama TEXT NOT NULL DEFAULT 'NO_APLICA';

UPDATE procesos
SET estado_rama = CASE
    WHEN UPPER(COALESCE(tipo_cartera,''))='PREJURIDICO' THEN 'NO_APLICA'
    WHEN NULLIF(BTRIM(COALESCE(radicado_rama,'')),'') IS NULL
         OR UPPER(BTRIM(COALESCE(radicado_rama,'')))='EN REPARTO'
        THEN 'PENDIENTE_REPARTO'
    ELSE 'ASIGNADO'
END;

-- Corrección controlada de los marcadores artificiales históricos.
UPDATE procesos
SET radicado_rama = NULL,
    juzgado = NULL,
    etapa_actual = NULL,
    estado_rama = 'NO_APLICA'
WHERE UPPER(COALESCE(tipo_cartera,''))='PREJURIDICO';

-- ---------------------------------------------------------------------------
-- 6. Reasignar el catálogo de procedimiento de los procesos históricos.
-- ---------------------------------------------------------------------------
UPDATE procesos p
SET tipo_proceso_id = tp.id
FROM tipos_proceso tp
WHERE tp.codigo = CASE
    WHEN UPPER(BTRIM(COALESCE(p.naturaleza,'')))='VERBAL' THEN 'VERBAL'
    ELSE 'EJECUTIVO'
END;

-- Los antiguos códigos que mezclaban procedimiento y título quedan fuera
-- del selector principal; NO se eliminan.
UPDATE tipos_proceso
SET activo=FALSE
WHERE codigo IN (
    'CUOTAS_ADMINISTRACION','PAGARE','LETRA_CAMBIO','FACTURA','OTRO'
)
AND codigo NOT IN ('EJECUTIVO','VERBAL');

-- ---------------------------------------------------------------------------
-- 7. Crear una obligación canónica para cada proceso histórico de PH.
-- No se usa pretensiones como saldo inicial; el saldo se obtendrá de
-- expensas_ph.
-- ---------------------------------------------------------------------------
INSERT INTO obligaciones (
    identificacion_deudor,
    tipo_titulo,
    numero_documento,
    capital,
    fecha_exigibilidad,
    estado,
    proceso_id,
    tipo_proceso_id,
    tipo_obligacion_id,
    deudor_contacto_id,
    acreedor_contacto_id,
    inmueble_id,
    capital_inicial,
    fuente_saldo
)
SELECT
    cdeu.identificacion,
    'CUOTAS_ADMINISTRACION',
    NULL,
    0,
    NULL,
    'ACTIVA',
    p.radicado_interno,
    p.tipo_proceso_id,
    tob.id,
    cdeu.id,
    acre.id,
    p.inmueble_id,
    0,
    'EXPENSAS_PH'
FROM procesos p
JOIN tipos_proceso tp
  ON tp.id=p.tipo_proceso_id
 AND tp.codigo='EJECUTIVO'
JOIN tipos_obligacion tob
  ON tob.codigo='CUOTAS_ADMINISTRACION'
JOIN contactos acre
  ON acre.identificacion = split_part(COALESCE(p.id_cliente,''),'|',1)
JOIN LATERAL (
    SELECT c.id,c.identificacion
    FROM proceso_partes pp
    JOIN contactos c ON c.id=pp.contacto_id
    WHERE pp.radicado_interno=p.radicado_interno
      AND pp.rol='DEMANDADO'
    ORDER BY pp.es_principal DESC,pp.id
    LIMIT 1
) cdeu ON TRUE
WHERE p.inmueble_id IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM tipos_obligacion x
      WHERE x.id=tob.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM obligaciones o
      WHERE o.proceso_id=p.radicado_interno
        AND o.tipo_obligacion_id=tob.id
  );

-- ---------------------------------------------------------------------------
-- 8. Relacionar obligaciones PH con proceso y detalle mensual.
-- ---------------------------------------------------------------------------
INSERT INTO proceso_obligaciones (radicado_interno,obligacion_id,es_principal)
SELECT o.proceso_id,o.id,TRUE
FROM obligaciones o
WHERE o.proceso_id IS NOT NULL
  AND o.tipo_obligacion_id=(SELECT id FROM tipos_obligacion WHERE codigo='CUOTAS_ADMINISTRACION')
ON CONFLICT (radicado_interno,obligacion_id) DO NOTHING;

UPDATE expensas_ph e
SET obligation_id = o.id
FROM obligaciones o
WHERE o.tipo_obligacion_id=(SELECT id FROM tipos_obligacion WHERE codigo='CUOTAS_ADMINISTRACION')
  AND o.inmueble_id=e.inmueble_id
  AND e.obligation_id IS NULL;

-- ---------------------------------------------------------------------------
-- 9. Vínculo financiero progresivo en CRM, acuerdos, recaudos, SMS y agenda.
-- ---------------------------------------------------------------------------
ALTER TABLE gestiones_crm
    ADD COLUMN IF NOT EXISTS obligacion_id INTEGER NULL;

ALTER TABLE acuerdos_pago
    ADD COLUMN IF NOT EXISTS obligacion_id INTEGER NULL;

ALTER TABLE recaudos_contabilidad
    ADD COLUMN IF NOT EXISTS obligacion_id INTEGER NULL;

ALTER TABLE sms_cola_envios
    ADD COLUMN IF NOT EXISTS obligacion_id INTEGER NULL;

ALTER TABLE vencimientos
    ADD COLUMN IF NOT EXISTS obligacion_id INTEGER NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_crm_obligacion') THEN
        ALTER TABLE gestiones_crm
            ADD CONSTRAINT fk_crm_obligacion
            FOREIGN KEY (obligacion_id) REFERENCES obligaciones(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_acuerdo_obligacion') THEN
        ALTER TABLE acuerdos_pago
            ADD CONSTRAINT fk_acuerdo_obligacion
            FOREIGN KEY (obligacion_id) REFERENCES obligaciones(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_recaudo_obligacion') THEN
        ALTER TABLE recaudos_contabilidad
            ADD CONSTRAINT fk_recaudo_obligacion
            FOREIGN KEY (obligacion_id) REFERENCES obligaciones(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_sms_obligacion') THEN
        ALTER TABLE sms_cola_envios
            ADD CONSTRAINT fk_sms_obligacion
            FOREIGN KEY (obligacion_id) REFERENCES obligaciones(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_vencimiento_obligacion') THEN
        ALTER TABLE vencimientos
            ADD CONSTRAINT fk_vencimiento_obligacion
            FOREIGN KEY (obligacion_id) REFERENCES obligaciones(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_gestiones_crm_obligacion
    ON gestiones_crm(obligacion_id,fecha DESC);
CREATE INDEX IF NOT EXISTS idx_acuerdos_obligacion
    ON acuerdos_pago(obligacion_id,fecha_compromiso DESC);
CREATE INDEX IF NOT EXISTS idx_recaudos_obligacion
    ON recaudos_contabilidad(obligacion_id,fecha_pago DESC);
CREATE INDEX IF NOT EXISTS idx_sms_obligacion
    ON sms_cola_envios(obligacion_id,estado);
CREATE INDEX IF NOT EXISTS idx_vencimientos_obligacion
    ON vencimientos(obligacion_id,fecha_vencimiento);

-- Backfill seguro cuando la relación proceso -> obligación es unívoca.
UPDATE gestiones_crm g
SET obligacion_id=po.obligacion_id
FROM proceso_obligaciones po
WHERE g.obligacion_id IS NULL
  AND g.radicado_interno=po.radicado_interno;

UPDATE recaudos_contabilidad r
SET obligacion_id=o.id
FROM obligaciones o
WHERE r.obligacion_id IS NULL
  AND r.inmueble_id=o.inmueble_id
  AND o.tipo_obligacion_id=(SELECT id FROM tipos_obligacion WHERE codigo='CUOTAS_ADMINISTRACION');

UPDATE sms_cola_envios s
SET obligacion_id=o.id
FROM obligaciones o
WHERE s.obligacion_id IS NULL
  AND s.inmueble_id=o.inmueble_id
  AND o.tipo_obligacion_id=(SELECT id FROM tipos_obligacion WHERE codigo='CUOTAS_ADMINISTRACION');

UPDATE vencimientos v
SET obligacion_id=po.obligacion_id
FROM proceso_obligaciones po
WHERE v.obligacion_id IS NULL
  AND v.radicado_interno=po.radicado_interno;

-- No se vinculan automáticamente acuerdos antiguos sin inmueble/proceso,
-- porque su identificación actual no permite una relación inequívoca.

-- ---------------------------------------------------------------------------
-- 10. Actualizar el nombre legacy de saldo en las obligaciones PH.
-- ---------------------------------------------------------------------------
UPDATE obligaciones
SET capital_inicial=COALESCE(capital_inicial,0),
    fuente_saldo=COALESCE(fuente_saldo,'EXPENSAS_PH')
WHERE tipo_obligacion_id=(SELECT id FROM tipos_obligacion WHERE codigo='CUOTAS_ADMINISTRACION');

COMMIT;
