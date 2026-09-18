-- Endurecimiento posterior a la separación Proceso/Obligación.
-- No crea datos financieros. Retira automatismos que ya contradicen el
-- modelo canónico y conserva defensa de actuaciones judiciales.

BEGIN;

-- Estos triggers pertenecían al catálogo antiguo donde PAGARE/FACTURA/
-- CUOTAS_ADMINISTRACION estaban mezclados con el tipo de proceso.
DROP TRIGGER IF EXISTS trg_integridad_proceso_ph ON procesos;
DROP TRIGGER IF EXISTS trg_sync_demandante_cuotas ON procesos;
DROP TRIGGER IF EXISTS trg_sync_tipo_cartera_proceso ON procesos;

DROP FUNCTION IF EXISTS fn_integridad_proceso_ph();
DROP FUNCTION IF EXISTS fn_sync_demandante_cuotas_administracion();
DROP FUNCTION IF EXISTS fn_sync_tipo_cartera_proceso();

-- El proceso_partes sigue recibiendo compatibilidad desde procesos durante
-- esta etapa; no retiramos todavía trg_sync_proceso_partes_desde_proceso.

-- La regla PREJURÍDICO sigue siendo de base de datos, pero un intento ilegal
-- debe producir un error explícito y no desaparecer silenciosamente.
CREATE OR REPLACE FUNCTION fn_bloquear_actuaciones_prejuridicas()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_tipo TEXT;
BEGIN
    SELECT tipo_cartera
      INTO v_tipo
      FROM procesos
     WHERE radicado_interno = NEW.radicado_interno
     LIMIT 1;

    IF UPPER(COALESCE(v_tipo, '')) = 'PREJURIDICO' THEN
        RAISE EXCEPTION
            'Las actuaciones judiciales no aplican a expedientes PREJURÍDICOS';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_bloquear_actuaciones_prejuridicas ON actuaciones;

CREATE TRIGGER trg_bloquear_actuaciones_prejuridicas
BEFORE INSERT ON actuaciones
FOR EACH ROW
EXECUTE FUNCTION fn_bloquear_actuaciones_prejuridicas();

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO schema_migrations(version)
VALUES ('20260918_endurecimiento_runtime')
ON CONFLICT (version) DO NOTHING;

COMMIT;
