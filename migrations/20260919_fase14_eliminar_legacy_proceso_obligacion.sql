-- FASE 14 — Eliminación física de compatibilidad legacy Proceso/Obligación
-- Esta migración se ejecuta únicamente como parte de la transición controlada.
-- Toda la lógica nueva ya consume proceso_partes y obligacion_partes.

DROP TRIGGER IF EXISTS trg_sync_proceso_partes_desde_proceso ON public.procesos;
DROP FUNCTION IF EXISTS public.fn_sync_proceso_partes_desde_proceso();

DROP TRIGGER IF EXISTS trg_integridad_proceso_ph ON public.procesos;
DROP FUNCTION IF EXISTS public.fn_integridad_proceso_ph();

DROP TRIGGER IF EXISTS trg_sync_demandante_cuotas ON public.procesos;
DROP FUNCTION IF EXISTS public.fn_sync_demandante_cuotas_administracion();

DROP TABLE IF EXISTS public.procesos_litisconsorcio;

ALTER TABLE public.procesos
    DROP COLUMN IF EXISTS id_cliente,
    DROP COLUMN IF EXISTS demandante,
    DROP COLUMN IF EXISTS id_demandado,
    DROP COLUMN IF EXISTS demandado;

ALTER TABLE public.obligaciones
    DROP CONSTRAINT IF EXISTS fk_obligacion_deudor_contacto;

DROP INDEX IF EXISTS public.idx_obligaciones_deudor_contacto;

ALTER TABLE public.obligaciones
    DROP COLUMN IF EXISTS deudor_contacto_id;

INSERT INTO public.schema_migrations(version)
VALUES ('20260919_fase14_eliminar_legacy_proceso_obligacion')
ON CONFLICT (version) DO NOTHING;
