-- Completar obligación canónica de procesos no-VERBAL sin proceso_obligaciones.
-- El preflight de start.py aborta el deploy si queda alguno.
-- Idempotente. Capital 0: no inventa saldo ni expensas.
-- Con inmueble -> CUOTAS_ADMINISTRACION (mismo patrón Fase 15).
-- Sin inmueble -> OTRO (p.ej. ejecutivo de familia EXP-0089).

INSERT INTO obligaciones (
    identificacion_deudor, tipo_titulo, numero_documento, capital,
    fecha_exigibilidad, estado, proceso_id, tipo_proceso_id,
    tipo_obligacion_id, acreedor_contacto_id, inmueble_id,
    capital_inicial, fuente_saldo
)
SELECT deudor.identificacion,
       t.codigo, NULL, 0,
       NULL, 'ACTIVA', p.radicado_interno, p.tipo_proceso_id,
       t.id, acreedor.contacto_id, p.inmueble_id,
       0, t.fuente_saldo
FROM procesos p
JOIN tipos_obligacion t
  ON t.activo = TRUE
 AND t.codigo = CASE
       WHEN p.inmueble_id IS NOT NULL THEN 'CUOTAS_ADMINISTRACION'
       ELSE 'OTRO'
     END
JOIN LATERAL (
    SELECT pp.contacto_id, c.identificacion
    FROM proceso_partes pp
    JOIN contactos c ON c.id = pp.contacto_id
    WHERE pp.radicado_interno = p.radicado_interno
      AND UPPER(pp.rol) = 'DEMANDADO'
    ORDER BY pp.es_principal DESC, pp.id
    LIMIT 1
) deudor ON TRUE
LEFT JOIN LATERAL (
    SELECT pp.contacto_id
    FROM proceso_partes pp
    WHERE pp.radicado_interno = p.radicado_interno
      AND UPPER(pp.rol) = 'DEMANDANTE'
    ORDER BY pp.es_principal DESC, pp.id
    LIMIT 1
) acreedor ON TRUE
WHERE UPPER(COALESCE(p.naturaleza, '')) <> 'VERBAL'
  AND NOT EXISTS (
      SELECT 1
      FROM proceso_obligaciones po
      WHERE po.radicado_interno = p.radicado_interno
  )
  AND NOT EXISTS (
      SELECT 1
      FROM obligaciones o
      WHERE o.proceso_id = p.radicado_interno
  );

INSERT INTO obligacion_partes (obligacion_id, contacto_id, rol, es_principal)
SELECT o.id,
       pp.contacto_id,
       'DEUDOR',
       (pp.id = ddo.principal_pp_id)
FROM obligaciones o
JOIN procesos p
  ON p.radicado_interno = o.proceso_id
JOIN proceso_partes pp
  ON pp.radicado_interno = o.proceso_id
 AND UPPER(pp.rol) = 'DEMANDADO'
JOIN LATERAL (
    SELECT x.id AS principal_pp_id
    FROM proceso_partes x
    WHERE x.radicado_interno = o.proceso_id
      AND UPPER(x.rol) = 'DEMANDADO'
    ORDER BY x.es_principal DESC, x.id
    LIMIT 1
) ddo ON TRUE
WHERE UPPER(COALESCE(p.naturaleza, '')) <> 'VERBAL'
  AND (
      (p.inmueble_id IS NOT NULL AND UPPER(COALESCE(o.fuente_saldo, '')) = 'EXPENSAS_PH')
      OR (p.inmueble_id IS NULL AND UPPER(COALESCE(o.fuente_saldo, '')) = 'OBLIGACION')
  )
ON CONFLICT (obligacion_id, contacto_id, rol)
DO UPDATE SET es_principal = EXCLUDED.es_principal;

INSERT INTO proceso_obligaciones (radicado_interno, obligacion_id, es_principal)
SELECT o.proceso_id, o.id, TRUE
FROM obligaciones o
JOIN procesos p
  ON p.radicado_interno = o.proceso_id
WHERE UPPER(COALESCE(p.naturaleza, '')) <> 'VERBAL'
  AND (
      (p.inmueble_id IS NOT NULL AND UPPER(COALESCE(o.fuente_saldo, '')) = 'EXPENSAS_PH')
      OR (p.inmueble_id IS NULL AND UPPER(COALESCE(o.fuente_saldo, '')) = 'OBLIGACION')
  )
  AND NOT EXISTS (
      SELECT 1
      FROM proceso_obligaciones po
      WHERE po.radicado_interno = o.proceso_id
        AND po.obligacion_id = o.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM proceso_obligaciones po
      WHERE po.radicado_interno = o.proceso_id
        AND po.es_principal = TRUE
  );

INSERT INTO schema_migrations(version)
VALUES ('20261118_completar_obligaciones_no_verbal')
ON CONFLICT DO NOTHING;
