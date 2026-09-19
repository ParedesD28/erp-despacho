-- Fase 15: completar obligaciones de ejecutivos históricos sin obligación.
INSERT INTO obligaciones (
    identificacion_deudor,tipo_titulo,numero_documento,capital,
    fecha_exigibilidad,estado,proceso_id,tipo_proceso_id,
    tipo_obligacion_id,acreedor_contacto_id,inmueble_id,
    capital_inicial,fuente_saldo
)
SELECT principal.identificacion,
       'CUOTAS_ADMINISTRACION',NULL,0,
       NULL,'ACTIVA',p.radicado_interno,p.tipo_proceso_id,
       t.id,acreedor.contacto_id,p.inmueble_id,
       0,t.fuente_saldo
FROM procesos p
JOIN tipos_obligacion t
  ON t.codigo='CUOTAS_ADMINISTRACION'
 AND t.activo=TRUE
JOIN LATERAL (
    SELECT pp.contacto_id
    FROM proceso_partes pp
    WHERE pp.radicado_interno=p.radicado_interno
      AND UPPER(pp.rol)='DEMANDANTE'
      AND pp.es_principal=TRUE
    LIMIT 1
) acreedor ON TRUE
JOIN LATERAL (
    SELECT c.identificacion
    FROM proceso_partes pp
    JOIN contactos c ON c.id=pp.contacto_id
    WHERE pp.radicado_interno=p.radicado_interno
      AND UPPER(pp.rol)='DEMANDADO'
    ORDER BY pp.es_principal DESC,pp.contacto_id
    LIMIT 1
) principal ON TRUE
WHERE p.radicado_interno IN ('EXP-0016','EXP-0017','EXP-0018','EXP-0020','EXP-0021')
  AND UPPER(COALESCE(p.naturaleza,''))='EJECUTIVO'
  AND p.inmueble_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM proceso_obligaciones po
      WHERE po.radicado_interno=p.radicado_interno
  );

INSERT INTO obligacion_partes (obligacion_id,contacto_id,rol,es_principal)
SELECT o.id,pp.contacto_id,'DEUDOR',pp.es_principal
FROM obligaciones o
JOIN proceso_partes pp
  ON pp.radicado_interno=o.proceso_id
 AND UPPER(pp.rol)='DEMANDADO'
WHERE o.proceso_id IN ('EXP-0016','EXP-0017','EXP-0018','EXP-0020','EXP-0021')
  AND UPPER(COALESCE(o.fuente_saldo,''))='EXPENSAS_PH'
ON CONFLICT (obligacion_id,contacto_id,rol)
DO UPDATE SET es_principal=EXCLUDED.es_principal;

INSERT INTO proceso_obligaciones (radicado_interno,obligacion_id,es_principal)
SELECT o.proceso_id,o.id,TRUE
FROM obligaciones o
WHERE o.proceso_id IN ('EXP-0016','EXP-0017','EXP-0018','EXP-0020','EXP-0021')
  AND UPPER(COALESCE(o.fuente_saldo,''))='EXPENSAS_PH'
  AND NOT EXISTS (
      SELECT 1
      FROM proceso_obligaciones po
      WHERE po.radicado_interno=o.proceso_id
        AND po.obligacion_id=o.id
  );

INSERT INTO schema_migrations(version)
VALUES ('20260919_fase15_completar_obligaciones_ejecutivos_historicos')
ON CONFLICT DO NOTHING;