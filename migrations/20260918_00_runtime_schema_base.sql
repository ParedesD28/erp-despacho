-- Estructuras históricamente creadas por servicios Python.
-- Desde esta migración, el runtime solo verifica su existencia.

BEGIN;

CREATE TABLE IF NOT EXISTS proceso_partes (
    id BIGSERIAL PRIMARY KEY,
    radicado_interno TEXT NOT NULL,
    contacto_id INTEGER NOT NULL,
    rol TEXT NOT NULL,
    es_principal BOOLEAN NOT NULL DEFAULT FALSE,
    fecha_vinculacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (radicado_interno, contacto_id, rol),
    FOREIGN KEY (radicado_interno) REFERENCES procesos(radicado_interno) ON DELETE CASCADE,
    FOREIGN KEY (contacto_id) REFERENCES contactos(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_proceso_partes_radicado_rol
    ON proceso_partes(radicado_interno, rol);
CREATE INDEX IF NOT EXISTS idx_proceso_partes_contacto_rol
    ON proceso_partes(contacto_id, rol);

CREATE UNIQUE INDEX IF NOT EXISTS uq_contactos_identificacion_idx
    ON contactos(identificacion);

CREATE TABLE IF NOT EXISTS gestiones_crm (
    id BIGSERIAL PRIMARY KEY,
    inmueble_id INTEGER NULL,
    identificacion_deudor TEXT NULL,
    tipo_contacto TEXT NULL,
    resumen TEXT NOT NULL,
    promesa_pago_fecha DATE NULL,
    fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    usuario TEXT NOT NULL DEFAULT 'ERP',
    anulado BOOLEAN NOT NULL DEFAULT FALSE,
    estado TEXT NOT NULL DEFAULT 'ACTIVO',
    radicado_interno TEXT NULL,
    obligacion_id INTEGER NULL
);

CREATE INDEX IF NOT EXISTS idx_gestiones_crm_radicado
    ON gestiones_crm(radicado_interno);
CREATE INDEX IF NOT EXISTS idx_gestiones_crm_inmueble
    ON gestiones_crm(inmueble_id);
CREATE INDEX IF NOT EXISTS idx_gestiones_crm_obligacion
    ON gestiones_crm(obligacion_id);

CREATE TABLE IF NOT EXISTS vencimientos (
    id BIGSERIAL PRIMARY KEY,
    radicado_interno TEXT NULL,
    titulo TEXT NULL,
    fecha_vencimiento TEXT NULL,
    estado TEXT NULL,
    observaciones TEXT NULL,
    completado BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tipo TEXT NULL,
    valor NUMERIC NULL,
    inmueble_id INTEGER NULL,
    anulado BOOLEAN NOT NULL DEFAULT FALSE,
    categoria TEXT NOT NULL DEFAULT 'TERMINO',
    abogado_id TEXT NULL,
    obligacion_id INTEGER NULL
);

CREATE INDEX IF NOT EXISTS idx_vencimientos_fecha_estado
    ON vencimientos(fecha_vencimiento, completado, anulado);
CREATE INDEX IF NOT EXISTS idx_vencimientos_radicado
    ON vencimientos(radicado_interno);
CREATE INDEX IF NOT EXISTS idx_vencimientos_obligacion
    ON vencimientos(obligacion_id);

ALTER TABLE acuerdos_pago
    ADD COLUMN IF NOT EXISTS abogado_id TEXT,
    ADD COLUMN IF NOT EXISTS frecuencia TEXT NOT NULL DEFAULT 'MENSUAL',
    ADD COLUMN IF NOT EXISTS obligacion_id INTEGER NULL;

CREATE TABLE IF NOT EXISTS acuerdos_pago_cuotas (
    id BIGSERIAL PRIMARY KEY,
    acuerdo_id BIGINT NOT NULL REFERENCES acuerdos_pago(id) ON DELETE CASCADE,
    numero_cuota INTEGER NOT NULL,
    fecha_vencimiento DATE NOT NULL,
    valor_cuota NUMERIC(14,2) NOT NULL DEFAULT 0,
    estado TEXT NOT NULL DEFAULT 'PENDIENTE',
    anulado BOOLEAN NOT NULL DEFAULT FALSE,
    abogado_id TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(acuerdo_id, numero_cuota)
);

CREATE INDEX IF NOT EXISTS idx_acuerdos_pago_cuotas_fecha
    ON acuerdos_pago_cuotas(fecha_vencimiento, estado, anulado);
CREATE INDEX IF NOT EXISTS idx_acuerdos_pago_obligacion
    ON acuerdos_pago(obligacion_id);

CREATE TABLE IF NOT EXISTS agenda_auditoria (
    id BIGSERIAL PRIMARY KEY,
    fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    abogado_id TEXT NULL,
    abogado_nombre TEXT NOT NULL DEFAULT 'Sistema',
    accion TEXT NOT NULL,
    tipo TEXT NOT NULL,
    registro_id BIGINT NULL,
    radicado_interno TEXT NULL,
    identificacion_deudor TEXT NULL,
    nombre_deudor TEXT NULL,
    inmueble_id INTEGER NULL,
    detalle TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_agenda_auditoria_fecha
    ON agenda_auditoria(fecha DESC);
CREATE INDEX IF NOT EXISTS idx_agenda_auditoria_abogado
    ON agenda_auditoria(abogado_id, fecha DESC);

CREATE TABLE IF NOT EXISTS inmueble_propietarios (
    id BIGSERIAL PRIMARY KEY,
    inmueble_id INTEGER NOT NULL,
    contacto_id INTEGER NOT NULL,
    es_principal BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(inmueble_id, contacto_id),
    FOREIGN KEY (inmueble_id) REFERENCES inmuebles_ph(id) ON DELETE CASCADE,
    FOREIGN KEY (contacto_id) REFERENCES contactos(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_inmueble_propietarios_inmueble
    ON inmueble_propietarios(inmueble_id);
CREATE INDEX IF NOT EXISTS idx_inmueble_propietarios_contacto
    ON inmueble_propietarios(contacto_id);

CREATE TABLE IF NOT EXISTS sms_plantillas (
    id BIGSERIAL PRIMARY KEY,
    nombre VARCHAR(100) NOT NULL,
    tipo VARCHAR(50) NOT NULL UNIQUE,
    cuerpo_template TEXT NOT NULL,
    es_predeterminada BOOLEAN NOT NULL DEFAULT FALSE,
    actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO sms_plantillas(nombre,tipo,cuerpo_template,es_predeterminada)
VALUES
(
    'Acuerdo Prejudicial Amistoso',
    'PREJUDICIAL',
    'Prejuridico: {nombre}, registra mora de $' || '{saldo} en {conjunto} {unidad}. Evite cobro judicial y acuerde su pago al WhatsApp {telefono_wa}.',
    TRUE
),
(
    'Aviso de Inicio de Cobro Jurídico',
    'COBRO_JURIDICO',
    'Aviso Juridico: {nombre}, inicio de proceso ejecutivo por mora de $' || '{saldo} en {conjunto} {unidad}. Evite embargo y concilie al WhatsApp {telefono_wa}.',
    FALSE
),
(
    'Alerta de Mandamiento de Pago',
    'MANDAMIENTO',
    'Urgente: {nombre}, mandamiento de pago en tramite para {conjunto} {unidad} ($' || '{saldo}). Comuniquese al WhatsApp {telefono_wa} antes de medidas cautelares.',
    FALSE
)
ON CONFLICT (tipo) DO NOTHING;

ALTER TABLE sms_cola_envios
    ADD COLUMN IF NOT EXISTS obligacion_id INTEGER NULL,
    ADD COLUMN IF NOT EXISTS saldo_fuente VARCHAR(100) NULL,
    ADD COLUMN IF NOT EXISTS saldo_verificado BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS saldo_calculado_en TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS mensaje_template TEXT NULL,
    ADD COLUMN IF NOT EXISTS contacto_id INTEGER NULL,
    ADD COLUMN IF NOT EXISTS fecha_proceso TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS processing_token VARCHAR(255) NULL,
    ADD COLUMN IF NOT EXISTS crm_auditado BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS crm_auditoria_fecha TIMESTAMP NULL;

CREATE INDEX IF NOT EXISTS idx_sms_cola_estado
    ON sms_cola_envios(estado);
CREATE INDEX IF NOT EXISTS idx_sms_cola_proceso
    ON sms_cola_envios(estado, fecha_proceso);
CREATE INDEX IF NOT EXISTS idx_sms_cola_telefono
    ON sms_cola_envios(telefono);
CREATE INDEX IF NOT EXISTS idx_sms_cola_contacto_fecha
    ON sms_cola_envios(contacto_id, fecha_envio);
CREATE INDEX IF NOT EXISTS idx_sms_cola_obligacion
    ON sms_cola_envios(obligacion_id, estado);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO schema_migrations(version)
VALUES ('20260918_runtime_schema_base')
ON CONFLICT (version) DO NOTHING;

COMMIT;
