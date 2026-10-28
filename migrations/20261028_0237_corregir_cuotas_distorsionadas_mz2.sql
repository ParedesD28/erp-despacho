-- Corrige 5 cuotas ordinarias distorsionadas de Santa Clara Manzana 2
-- (conjunto_id=2) a $35.000, que es el valor oficial de la administración.
--
-- Idempotente: el UPDATE solo pega si el valor actual es EXACTAMENTE el
-- distorsionado. No toca otras filas ni otros inmuebles.
--
--   inm_id=10 (32-502)  2026-08 y 2026-09  $45.000  → $35.000
--   inm_id=14 (24-502)  2026-08 $35.001 / 2026-09 $35.002 → $35.000
--   inm_id=19 (34-402)  2026-09 $35.001 → $35.000

UPDATE expensas_ph
   SET valor_capital = 35000.00
 WHERE inmueble_id = 10
   AND concepto = 'Expensa Ordinaria'
   AND periodo_anio = 2026
   AND periodo_mes = 8
   AND valor_capital = 45000.00;

UPDATE expensas_ph
   SET valor_capital = 35000.00
 WHERE inmueble_id = 10
   AND concepto = 'Expensa Ordinaria'
   AND periodo_anio = 2026
   AND periodo_mes = 9
   AND valor_capital = 45000.00;

UPDATE expensas_ph
   SET valor_capital = 35000.00
 WHERE inmueble_id = 14
   AND concepto = 'Expensa Ordinaria'
   AND periodo_anio = 2026
   AND periodo_mes = 8
   AND valor_capital = 35001.00;

UPDATE expensas_ph
   SET valor_capital = 35000.00
 WHERE inmueble_id = 14
   AND concepto = 'Expensa Ordinaria'
   AND periodo_anio = 2026
   AND periodo_mes = 9
   AND valor_capital = 35002.00;

UPDATE expensas_ph
   SET valor_capital = 35000.00
 WHERE inmueble_id = 19
   AND concepto = 'Expensa Ordinaria'
   AND periodo_anio = 2026
   AND periodo_mes = 9
   AND valor_capital = 35001.00;
