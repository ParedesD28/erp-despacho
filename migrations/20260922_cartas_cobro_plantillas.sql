-- Plantillas editables de cartas de cobro (PREJURIDICO / JURIDICO).
-- Idempotente: CREATE IF NOT EXISTS + seed solo si no hay filas del sistema.

CREATE TABLE IF NOT EXISTS cartas_cobro_plantillas (
    id BIGSERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    tipo_cartera VARCHAR(20) NOT NULL
        CHECK (tipo_cartera IN ('PREJURIDICO', 'JURIDICO')),
    cuerpo TEXT NOT NULL,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    es_sistema BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_cartas_cobro_plantillas_nombre_tipo
        UNIQUE (nombre, tipo_cartera)
);

CREATE INDEX IF NOT EXISTS idx_cartas_cobro_plantillas_tipo_activo
    ON cartas_cobro_plantillas (tipo_cartera, activo);

INSERT INTO cartas_cobro_plantillas (nombre, tipo_cartera, cuerpo, activo, es_sistema)
SELECT
    'Requerimiento prejurídico estándar',
    'PREJURIDICO',
    E'Señor(a)\n{{deudor_nombre}}\nC.C. {{cedula}}\nPropietario(a) del ({{torre_apto}}) ({{conjunto}}) {{ciudad}}.\n{{atn_codeudor}}\nREF: REQUERIMIENTO DE PAGO PREJURÍDICO - {{nombre_ph}}\n\nRespetado(a) señor(a),\n\nActuando en mi calidad de apoderado legal de {{nombre_ph}}, me dirijo respetuosamente a usted con el fin de requerirle el pago de las obligaciones pendientes a cargo de la unidad ({{torre_apto}}) del conjunto {{conjunto}}, ubicado en {{ciudad}}.\n\nA la fecha, revisada la liquidación de cartera correspondiente, se advierte una mora que asciende a la suma total de {{monto}}.\n\nEl bienestar y mantenimiento de la copropiedad dependen del pago oportuno de las cuotas de administración y demás conceptos a cargo de cada propietario. Su colaboración es indispensable para preservar los servicios comunes y evitar mayores costos para la comunidad.\n\nLe otorgamos un plazo máximo hasta el {{fecha_limite}} para cancelar la totalidad de la suma adeudada o para formalizar un acuerdo de pago viable con este despacho.\n\nPara gestionar su pago, aclarar saldos o radicar propuestas de acuerdo, puede comunicarse a través de los siguientes canales:\n• Teléfono / WhatsApp: {{telefono_despacho}}\n• Correo electrónico: {{correo_despacho}}\n\nHacemos de su conocimiento que, de no recibirse el pago o una propuesta seria dentro del plazo indicado, se adelantarán las gestiones prejurídicas y jurídicas pertinentes, con cargo de intereses, costas y honorarios a que haya lugar.\n\nConfiamos en su voluntad de normalizar esta situación de manera pronta y cordial, evitando mayores inconvenientes.\n\nAtentamente,\n\n\n{{firmante_nombre}}\n{{firmante_cargo}}',
    TRUE,
    TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM cartas_cobro_plantillas
    WHERE nombre='Requerimiento prejurídico estándar'
      AND tipo_cartera='PREJURIDICO'
);

INSERT INTO cartas_cobro_plantillas (nombre, tipo_cartera, cuerpo, activo, es_sistema)
SELECT
    'Requerimiento jurídico estándar',
    'JURIDICO',
    E'Señor(a)\n{{deudor_nombre}}\nC.C. {{cedula}}\nPropietario(a) del ({{torre_apto}}) ({{conjunto}}) {{ciudad}}.\n{{atn_codeudor}}\nREF: REQUERIMIENTO DE PAGO JURÍDICO - {{nombre_ph}}\n\nRespetado(a) señor(a),\n\nActuando en mi calidad de apoderado legal de {{nombre_ph}}, me permito reiterar el requerimiento de pago de las obligaciones a cargo de la unidad ({{torre_apto}}) del conjunto {{conjunto}}, por la suma total de {{monto}}.\n\nLe otorgamos un plazo máximo hasta el {{fecha_limite}} para cancelar la totalidad adeudada o formalizar un acuerdo de pago.\n\nCanales de contacto:\n• Teléfono / WhatsApp: {{telefono_despacho}}\n• Correo electrónico: {{correo_despacho}}\n\nDe persistir el incumplimiento se continuarán las gestiones judiciales correspondientes.\n\nAtentamente,\n\n\n{{firmante_nombre}}\n{{firmante_cargo}}',
    TRUE,
    TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM cartas_cobro_plantillas
    WHERE nombre='Requerimiento jurídico estándar'
      AND tipo_cartera='JURIDICO'
);

INSERT INTO schema_migrations(version)
VALUES ('20260922_cartas_cobro_plantillas')
ON CONFLICT (version) DO NOTHING;
