-- Asignador de tareas por usuario (plazos Parte + tareas manuales).
-- Idempotente (IF NOT EXISTS / ON CONFLICT / DO $$ duplicate_object).
-- No duplica CRM: las tareas cuelgan del expediente y del vencimiento unificado.

-- ---------------------------------------------------------------------------
-- 1. Tabla principal
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tareas (
    id BIGSERIAL PRIMARY KEY,
    radicado_interno TEXT,
    actuacion_id BIGINT,
    vencimiento_id BIGINT,
    titulo TEXT NOT NULL,
    descripcion TEXT,
    asignado_a BIGINT,
    creado_por BIGINT,
    prioridad TEXT NOT NULL DEFAULT 'Media',
    estado TEXT NOT NULL DEFAULT 'Pendiente',
    fecha_vencimiento DATE,
    fecha_completada TIMESTAMPTZ,
    es_automatica BOOLEAN NOT NULL DEFAULT FALSE,
    origen TEXT NOT NULL DEFAULT 'manual',
    recordatorio_dias INTEGER,
    notas_internas TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    ALTER TABLE tareas
        ADD CONSTRAINT ck_tareas_prioridad
        CHECK (prioridad IN ('Baja', 'Media', 'Alta', 'Urgente'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE tareas
        ADD CONSTRAINT ck_tareas_estado
        CHECK (estado IN ('Pendiente', 'En progreso', 'Completada', 'Cancelada', 'Bloqueada'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE tareas
        ADD CONSTRAINT ck_tareas_origen
        CHECK (origen IN ('manual', 'plazo', 'desviacion', 'alerta', 'seguimiento'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE tareas
        ADD CONSTRAINT fk_tareas_actuacion
        FOREIGN KEY (actuacion_id) REFERENCES actuaciones_proceso(id) ON DELETE SET NULL;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE tareas
        ADD CONSTRAINT fk_tareas_vencimiento
        FOREIGN KEY (vencimiento_id) REFERENCES vencimientos(id) ON DELETE SET NULL;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE tareas
        ADD CONSTRAINT fk_tareas_asignado
        FOREIGN KEY (asignado_a) REFERENCES abogados(id) ON DELETE SET NULL;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN NULL;
END
$$;

DO $$
BEGIN
    ALTER TABLE tareas
        ADD CONSTRAINT fk_tareas_creador
        FOREIGN KEY (creado_por) REFERENCES abogados(id) ON DELETE SET NULL;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_table THEN NULL;
END
$$;

CREATE INDEX IF NOT EXISTS idx_tareas_asignado_estado
    ON tareas (asignado_a, estado, fecha_vencimiento);

CREATE INDEX IF NOT EXISTS idx_tareas_radicado
    ON tareas (radicado_interno, estado)
    WHERE radicado_interno IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tareas_vencimiento
    ON tareas (vencimiento_id)
    WHERE vencimiento_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tareas_origen
    ON tareas (origen)
    WHERE origen IS NOT NULL;

-- Una sola abierta por actuación/plazo (Pendiente, En progreso, Bloqueada).
CREATE UNIQUE INDEX IF NOT EXISTS uq_tareas_abierta_actuacion
    ON tareas (actuacion_id)
    WHERE actuacion_id IS NOT NULL
      AND estado IN ('Pendiente', 'En progreso', 'Bloqueada');

CREATE UNIQUE INDEX IF NOT EXISTS uq_tareas_abierta_vencimiento
    ON tareas (vencimiento_id)
    WHERE vencimiento_id IS NOT NULL
      AND estado IN ('Pendiente', 'En progreso', 'Bloqueada');

-- ---------------------------------------------------------------------------
-- 2. Historial de cambios
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tareas_historial (
    id BIGSERIAL PRIMARY KEY,
    tarea_id BIGINT NOT NULL,
    usuario_id BIGINT,
    accion TEXT NOT NULL,
    campo TEXT,
    valor_anterior TEXT,
    valor_nuevo TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    ALTER TABLE tareas_historial
        ADD CONSTRAINT fk_tareas_historial_tarea
        FOREIGN KEY (tarea_id) REFERENCES tareas(id) ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE INDEX IF NOT EXISTS idx_tareas_historial_tarea
    ON tareas_historial (tarea_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 3. Comentarios
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tareas_comentarios (
    id BIGSERIAL PRIMARY KEY,
    tarea_id BIGINT NOT NULL,
    usuario_id BIGINT,
    comentario TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    ALTER TABLE tareas_comentarios
        ADD CONSTRAINT fk_tareas_comentarios_tarea
        FOREIGN KEY (tarea_id) REFERENCES tareas(id) ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE INDEX IF NOT EXISTS idx_tareas_comentarios_tarea
    ON tareas_comentarios (tarea_id, created_at ASC);

INSERT INTO schema_migrations(version)
VALUES ('20261118_asignador_tareas')
ON CONFLICT DO NOTHING;
