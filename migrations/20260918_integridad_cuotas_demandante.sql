-- Garantiza que los procesos de CUOTAS_ADMINISTRACION siempre
-- tengan como demandante la persona jurídica del conjunto del inmueble.

CREATE OR REPLACE FUNCTION fn_sync_demandante_cuotas_administracion()
RETURNS trigger
LANGUAGE plpgsql
AS E'
DECLARE
    v_codigo TEXT;
    v_identificacion TEXT;
BEGIN
    SELECT codigo INTO v_codigo FROM tipos_proceso WHERE id = NEW.tipo_proceso_id;

    IF v_codigo = ''CUOTAS_ADMINISTRACION'' THEN
        IF NEW.inmueble_id IS NULL THEN
            RAISE EXCEPTION ''Las cuotas de administración requieren inmueble_id'';
        END IF;

        SELECT ct.identificacion INTO v_identificacion
        FROM inmuebles_ph i
        JOIN conjuntos_residenciales cr ON cr.id = i.conjunto_id
        JOIN contactos ct ON ct.id = cr.contacto_id
        WHERE i.id = NEW.inmueble_id;

        IF v_identificacion IS NULL THEN
            RAISE EXCEPTION ''El inmueble % de cuotas de administración no tiene persona jurídica de conjunto configurada'', NEW.inmueble_id;
        END IF;

        NEW.id_cliente := v_identificacion;
    END IF;

    RETURN NEW;
END;
';

DROP TRIGGER IF EXISTS trg_sync_demandante_cuotas ON procesos;

CREATE TRIGGER trg_sync_demandante_cuotas
BEFORE INSERT OR UPDATE OF tipo_proceso_id, inmueble_id, id_cliente
ON procesos
FOR EACH ROW
EXECUTE FUNCTION fn_sync_demandante_cuotas_administracion();

-- Corrige cualquier expediente histórico cuyo demandante no coincida con el conjunto.
UPDATE procesos p
SET id_cliente = ct.identificacion
FROM tipos_proceso tp, inmuebles_ph i, conjuntos_residenciales cr, contactos ct
WHERE p.tipo_proceso_id = tp.id
  AND tp.codigo = ''CUOTAS_ADMINISTRACION''
  AND i.id = p.inmueble_id
  AND cr.id = i.conjunto_id
  AND ct.id = cr.contacto_id
  AND BTRIM(p.id_cliente) <> BTRIM(ct.identificacion);
