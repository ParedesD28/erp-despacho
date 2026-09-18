-- Auditoria e integridad de procesos PH.
-- Se ejecuta como migracion explicita; no se invoca desde startup.

UPDATE inmuebles_ph i
SET conjunto_id = cr.id
FROM conjuntos_residenciales cr
WHERE i.conjunto_id IS NULL
  AND lower(trim(i.conjunto_residencial)) = lower(trim(cr.nombre));

UPDATE inmuebles_ph i
SET conjunto_residencial = cr.nombre
FROM conjuntos_residenciales cr
WHERE i.conjunto_id = cr.id
  AND i.conjunto_residencial IS DISTINCT FROM cr.nombre;

WITH partes AS (
    SELECT
        pp.radicado_interno,
        string_agg(c.identificacion, ' | ' ORDER BY pp.es_principal DESC, pp.id)
            FILTER (WHERE pp.rol='DEMANDANTE') AS ids_demandantes,
        string_agg(c.identificacion, ' | ' ORDER BY pp.es_principal DESC, pp.id)
            FILTER (WHERE pp.rol='DEMANDADO') AS ids_demandados,
        string_agg(c.nombre, ' | ' ORDER BY pp.es_principal DESC, pp.id)
            FILTER (WHERE pp.rol='DEMANDADO') AS nombres_demandados
    FROM proceso_partes pp
    JOIN contactos c ON c.id=pp.contacto_id
    GROUP BY pp.radicado_interno
)
UPDATE procesos p
SET id_cliente=COALESCE(partes.ids_demandantes,p.id_cliente),
    id_demandado=COALESCE(partes.ids_demandados,p.id_demandado),
    demandado=COALESCE(partes.nombres_demandados,p.demandado)
FROM partes
WHERE partes.radicado_interno=p.radicado_interno;

UPDATE procesos
SET naturaleza=CASE
    WHEN upper(trim(coalesce(naturaleza,''))) LIKE '%VERBAL%' THEN 'VERBAL'
    ELSE 'EJECUTIVO'
END;

UPDATE procesos
SET etapa_actual='1. Presentación de la demanda'
WHERE etapa_actual IS NULL
  AND radicado_interno <> 'EXP-0006';

-- EXP-0027 fue creado sin abogado; se asigna al abogado operativo principal
-- utilizado en los procesos nuevos de esta cartera.
UPDATE procesos
SET abogado_id=2
WHERE radicado_interno='EXP-0027'
  AND abogado_id IS NULL;

DELETE FROM procesos_litisconsorcio;

INSERT INTO procesos_litisconsorcio
    (radicado_interno,identificacion_demandado,es_principal,fecha_vinculacion)
SELECT pp.radicado_interno,c.identificacion,pp.es_principal,pp.fecha_vinculacion
FROM proceso_partes pp
JOIN contactos c ON c.id=pp.contacto_id
WHERE pp.rol='DEMANDADO';

CREATE UNIQUE INDEX IF NOT EXISTS uq_procesos_litisconsorcio_parte
ON procesos_litisconsorcio (radicado_interno,identificacion_demandado);

CREATE OR REPLACE FUNCTION fn_integridad_proceso_ph()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_codigo TEXT;
    v_conjunto_id INTEGER;
    v_contacto_juridico INTEGER;
    v_nit_juridico TEXT;
BEGIN
    SELECT codigo INTO v_codigo
    FROM tipos_proceso
    WHERE id=NEW.tipo_proceso_id;

    IF v_codigo='CUOTAS_ADMINISTRACION' THEN
        IF NEW.inmueble_id IS NULL THEN
            RAISE EXCEPTION 'CUOTAS_ADMINISTRACION requiere inmueble_id';
        END IF;

        SELECT i.conjunto_id,cr.contacto_id,ct.identificacion
        INTO v_conjunto_id,v_contacto_juridico,v_nit_juridico
        FROM inmuebles_ph i
        LEFT JOIN conjuntos_residenciales cr ON cr.id=i.conjunto_id
        LEFT JOIN contactos ct ON ct.id=cr.contacto_id
        WHERE i.id=NEW.inmueble_id;

        IF v_conjunto_id IS NULL OR v_contacto_juridico IS NULL OR v_nit_juridico IS NULL THEN
            RAISE EXCEPTION 'El inmueble de CUOTAS_ADMINISTRACION debe tener conjunto y persona juridica configurados';
        END IF;

        NEW.id_cliente:=v_nit_juridico;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_integridad_proceso_ph ON procesos;

CREATE TRIGGER trg_integridad_proceso_ph
BEFORE INSERT OR UPDATE OF tipo_proceso_id,inmueble_id,id_cliente
ON procesos
FOR EACH ROW
EXECUTE FUNCTION fn_integridad_proceso_ph();
