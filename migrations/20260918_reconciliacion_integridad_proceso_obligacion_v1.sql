-- FASE 5.1: reconciliación de integridad después de Proceso + Obligación.
-- IMPORTANTE:
-- 1) NO recrea ni reaplica 20260918_proceso_obligacion_base.
-- 2) NO elimina tablas/columnas legacy.
-- 3) Solo debe ejecutarse después de verificar el marcador base.
-- 4) Está diseñada para dejar explícito qué automatismos quedan vigentes.
--
-- Antes de ejecutar en production:
--   - probar este archivo en el branch Neon aislado;
--   - revisar el resultado de cada precondición;
--   - respaldar/confirmar punto de recuperación.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM schema_migrations
        WHERE version='20260918_proceso_obligacion_base'
    ) THEN
        RAISE EXCEPTION
            'Precondición incumplida: 20260918_proceso_obligacion_base no está registrada.';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM procesos p
        JOIN tipos_proceso tp ON tp.id=p.tipo_proceso_id
        WHERE tp.codigo IN (
            'CUOTAS_ADMINISTRACION',
            'PAGARE',
            'LETRA_CAMBIO',
            'FACTURA',
            'OTRO'
        )
    ) THEN
        RAISE EXCEPTION
            'Precondición incumplida: todavía existen procesos usando tipos_proceso legacy.';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM (
            SELECT radicado_rama
            FROM procesos
            WHERE NULLIF(BTRIM(radicado_rama),'') IS NOT NULL
            GROUP BY radicado_rama
            HAVING COUNT(*) > 1
        ) d
    ) THEN
        RAISE EXCEPTION
            'Precondición incumplida: existen radicados Rama duplicados.';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM procesos
        WHERE UPPER(BTRIM(COALESCE(tipo_cartera,'')))='PREJURIDICO'
          AND (radicado_rama IS NOT NULL OR juzgado IS NOT NULL)
    ) THEN
        RAISE EXCEPTION
            'Precondición incumplida: existen procesos prejurídicos con campos judiciales.';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 1. Triggers legacy que ya no deben decidir la semántica del proceso.
--
-- La radicación canónica escribe explícitamente:
--   procedimiento -> tipos_proceso
--   cartera       -> tipo_cartera
--   estado Rama   -> estado_rama
--   partes        -> proceso_partes
--
-- Se conserva trg_bloquear_actuaciones_prejuridicas porque sí expresa una
-- regla vigente: un expediente PREJURÍDICO no puede registrar actuaciones.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_integridad_proceso_ph ON procesos;
DROP TRIGGER IF EXISTS trg_sync_demandante_cuotas ON procesos;
DROP TRIGGER IF EXISTS trg_sync_tipo_cartera_proceso ON procesos;

-- El trigger de sincronización de proceso_partes se conserva durante esta
-- fase para compatibilidad de lectura/escritura con procesos históricos.
-- Se retirará cuando termine la transición del editor estructurado.

-- ---------------------------------------------------------------------------
-- 2. Eliminar FKs duplicadas manteniendo una única política.
--
-- inmuebles_ph.contacto_id:
--   se conserva fk_inmueble_contacto (RESTRICT).
--
-- expensas_ph.inmueble_id:
--   se conserva fk_expensa_inmueble (CASCADE).
-- ---------------------------------------------------------------------------
ALTER TABLE inmuebles_ph
    DROP CONSTRAINT IF EXISTS inmuebles_ph_contacto_id_fkey;

ALTER TABLE expensas_ph
    DROP CONSTRAINT IF EXISTS expensas_ph_inmueble_id_fkey;

-- ---------------------------------------------------------------------------
-- 3. Índices canónicos requeridos por la aplicación.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_proceso_obligaciones_proceso
    ON proceso_obligaciones(radicado_interno);

CREATE INDEX IF NOT EXISTS idx_proceso_obligaciones_obligacion
    ON proceso_obligaciones(obligacion_id);

CREATE INDEX IF NOT EXISTS idx_obligaciones_deudor_contacto
    ON obligaciones(deudor_contacto_id);

CREATE INDEX IF NOT EXISTS idx_obligaciones_inmueble
    ON obligaciones(inmueble_id);

CREATE INDEX IF NOT EXISTS idx_sms_obligacion
    ON sms_cola_envios(obligacion_id,estado);

-- ---------------------------------------------------------------------------
-- 4. Registrar la migración. No se modifica el historial de migraciones
-- antiguas que no tengan evidencia suficiente de haber sido ejecutadas.
-- ---------------------------------------------------------------------------
INSERT INTO schema_migrations(version)
VALUES ('20260918_reconciliacion_integridad_proceso_obligacion_v1')
ON CONFLICT (version) DO NOTHING;

COMMIT;
