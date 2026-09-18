-- Catálogos de procesos y conjuntos: estructura declarativa, sin migraciones automáticas en el arranque.

CREATE TABLE IF NOT EXISTS tipos_proceso (
    id BIGSERIAL PRIMARY KEY,
    codigo TEXT NOT NULL UNIQUE,
    nombre TEXT NOT NULL UNIQUE,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    tipo_cartera_default VARCHAR(20) NOT NULL DEFAULT 'JURIDICO',
    requiere_conjunto BOOLEAN NOT NULL DEFAULT FALSE,
    requiere_inmueble BOOLEAN NOT NULL DEFAULT FALSE,
    requiere_juzgado BOOLEAN NOT NULL DEFAULT FALSE,
    requiere_documento BOOLEAN NOT NULL DEFAULT FALSE,
    fuente_saldo TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO tipos_proceso
    (codigo,nombre,tipo_cartera_default,requiere_conjunto,requiere_inmueble,requiere_juzgado,requiere_documento,fuente_saldo)
VALUES
    ('CUOTAS_ADMINISTRACION','Cuotas de administración','PREJURIDICO',TRUE,TRUE,FALSE,FALSE,'EXPENSAS_PH'),
    ('PAGARE','Pagaré','JURIDICO',FALSE,FALSE,TRUE,TRUE,'OBLIGACION'),
    ('LETRA_CAMBIO','Letra de cambio','JURIDICO',FALSE,FALSE,TRUE,TRUE,'OBLIGACION'),
    ('FACTURA','Factura','JURIDICO',FALSE,FALSE,TRUE,TRUE,'OBLIGACION'),
    ('OTRO','Otro','JURIDICO',FALSE,FALSE,TRUE,FALSE,'PROCESO')
ON CONFLICT (codigo) DO UPDATE SET
    nombre=EXCLUDED.nombre,
    tipo_cartera_default=EXCLUDED.tipo_cartera_default,
    requiere_conjunto=EXCLUDED.requiere_conjunto,
    requiere_inmueble=EXCLUDED.requiere_inmueble,
    requiere_juzgado=EXCLUDED.requiere_juzgado,
    requiere_documento=EXCLUDED.requiere_documento,
    fuente_saldo=EXCLUDED.fuente_saldo,
    activo=TRUE;

ALTER TABLE conjuntos_residenciales
    ADD COLUMN IF NOT EXISTS contacto_id INTEGER NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='fk_conjunto_contacto'
    ) THEN
        ALTER TABLE conjuntos_residenciales
            ADD CONSTRAINT fk_conjunto_contacto
            FOREIGN KEY (contacto_id) REFERENCES contactos(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_conjuntos_residenciales_contacto
    ON conjuntos_residenciales(contacto_id);

ALTER TABLE inmuebles_ph
    ADD COLUMN IF NOT EXISTS conjunto_id BIGINT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='fk_inmueble_conjunto'
    ) THEN
        ALTER TABLE inmuebles_ph
            ADD CONSTRAINT fk_inmueble_conjunto
            FOREIGN KEY (conjunto_id) REFERENCES conjuntos_residenciales(id) ON DELETE RESTRICT;
    END IF;
END $$;

UPDATE inmuebles_ph i
SET conjunto_id = c.id
FROM conjuntos_residenciales c
WHERE i.conjunto_id IS NULL
  AND UPPER(BTRIM(i.conjunto_residencial)) = UPPER(BTRIM(c.nombre));

CREATE INDEX IF NOT EXISTS idx_inmuebles_ph_conjunto_id
    ON inmuebles_ph(conjunto_id);

ALTER TABLE procesos
    ADD COLUMN IF NOT EXISTS tipo_proceso_id BIGINT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='fk_proceso_tipo_proceso'
    ) THEN
        ALTER TABLE procesos
            ADD CONSTRAINT fk_proceso_tipo_proceso
            FOREIGN KEY (tipo_proceso_id) REFERENCES tipos_proceso(id) ON DELETE RESTRICT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_procesos_tipo_proceso
    ON procesos(tipo_proceso_id);

UPDATE procesos p
SET tipo_proceso_id = t.id
FROM tipos_proceso t
WHERE p.tipo_proceso_id IS NULL
  AND t.codigo = CASE
      WHEN p.inmueble_id IS NOT NULL
       AND EXISTS (
           SELECT 1
           FROM inmuebles_ph i
           WHERE i.id=p.inmueble_id
             AND UPPER(BTRIM(i.conjunto_residencial)) <> 'SIN CONJUNTO'
       )
      THEN 'CUOTAS_ADMINISTRACION'
      ELSE 'OTRO'
  END;

ALTER TABLE obligaciones
    ADD COLUMN IF NOT EXISTS proceso_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS tipo_proceso_id BIGINT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='fk_obligacion_proceso'
    ) THEN
        ALTER TABLE obligaciones
            ADD CONSTRAINT fk_obligacion_proceso
            FOREIGN KEY (proceso_id) REFERENCES procesos(radicado_interno) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='fk_obligacion_tipo_proceso'
    ) THEN
        ALTER TABLE obligaciones
            ADD CONSTRAINT fk_obligacion_tipo_proceso
            FOREIGN KEY (tipo_proceso_id) REFERENCES tipos_proceso(id) ON DELETE RESTRICT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_obligaciones_proceso
    ON obligaciones(proceso_id);
CREATE INDEX IF NOT EXISTS idx_obligaciones_tipo_proceso
    ON obligaciones(tipo_proceso_id);
