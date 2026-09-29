-- Normaliza inmuebles_ph.torre_apto al formato canónico `torre-apto` sin ceros
-- a la izquierda (02-042 → 2-42, TORRE 1 APTO 201 → 1-201).
--
-- IDEMPOTENTE y seguro de re-ejecutar.
-- NO ejecutar a ciegas en producción Neon sin dry-run previo
-- (ver scripts/normalizar_torre_apto.py --dry-run).
--
-- Criterio:
--   - Extrae tokens numéricos de etiquetas TORRE/APTO/BLOQUE/etc.
--   - Une como bloque-apto; strip leading zeros por segmento.
--   - Solo actualiza filas cuyo valor normalizado ≠ al actual.
--   - Si tras normalizar chocaría UNIQUE (conjunto_id, torre_apto) implícito
--     por duplicados semánticos, la fila se lista en el SELECT de conflictos
--     y NO se actualiza automáticamente (revisión manual).

-- 1) Dry-run: filas que cambiarían
WITH candidatos AS (
    SELECT
        id,
        conjunto_id,
        torre_apto AS actual,
        -- Heurística SQL alineada al helper Python clave_canonica_unidad:
        -- quita ruido textual, toma 2 primeros números, strip ceros.
        (
            SELECT string_agg(tok, '-' ORDER BY ord)
            FROM (
                SELECT
                    CASE
                        WHEN tok ~ '^[0-9]+$' THEN (tok::bigint)::text
                        WHEN tok ~ '^[A-Za-z]+[0-9]+$' THEN
                            regexp_replace(tok, '^([A-Za-z]+)0*([0-9]+)$', '\1\2')
                        ELSE tok
                    END AS tok,
                    ord
                FROM (
                    SELECT
                        upper(m[1]) AS tok,
                        row_number() OVER () AS ord
                    FROM regexp_matches(
                        regexp_replace(
                            regexp_replace(
                                upper(coalesce(torre_apto, '')),
                                '\y(TORRE|BLOQUE|BL|MZ|MANZANA|APTO|APARTAMENTO|APT|AP|NRO|NUMERO|NÚMERO|NO|N0|N)\y',
                                ' ',
                                'gi'
                            ),
                            '[^A-Z0-9]+',
                            ' ',
                            'g'
                        ),
                        '([A-Z]*[0-9]+[A-Z]*)',
                        'g'
                    ) AS m
                ) t
                ORDER BY ord
                LIMIT 2
            ) u
        ) AS canonico
    FROM inmuebles_ph
    WHERE coalesce(btrim(torre_apto), '') <> ''
)
SELECT id, conjunto_id, actual, canonico
FROM candidatos
WHERE canonico IS NOT NULL
  AND canonico <> ''
  AND canonico IS DISTINCT FROM actual
ORDER BY id;

-- 2) Conflictos: dos filas del mismo conjunto colapsarían a la misma clave
-- WITH ... (reutilizar candidatos) → revisar manualmente antes del UPDATE.

-- 3) UPDATE (descomentar tras dry-run y revisión de conflictos):
-- UPDATE inmuebles_ph i
-- SET torre_apto = c.canonico
-- FROM (
--     /* mismo CTE candidatos filtrado sin conflictos */
-- ) c
-- WHERE i.id = c.id;
--
-- Preferible: python scripts/normalizar_torre_apto.py [--dry-run] [--apply]
