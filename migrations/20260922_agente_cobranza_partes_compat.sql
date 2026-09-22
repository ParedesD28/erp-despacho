-- Compatibilidad lectura para Agentecobranza (WhatsApp).
-- Fase 14 eliminó la tabla física procesos_litisconsorcio; el agente aún
-- consulta ese nombre en buscar_deuda_en_neon / property_identity.
-- Esta vista proyecta las partes DEMANDADO desde proceso_partes + contactos
-- sin reintroducir columnas legacy en procesos ni obligación.

CREATE OR REPLACE VIEW public.procesos_litisconsorcio AS
SELECT
    pp.radicado_interno,
    c.identificacion AS identificacion_demandado,
    pp.es_principal,
    pp.fecha_vinculacion
FROM public.proceso_partes pp
JOIN public.contactos c
  ON c.id = pp.contacto_id
WHERE UPPER(BTRIM(pp.rol)) = 'DEMANDADO';

COMMENT ON VIEW public.procesos_litisconsorcio IS
    'Compatibilidad lectura Agentecobranza. Fuente canónica: proceso_partes + contactos (rol DEMANDADO).';

INSERT INTO public.schema_migrations(version)
VALUES ('20260922_agente_cobranza_partes_compat')
ON CONFLICT (version) DO NOTHING;
