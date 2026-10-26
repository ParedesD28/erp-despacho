-- BASELINE del esquema ERP en la version 20261006_usuario_conjuntos_portal_ph.
-- Generado con pg_dump --schema-only (PostgreSQL 18, Neon) + catalogos semilla
-- (tipos_proceso, tipos_obligacion, perfiles).
--
-- Solo lo ejecuta migrate.py sobre una BD VACIA (sin tabla procesos). En BD con
-- esquema existente (Neon/produccion) NUNCA se ejecuta: las migraciones legacy
-- anteriores a esta version quedan sustituidas por este baseline.



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', 'public', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

CREATE FUNCTION public.fn_bloquear_actuaciones_prejuridicas() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
            DECLARE
                v_tipo TEXT;
            BEGIN
                SELECT tipo_cartera INTO v_tipo
                  FROM procesos
                 WHERE radicado_interno = NEW.radicado_interno
                 LIMIT 1;

                IF UPPER(COALESCE(v_tipo, '')) = 'PREJURIDICO' THEN
                    RETURN NULL;
                END IF;

                RETURN NEW;
            END;
            $$;

CREATE FUNCTION public.fn_sync_tipo_cartera_proceso() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF UPPER(TRIM(COALESCE(NEW.radicado_rama, ''))) = 'PREJURIDICO'
               OR UPPER(TRIM(COALESCE(NEW.radicado_rama, ''))) LIKE 'PREJURIDICO-%'
               OR UPPER(TRIM(COALESCE(NEW.radicado_rama, ''))) LIKE 'PREJ-%'
            THEN
                NEW.tipo_cartera := 'PREJURIDICO';
            ELSIF NULLIF(TRIM(COALESCE(NEW.radicado_rama, '')), '') IS NOT NULL THEN
                NEW.tipo_cartera := 'JURIDICO';
            END IF;
            RETURN NEW;
        END;
        $$;

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE public.abogados (
    id integer NOT NULL,
    nombre text,
    email text,
    telefono text,
    rol text DEFAULT 'Abogado'::text,
    password text DEFAULT '1234'::text,
    perfil_id integer,
    activo boolean DEFAULT true NOT NULL
);

CREATE SEQUENCE public.abogados_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.abogados_id_seq OWNED BY public.abogados.id;

CREATE TABLE public.actuaciones (
    id integer NOT NULL,
    radicado_interno text,
    fecha text,
    etapa text,
    descripcion text,
    usuario text,
    tipificacion_sugerida text,
    tipificacion_definitiva text,
    revisado boolean DEFAULT false
);

CREATE SEQUENCE public.actuaciones_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.actuaciones_id_seq OWNED BY public.actuaciones.id;

CREATE TABLE public.acuerdos_pago (
    id bigint NOT NULL,
    inmueble_id integer,
    identificacion_deudor text NOT NULL,
    nombre_deudor text,
    telefono text,
    valor_acordado numeric(14,2) DEFAULT 0 NOT NULL,
    numero_cuotas integer DEFAULT 1 NOT NULL,
    cuota_actual integer DEFAULT 1 NOT NULL,
    fecha_compromiso date NOT NULL,
    estado text DEFAULT 'PENDIENTE'::text NOT NULL,
    origen text DEFAULT 'ROBOT_IA'::text NOT NULL,
    observaciones text,
    recordatorio_previo_enviado boolean DEFAULT false NOT NULL,
    recordatorio_dia_enviado boolean DEFAULT false NOT NULL,
    recordatorio_mora_enviado boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    abogado_id text,
    frecuencia text DEFAULT 'MENSUAL'::text NOT NULL,
    obligacion_id integer
);

CREATE TABLE public.acuerdos_pago_cuotas (
    id bigint NOT NULL,
    acuerdo_id bigint NOT NULL,
    numero_cuota integer NOT NULL,
    fecha_vencimiento date NOT NULL,
    valor_cuota numeric(14,2) DEFAULT 0 NOT NULL,
    estado text DEFAULT 'PENDIENTE'::text NOT NULL,
    anulado boolean DEFAULT false NOT NULL,
    abogado_id text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE public.acuerdos_pago_cuotas_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.acuerdos_pago_cuotas_id_seq OWNED BY public.acuerdos_pago_cuotas.id;

CREATE SEQUENCE public.acuerdos_pago_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.acuerdos_pago_id_seq OWNED BY public.acuerdos_pago.id;

CREATE TABLE public.agenda_auditoria (
    id bigint NOT NULL,
    fecha timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    abogado_id text,
    abogado_nombre text DEFAULT 'Sistema'::text NOT NULL,
    accion text NOT NULL,
    tipo text NOT NULL,
    registro_id bigint,
    radicado_interno text,
    identificacion_deudor text,
    nombre_deudor text,
    inmueble_id integer,
    detalle text
);

CREATE SEQUENCE public.agenda_auditoria_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.agenda_auditoria_id_seq OWNED BY public.agenda_auditoria.id;

CREATE TABLE public.auditoria (
    id integer NOT NULL,
    fecha timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    usuario text,
    accion text,
    tabla text,
    registro_id text,
    estado_anterior text,
    estado_nuevo text,
    datos_json text
);

CREATE TABLE public.auditoria_chats (
    id integer NOT NULL,
    numero_telefono text NOT NULL,
    remitente text NOT NULL,
    mensaje text NOT NULL,
    fecha_hora timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.auditoria_chats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.auditoria_chats_id_seq OWNED BY public.auditoria_chats.id;

CREATE SEQUENCE public.auditoria_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.auditoria_id_seq OWNED BY public.auditoria.id;

CREATE TABLE public.bot_estado_persistente (
    numero_telefono text NOT NULL,
    memoria_chat jsonb DEFAULT '[]'::jsonb NOT NULL,
    obligacion_activa jsonb,
    actualizado_en timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.bot_eventos_procesados (
    message_id text NOT NULL,
    numero_telefono text,
    recibido_en timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.cartas_cobro_plantillas (
    id bigint NOT NULL,
    nombre text NOT NULL,
    tipo_cartera character varying(20) NOT NULL,
    cuerpo text NOT NULL,
    activo boolean DEFAULT true NOT NULL,
    es_sistema boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cartas_cobro_plantillas_tipo_cartera_check CHECK (((tipo_cartera)::text = ANY ((ARRAY['PREJURIDICO'::character varying, 'JURIDICO'::character varying])::text[])))
);

CREATE SEQUENCE public.cartas_cobro_plantillas_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.cartas_cobro_plantillas_id_seq OWNED BY public.cartas_cobro_plantillas.id;

CREATE TABLE public.cartera_totales_snapshot (
    id smallint DEFAULT 1 NOT NULL,
    capital numeric(18,2) DEFAULT 0 NOT NULL,
    intereses numeric(18,2) DEFAULT 0 NOT NULL,
    honorarios numeric(18,2) DEFAULT 0 NOT NULL,
    valor_cartera numeric(18,2) DEFAULT 0 NOT NULL,
    obligaciones_incluidas integer DEFAULT 0 NOT NULL,
    obligaciones_sin_deuda integer DEFAULT 0 NOT NULL,
    obligaciones_error integer DEFAULT 0 NOT NULL,
    fecha_corte date,
    calculado_en timestamp with time zone DEFAULT now() NOT NULL,
    actualizado_por text,
    CONSTRAINT cartera_totales_snapshot_id_check CHECK ((id = 1))
);

CREATE TABLE public.configuracion (
    clave text NOT NULL,
    valor integer
);

CREATE TABLE public.conjuntos_residenciales (
    id bigint NOT NULL,
    nombre character varying(255) NOT NULL,
    activo boolean DEFAULT true NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    contacto_id integer
);

CREATE SEQUENCE public.conjuntos_residenciales_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.conjuntos_residenciales_id_seq OWNED BY public.conjuntos_residenciales.id;

CREATE TABLE public.contactos (
    id integer NOT NULL,
    nombre text,
    tipo text,
    telefono text,
    email text,
    direccion text,
    ciudad text,
    identificacion text
);

CREATE SEQUENCE public.contactos_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.contactos_id_seq OWNED BY public.contactos.id;

CREATE TABLE public.control_agente (
    id bigint NOT NULL,
    conversacion_id bigint NOT NULL,
    usuario text NOT NULL,
    accion text NOT NULL,
    fecha timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE SEQUENCE public.control_agente_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.control_agente_id_seq OWNED BY public.control_agente.id;

CREATE TABLE public.conversaciones_agente (
    id bigint NOT NULL,
    telefono text NOT NULL,
    identificacion text,
    estado text DEFAULT 'ACTIVA'::text NOT NULL,
    modo_actual text DEFAULT 'AGENTE'::text NOT NULL,
    usuario_humano text,
    fecha_inicio timestamp with time zone DEFAULT now() NOT NULL,
    fecha_ultima_actividad timestamp with time zone DEFAULT now() NOT NULL,
    tomada_en timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE SEQUENCE public.conversaciones_agente_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.conversaciones_agente_id_seq OWNED BY public.conversaciones_agente.id;

CREATE TABLE public.data_migration_exceptions (
    id bigint NOT NULL,
    migration_version text NOT NULL,
    entity_type text NOT NULL,
    entity_key text NOT NULL,
    reason text NOT NULL,
    status text DEFAULT 'PENDING_REVIEW'::text NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    resolved_at timestamp without time zone,
    notes text,
    CONSTRAINT ck_data_migration_exception_status CHECK ((status = ANY (ARRAY['PENDING_REVIEW'::text, 'RESOLVED'::text, 'IGNORED'::text])))
);

CREATE SEQUENCE public.data_migration_exceptions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.data_migration_exceptions_id_seq OWNED BY public.data_migration_exceptions.id;

CREATE TABLE public.expediente_ediciones (
    id bigint NOT NULL,
    radicado_interno text NOT NULL,
    usuario text,
    fecha timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    accion text NOT NULL,
    antes jsonb,
    despues jsonb
);

CREATE SEQUENCE public.expediente_ediciones_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.expediente_ediciones_id_seq OWNED BY public.expediente_ediciones.id;

CREATE TABLE public.expensas_ph (
    id integer NOT NULL,
    inmueble_id integer,
    concepto character varying(100) NOT NULL,
    periodo_mes integer NOT NULL,
    periodo_anio integer NOT NULL,
    valor_capital numeric(12,2) NOT NULL,
    fecha_vencimiento date NOT NULL,
    estado character varying(20) DEFAULT 'En Mora'::character varying,
    obligation_id integer
);

CREATE SEQUENCE public.expensas_ph_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.expensas_ph_id_seq OWNED BY public.expensas_ph.id;

CREATE TABLE public.gastos (
    id integer NOT NULL,
    radicado_interno text,
    fecha text,
    concepto text,
    valor numeric
);

CREATE SEQUENCE public.gastos_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.gastos_id_seq OWNED BY public.gastos.id;

CREATE TABLE public.gestiones_cartera (
    id integer NOT NULL,
    id_obligacion integer,
    fecha_hora timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    tipo_contacto text,
    resumen text,
    promesa_pago_fecha text,
    usuario text,
    identificacion_deudor text,
    activo boolean DEFAULT true
);

CREATE SEQUENCE public.gestiones_cartera_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.gestiones_cartera_id_seq OWNED BY public.gestiones_cartera.id;

CREATE TABLE public.gestiones_crm (
    id bigint NOT NULL,
    inmueble_id integer,
    identificacion_deudor text,
    tipo_contacto text,
    resumen text NOT NULL,
    promesa_pago_fecha date,
    fecha timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    usuario text DEFAULT 'ERP'::text NOT NULL,
    anulado boolean DEFAULT false NOT NULL,
    estado text DEFAULT 'ACTIVO'::text NOT NULL,
    radicado_interno text,
    obligacion_id integer
);

CREATE SEQUENCE public.gestiones_crm_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.gestiones_crm_id_seq OWNED BY public.gestiones_crm.id;

CREATE TABLE public.historico_tasas (
    id integer NOT NULL,
    anio integer NOT NULL,
    mes integer NOT NULL,
    tasa_efectiva_anual numeric(5,2) NOT NULL,
    tasa_mora_mensual numeric(5,2) GENERATED ALWAYS AS (((tasa_efectiva_anual * 1.5) / (12)::numeric)) STORED,
    tasa_usura_ea numeric(18,10),
    modalidad character varying(120),
    vigencia_desde date,
    vigencia_hasta date,
    fuente text,
    consultado_en timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    ibc_ea numeric(18,10),
    validado_sfc boolean DEFAULT false
);

CREATE SEQUENCE public.historico_tasas_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.historico_tasas_id_seq OWNED BY public.historico_tasas.id;

CREATE TABLE public.inmueble_propietarios (
    id bigint NOT NULL,
    inmueble_id integer NOT NULL,
    contacto_id integer NOT NULL,
    es_principal boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE public.inmueble_propietarios_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.inmueble_propietarios_id_seq OWNED BY public.inmueble_propietarios.id;

CREATE TABLE public.inmuebles_ph (
    id integer NOT NULL,
    contacto_id integer,
    conjunto_residencial character varying(150) NOT NULL,
    torre_apto character varying(50) NOT NULL,
    conjunto_id bigint
);

CREATE SEQUENCE public.inmuebles_ph_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.inmuebles_ph_id_seq OWNED BY public.inmuebles_ph.id;

CREATE TABLE public.mensajes_agente (
    id bigint NOT NULL,
    conversacion_id bigint NOT NULL,
    direccion text NOT NULL,
    autor text NOT NULL,
    contenido text,
    mensaje_meta_id text,
    tipo_mensaje text,
    fecha timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE SEQUENCE public.mensajes_agente_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.mensajes_agente_id_seq OWNED BY public.mensajes_agente.id;

CREATE TABLE public.obligacion_movimientos (
    id bigint NOT NULL,
    obligacion_id integer NOT NULL,
    tipo text NOT NULL,
    concepto text NOT NULL,
    valor numeric(14,2) NOT NULL,
    fecha date DEFAULT CURRENT_DATE NOT NULL,
    observaciones text,
    recaudo_id integer,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_movimiento_tipo CHECK ((tipo = ANY (ARRAY['CARGO'::text, 'ABONO'::text, 'AJUSTE_CARGO'::text, 'AJUSTE_ABONO'::text]))),
    CONSTRAINT obligacion_movimientos_valor_check CHECK ((valor > (0)::numeric))
);

CREATE SEQUENCE public.obligacion_movimientos_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.obligacion_movimientos_id_seq OWNED BY public.obligacion_movimientos.id;

CREATE TABLE public.obligacion_partes (
    id bigint NOT NULL,
    obligacion_id bigint NOT NULL,
    contacto_id bigint NOT NULL,
    rol character varying(30) DEFAULT 'DEUDOR'::character varying NOT NULL,
    es_principal boolean DEFAULT false NOT NULL,
    fecha_vinculacion timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_obligacion_partes_rol CHECK (((rol)::text = ANY ((ARRAY['DEUDOR'::character varying, 'CODEUDOR'::character varying, 'GARANTE'::character varying])::text[])))
);

CREATE SEQUENCE public.obligacion_partes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.obligacion_partes_id_seq OWNED BY public.obligacion_partes.id;

CREATE TABLE public.obligaciones (
    id integer NOT NULL,
    identificacion_deudor text,
    tipo_titulo text,
    capital numeric,
    fecha_exigibilidad text,
    estado text DEFAULT 'En Mora'::text,
    proceso_id text,
    tipo_proceso_id bigint,
    numero_documento text,
    tipo_obligacion_id bigint,
    acreedor_contacto_id integer,
    inmueble_id integer,
    capital_inicial numeric(14,2) DEFAULT 0,
    fuente_saldo text,
    CONSTRAINT ck_obligaciones_capital_inicial_no_negativo CHECK ((capital_inicial >= (0)::numeric))
);

CREATE SEQUENCE public.obligaciones_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.obligaciones_id_seq OWNED BY public.obligaciones.id;

CREATE TABLE public.perfiles (
    id integer NOT NULL,
    codigo character varying(32) NOT NULL,
    nombre text NOT NULL,
    descripcion text,
    activo boolean DEFAULT true NOT NULL
);

CREATE SEQUENCE public.perfiles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.perfiles_id_seq OWNED BY public.perfiles.id;

CREATE TABLE public.proceso_obligaciones (
    id bigint NOT NULL,
    radicado_interno text NOT NULL,
    obligacion_id integer NOT NULL,
    es_principal boolean DEFAULT true NOT NULL,
    fecha_vinculacion timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE public.proceso_obligaciones_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.proceso_obligaciones_id_seq OWNED BY public.proceso_obligaciones.id;

CREATE TABLE public.proceso_partes (
    id bigint NOT NULL,
    radicado_interno text NOT NULL,
    contacto_id integer NOT NULL,
    rol text NOT NULL,
    es_principal boolean DEFAULT false NOT NULL,
    fecha_vinculacion timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE public.proceso_partes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.proceso_partes_id_seq OWNED BY public.proceso_partes.id;

CREATE TABLE public.procesos (
    radicado_interno text NOT NULL,
    radicado_rama text,
    naturaleza text NOT NULL,
    juzgado text,
    etapa_actual text,
    estado text DEFAULT 'Activo'::text,
    pretensiones numeric,
    medidas_cautelares text,
    abogado_id integer,
    inmueble_id integer,
    tipo_cartera character varying(20) NOT NULL,
    tipo_proceso_id bigint NOT NULL,
    estado_rama text DEFAULT 'NO_APLICA'::text NOT NULL,
    CONSTRAINT ck_procesos_estado_rama CHECK ((estado_rama = ANY (ARRAY['NO_APLICA'::text, 'PENDIENTE_REPARTO'::text, 'ASIGNADO'::text]))),
    CONSTRAINT ck_procesos_naturaleza CHECK ((upper(btrim(naturaleza)) = ANY (ARRAY['EJECUTIVO'::text, 'VERBAL'::text]))),
    CONSTRAINT ck_procesos_prejuridico_sin_judicial CHECK (((upper(btrim((tipo_cartera)::text)) <> 'PREJURIDICO'::text) OR ((radicado_rama IS NULL) AND (juzgado IS NULL)))),
    CONSTRAINT ck_procesos_rama_real CHECK (((radicado_rama IS NULL) OR (upper(btrim(radicado_rama)) <> ALL (ARRAY['EN REPARTO'::text, 'PREJURIDICO'::text, 'PRE-JURIDICO'::text])))),
    CONSTRAINT ck_procesos_tipo_cartera CHECK ((upper(btrim((tipo_cartera)::text)) = ANY (ARRAY['JURIDICO'::text, 'PREJURIDICO'::text])))
);

CREATE SEQUENCE public.radicado_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

CREATE TABLE public.recaudos_contabilidad (
    id integer NOT NULL,
    inmueble_id integer,
    identificacion_deudor character varying(50) NOT NULL,
    nombre_deudor character varying(255),
    fecha_pago date NOT NULL,
    valor_total numeric(14,2) NOT NULL,
    abono_intereses numeric(14,2) DEFAULT 0.0,
    abono_capital numeric(14,2) DEFAULT 0.0,
    honorarios_cobro numeric(14,2) DEFAULT 0.0,
    gastos_reembolso numeric(14,2) DEFAULT 0.0,
    banco_origen character varying(100),
    referencia_transaccion character varying(120),
    cuenta_destino character varying(100),
    soporte_url text,
    estado_conciliacion character varying(30) DEFAULT 'PENDIENTE_CONCILIACION'::character varying,
    aprobado_por character varying(100),
    fecha_aprobacion timestamp without time zone,
    observaciones text,
    creado_en timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    obligacion_id integer,
    CONSTRAINT recaudos_contabilidad_valor_total_check CHECK ((valor_total > (0)::numeric))
);

CREATE SEQUENCE public.recaudos_contabilidad_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.recaudos_contabilidad_id_seq OWNED BY public.recaudos_contabilidad.id;

CREATE TABLE public.schema_migrations (
    version text NOT NULL,
    applied_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.sms_cola_envios (
    id bigint NOT NULL,
    inmueble_id integer,
    identificacion character varying(50) NOT NULL,
    nombre character varying(255),
    conjunto_residencial character varying(255),
    torre_apto character varying(100),
    telefono character varying(20) NOT NULL,
    saldo_calculado numeric(14,2) DEFAULT 0.0,
    mensaje_texto text NOT NULL,
    tipo_campana character varying(50) DEFAULT 'PREJUDICIAL'::character varying,
    estado character varying(30) DEFAULT 'PENDIENTE'::character varying,
    fecha_creacion timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    fecha_envio timestamp without time zone,
    error_detalle text,
    contacto_id integer,
    fecha_proceso timestamp without time zone,
    processing_token character varying(128),
    crm_auditado boolean DEFAULT false NOT NULL,
    crm_auditoria_fecha timestamp without time zone,
    saldo_fuente character varying(80),
    saldo_verificado boolean DEFAULT false NOT NULL,
    saldo_calculado_en timestamp without time zone,
    mensaje_template text,
    obligacion_id integer
);

CREATE SEQUENCE public.sms_cola_envios_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.sms_cola_envios_id_seq OWNED BY public.sms_cola_envios.id;

CREATE TABLE public.sms_plantillas (
    id integer NOT NULL,
    nombre character varying(100) NOT NULL,
    tipo character varying(50) NOT NULL,
    cuerpo_template text NOT NULL,
    es_predeterminada boolean DEFAULT false,
    actualizado_en timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE public.sms_plantillas_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.sms_plantillas_id_seq OWNED BY public.sms_plantillas.id;

CREATE TABLE public.solicitudes_paz_y_salvo (
    id integer NOT NULL,
    inmueble_id integer,
    identificacion_deudor character varying(50) NOT NULL,
    nombre_deudor character varying(255),
    conjunto_residencial character varying(255),
    torre_apto character varying(100),
    fecha_solicitud date DEFAULT CURRENT_DATE NOT NULL,
    recaudo_id integer,
    estado character varying(30) DEFAULT 'PENDIENTE_REVISION'::character varying,
    url_pdf_paz_y_salvo text,
    codigo_verificacion character varying(64),
    verificado_por character varying(100),
    fecha_verificacion timestamp without time zone,
    motivo_rechazo text,
    creado_en timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    obligacion_id integer
);

CREATE SEQUENCE public.solicitudes_paz_y_salvo_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.solicitudes_paz_y_salvo_id_seq OWNED BY public.solicitudes_paz_y_salvo.id;

CREATE TABLE public.tipos_obligacion (
    id bigint NOT NULL,
    codigo text NOT NULL,
    nombre text NOT NULL,
    activo boolean DEFAULT true NOT NULL,
    requiere_documento boolean DEFAULT false NOT NULL,
    requiere_conjunto boolean DEFAULT false NOT NULL,
    requiere_inmueble boolean DEFAULT false NOT NULL,
    fuente_saldo text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE public.tipos_obligacion_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.tipos_obligacion_id_seq OWNED BY public.tipos_obligacion.id;

CREATE TABLE public.tipos_proceso (
    id bigint NOT NULL,
    codigo text NOT NULL,
    nombre text NOT NULL,
    activo boolean DEFAULT true NOT NULL,
    tipo_cartera_default character varying(20) DEFAULT 'JURIDICO'::character varying NOT NULL,
    requiere_conjunto boolean DEFAULT false NOT NULL,
    requiere_inmueble boolean DEFAULT false NOT NULL,
    requiere_juzgado boolean DEFAULT false NOT NULL,
    requiere_documento boolean DEFAULT false NOT NULL,
    fuente_saldo text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    familia text DEFAULT 'EJECUTIVO'::text NOT NULL,
    subtipo text
);

CREATE SEQUENCE public.tipos_proceso_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.tipos_proceso_id_seq OWNED BY public.tipos_proceso.id;

CREATE TABLE public.usuario_conjuntos (
    usuario_id integer NOT NULL,
    conjunto_id integer NOT NULL
);

CREATE TABLE public.vencimientos (
    id integer NOT NULL,
    radicado_interno text,
    titulo text,
    fecha_vencimiento text,
    estado text DEFAULT 'Pendiente'::text,
    observaciones text,
    completado boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    tipo text DEFAULT 'PROCESAL'::text,
    valor numeric(14,2) DEFAULT 0,
    inmueble_id integer,
    anulado boolean DEFAULT false NOT NULL,
    categoria text DEFAULT 'TERMINO'::text NOT NULL,
    abogado_id text,
    obligacion_id integer
);

CREATE SEQUENCE public.vencimientos_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.vencimientos_id_seq OWNED BY public.vencimientos.id;

CREATE TABLE public.whatsapp_agenda (
    id bigint NOT NULL,
    telefono_digits text NOT NULL,
    telefono_display text,
    nombre text NOT NULL,
    notas text,
    contacto_id bigint,
    updated_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.whatsapp_agenda_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.whatsapp_agenda_id_seq OWNED BY public.whatsapp_agenda.id;

ALTER TABLE ONLY public.abogados ALTER COLUMN id SET DEFAULT nextval('public.abogados_id_seq'::regclass);

ALTER TABLE ONLY public.actuaciones ALTER COLUMN id SET DEFAULT nextval('public.actuaciones_id_seq'::regclass);

ALTER TABLE ONLY public.acuerdos_pago ALTER COLUMN id SET DEFAULT nextval('public.acuerdos_pago_id_seq'::regclass);

ALTER TABLE ONLY public.acuerdos_pago_cuotas ALTER COLUMN id SET DEFAULT nextval('public.acuerdos_pago_cuotas_id_seq'::regclass);

ALTER TABLE ONLY public.agenda_auditoria ALTER COLUMN id SET DEFAULT nextval('public.agenda_auditoria_id_seq'::regclass);

ALTER TABLE ONLY public.auditoria ALTER COLUMN id SET DEFAULT nextval('public.auditoria_id_seq'::regclass);

ALTER TABLE ONLY public.auditoria_chats ALTER COLUMN id SET DEFAULT nextval('public.auditoria_chats_id_seq'::regclass);

ALTER TABLE ONLY public.cartas_cobro_plantillas ALTER COLUMN id SET DEFAULT nextval('public.cartas_cobro_plantillas_id_seq'::regclass);

ALTER TABLE ONLY public.conjuntos_residenciales ALTER COLUMN id SET DEFAULT nextval('public.conjuntos_residenciales_id_seq'::regclass);

ALTER TABLE ONLY public.contactos ALTER COLUMN id SET DEFAULT nextval('public.contactos_id_seq'::regclass);

ALTER TABLE ONLY public.control_agente ALTER COLUMN id SET DEFAULT nextval('public.control_agente_id_seq'::regclass);

ALTER TABLE ONLY public.conversaciones_agente ALTER COLUMN id SET DEFAULT nextval('public.conversaciones_agente_id_seq'::regclass);

ALTER TABLE ONLY public.data_migration_exceptions ALTER COLUMN id SET DEFAULT nextval('public.data_migration_exceptions_id_seq'::regclass);

ALTER TABLE ONLY public.expediente_ediciones ALTER COLUMN id SET DEFAULT nextval('public.expediente_ediciones_id_seq'::regclass);

ALTER TABLE ONLY public.expensas_ph ALTER COLUMN id SET DEFAULT nextval('public.expensas_ph_id_seq'::regclass);

ALTER TABLE ONLY public.gastos ALTER COLUMN id SET DEFAULT nextval('public.gastos_id_seq'::regclass);

ALTER TABLE ONLY public.gestiones_cartera ALTER COLUMN id SET DEFAULT nextval('public.gestiones_cartera_id_seq'::regclass);

ALTER TABLE ONLY public.gestiones_crm ALTER COLUMN id SET DEFAULT nextval('public.gestiones_crm_id_seq'::regclass);

ALTER TABLE ONLY public.historico_tasas ALTER COLUMN id SET DEFAULT nextval('public.historico_tasas_id_seq'::regclass);

ALTER TABLE ONLY public.inmueble_propietarios ALTER COLUMN id SET DEFAULT nextval('public.inmueble_propietarios_id_seq'::regclass);

ALTER TABLE ONLY public.inmuebles_ph ALTER COLUMN id SET DEFAULT nextval('public.inmuebles_ph_id_seq'::regclass);

ALTER TABLE ONLY public.mensajes_agente ALTER COLUMN id SET DEFAULT nextval('public.mensajes_agente_id_seq'::regclass);

ALTER TABLE ONLY public.obligacion_movimientos ALTER COLUMN id SET DEFAULT nextval('public.obligacion_movimientos_id_seq'::regclass);

ALTER TABLE ONLY public.obligacion_partes ALTER COLUMN id SET DEFAULT nextval('public.obligacion_partes_id_seq'::regclass);

ALTER TABLE ONLY public.obligaciones ALTER COLUMN id SET DEFAULT nextval('public.obligaciones_id_seq'::regclass);

ALTER TABLE ONLY public.perfiles ALTER COLUMN id SET DEFAULT nextval('public.perfiles_id_seq'::regclass);

ALTER TABLE ONLY public.proceso_obligaciones ALTER COLUMN id SET DEFAULT nextval('public.proceso_obligaciones_id_seq'::regclass);

ALTER TABLE ONLY public.proceso_partes ALTER COLUMN id SET DEFAULT nextval('public.proceso_partes_id_seq'::regclass);

ALTER TABLE ONLY public.recaudos_contabilidad ALTER COLUMN id SET DEFAULT nextval('public.recaudos_contabilidad_id_seq'::regclass);

ALTER TABLE ONLY public.sms_cola_envios ALTER COLUMN id SET DEFAULT nextval('public.sms_cola_envios_id_seq'::regclass);

ALTER TABLE ONLY public.sms_plantillas ALTER COLUMN id SET DEFAULT nextval('public.sms_plantillas_id_seq'::regclass);

ALTER TABLE ONLY public.solicitudes_paz_y_salvo ALTER COLUMN id SET DEFAULT nextval('public.solicitudes_paz_y_salvo_id_seq'::regclass);

ALTER TABLE ONLY public.tipos_obligacion ALTER COLUMN id SET DEFAULT nextval('public.tipos_obligacion_id_seq'::regclass);

ALTER TABLE ONLY public.tipos_proceso ALTER COLUMN id SET DEFAULT nextval('public.tipos_proceso_id_seq'::regclass);

ALTER TABLE ONLY public.vencimientos ALTER COLUMN id SET DEFAULT nextval('public.vencimientos_id_seq'::regclass);

ALTER TABLE ONLY public.whatsapp_agenda ALTER COLUMN id SET DEFAULT nextval('public.whatsapp_agenda_id_seq'::regclass);

ALTER TABLE ONLY public.abogados
    ADD CONSTRAINT abogados_nombre_key UNIQUE (nombre);

ALTER TABLE ONLY public.abogados
    ADD CONSTRAINT abogados_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.actuaciones
    ADD CONSTRAINT actuaciones_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.acuerdos_pago_cuotas
    ADD CONSTRAINT acuerdos_pago_cuotas_acuerdo_id_numero_cuota_key UNIQUE (acuerdo_id, numero_cuota);

ALTER TABLE ONLY public.acuerdos_pago_cuotas
    ADD CONSTRAINT acuerdos_pago_cuotas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.acuerdos_pago
    ADD CONSTRAINT acuerdos_pago_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agenda_auditoria
    ADD CONSTRAINT agenda_auditoria_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.auditoria_chats
    ADD CONSTRAINT auditoria_chats_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.auditoria
    ADD CONSTRAINT auditoria_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.bot_estado_persistente
    ADD CONSTRAINT bot_estado_persistente_pkey PRIMARY KEY (numero_telefono);

ALTER TABLE ONLY public.bot_eventos_procesados
    ADD CONSTRAINT bot_eventos_procesados_pkey PRIMARY KEY (message_id);

ALTER TABLE ONLY public.cartas_cobro_plantillas
    ADD CONSTRAINT cartas_cobro_plantillas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cartera_totales_snapshot
    ADD CONSTRAINT cartera_totales_snapshot_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.configuracion
    ADD CONSTRAINT configuracion_pkey PRIMARY KEY (clave);

ALTER TABLE ONLY public.conjuntos_residenciales
    ADD CONSTRAINT conjuntos_residenciales_nombre_key UNIQUE (nombre);

ALTER TABLE ONLY public.conjuntos_residenciales
    ADD CONSTRAINT conjuntos_residenciales_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.contactos
    ADD CONSTRAINT contactos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.control_agente
    ADD CONSTRAINT control_agente_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.conversaciones_agente
    ADD CONSTRAINT conversaciones_agente_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.conversaciones_agente
    ADD CONSTRAINT conversaciones_agente_telefono_key UNIQUE (telefono);

ALTER TABLE ONLY public.data_migration_exceptions
    ADD CONSTRAINT data_migration_exceptions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.expediente_ediciones
    ADD CONSTRAINT expediente_ediciones_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.expensas_ph
    ADD CONSTRAINT expensas_ph_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.gastos
    ADD CONSTRAINT gastos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.gestiones_cartera
    ADD CONSTRAINT gestiones_cartera_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.gestiones_crm
    ADD CONSTRAINT gestiones_crm_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.historico_tasas
    ADD CONSTRAINT historico_tasas_anio_mes_key UNIQUE (anio, mes);

ALTER TABLE ONLY public.historico_tasas
    ADD CONSTRAINT historico_tasas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.inmueble_propietarios
    ADD CONSTRAINT inmueble_propietarios_inmueble_id_contacto_id_key UNIQUE (inmueble_id, contacto_id);

ALTER TABLE ONLY public.inmueble_propietarios
    ADD CONSTRAINT inmueble_propietarios_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.inmuebles_ph
    ADD CONSTRAINT inmuebles_ph_conjunto_residencial_torre_apto_key UNIQUE (conjunto_residencial, torre_apto);

ALTER TABLE ONLY public.inmuebles_ph
    ADD CONSTRAINT inmuebles_ph_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.mensajes_agente
    ADD CONSTRAINT mensajes_agente_mensaje_meta_id_key UNIQUE (mensaje_meta_id);

ALTER TABLE ONLY public.mensajes_agente
    ADD CONSTRAINT mensajes_agente_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.obligacion_movimientos
    ADD CONSTRAINT obligacion_movimientos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.obligacion_partes
    ADD CONSTRAINT obligacion_partes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.obligaciones
    ADD CONSTRAINT obligaciones_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.perfiles
    ADD CONSTRAINT perfiles_codigo_key UNIQUE (codigo);

ALTER TABLE ONLY public.perfiles
    ADD CONSTRAINT perfiles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.proceso_obligaciones
    ADD CONSTRAINT proceso_obligaciones_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.proceso_partes
    ADD CONSTRAINT proceso_partes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.procesos
    ADD CONSTRAINT procesos_pkey PRIMARY KEY (radicado_interno);

ALTER TABLE ONLY public.recaudos_contabilidad
    ADD CONSTRAINT recaudos_contabilidad_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);

ALTER TABLE ONLY public.sms_cola_envios
    ADD CONSTRAINT sms_cola_envios_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sms_plantillas
    ADD CONSTRAINT sms_plantillas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sms_plantillas
    ADD CONSTRAINT sms_plantillas_tipo_key UNIQUE (tipo);

ALTER TABLE ONLY public.solicitudes_paz_y_salvo
    ADD CONSTRAINT solicitudes_paz_y_salvo_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tipos_obligacion
    ADD CONSTRAINT tipos_obligacion_codigo_key UNIQUE (codigo);

ALTER TABLE ONLY public.tipos_obligacion
    ADD CONSTRAINT tipos_obligacion_nombre_key UNIQUE (nombre);

ALTER TABLE ONLY public.tipos_obligacion
    ADD CONSTRAINT tipos_obligacion_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tipos_proceso
    ADD CONSTRAINT tipos_proceso_codigo_key UNIQUE (codigo);

ALTER TABLE ONLY public.tipos_proceso
    ADD CONSTRAINT tipos_proceso_nombre_key UNIQUE (nombre);

ALTER TABLE ONLY public.tipos_proceso
    ADD CONSTRAINT tipos_proceso_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.actuaciones
    ADD CONSTRAINT unicidad_actuacion UNIQUE (radicado_interno, fecha, descripcion);

ALTER TABLE ONLY public.cartas_cobro_plantillas
    ADD CONSTRAINT uq_cartas_cobro_plantillas_nombre_tipo UNIQUE (nombre, tipo_cartera);

ALTER TABLE ONLY public.data_migration_exceptions
    ADD CONSTRAINT uq_data_migration_exception UNIQUE (migration_version, entity_type, entity_key);

ALTER TABLE ONLY public.obligacion_partes
    ADD CONSTRAINT uq_obligacion_partes_rol UNIQUE (obligacion_id, contacto_id, rol);

ALTER TABLE ONLY public.proceso_obligaciones
    ADD CONSTRAINT uq_proceso_obligacion UNIQUE (radicado_interno, obligacion_id);

ALTER TABLE ONLY public.proceso_partes
    ADD CONSTRAINT uq_proceso_partes UNIQUE (radicado_interno, contacto_id, rol);

ALTER TABLE ONLY public.whatsapp_agenda
    ADD CONSTRAINT uq_whatsapp_agenda_telefono_digits UNIQUE (telefono_digits);

ALTER TABLE ONLY public.usuario_conjuntos
    ADD CONSTRAINT usuario_conjuntos_pkey PRIMARY KEY (usuario_id, conjunto_id);

ALTER TABLE ONLY public.vencimientos
    ADD CONSTRAINT vencimientos_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.whatsapp_agenda
    ADD CONSTRAINT whatsapp_agenda_pkey PRIMARY KEY (id);

CREATE INDEX idx_acuerdos_cuotas_fecha ON public.acuerdos_pago_cuotas USING btree (fecha_vencimiento, estado, anulado);

CREATE INDEX idx_acuerdos_deudor ON public.acuerdos_pago USING btree (identificacion_deudor);

CREATE INDEX idx_acuerdos_fecha ON public.acuerdos_pago USING btree (fecha_compromiso, estado);

CREATE INDEX idx_acuerdos_inmueble ON public.acuerdos_pago USING btree (inmueble_id);

CREATE INDEX idx_acuerdos_obligacion ON public.acuerdos_pago USING btree (obligacion_id, fecha_compromiso DESC);

CREATE INDEX idx_agenda_auditoria_abogado ON public.agenda_auditoria USING btree (abogado_id, fecha DESC);

CREATE INDEX idx_agenda_auditoria_fecha ON public.agenda_auditoria USING btree (fecha DESC);

CREATE INDEX idx_auditoria_telefono ON public.auditoria_chats USING btree (numero_telefono);

CREATE INDEX idx_bot_estado_persistente_actualizado ON public.bot_estado_persistente USING btree (actualizado_en);

CREATE INDEX idx_bot_eventos_procesados_numero ON public.bot_eventos_procesados USING btree (numero_telefono);

CREATE INDEX idx_cartas_cobro_plantillas_tipo_activo ON public.cartas_cobro_plantillas USING btree (tipo_cartera, activo);

CREATE INDEX idx_conjuntos_residenciales_activo_nombre ON public.conjuntos_residenciales USING btree (activo, nombre);

CREATE INDEX idx_conjuntos_residenciales_contacto ON public.conjuntos_residenciales USING btree (contacto_id);

CREATE INDEX idx_contactos_sms_telefono ON public.contactos USING btree (id, telefono);

CREATE INDEX idx_control_agente_conversacion ON public.control_agente USING btree (conversacion_id, fecha DESC);

CREATE INDEX idx_conversaciones_agente_actividad ON public.conversaciones_agente USING btree (fecha_ultima_actividad DESC);

CREATE INDEX idx_conversaciones_agente_modo ON public.conversaciones_agente USING btree (modo_actual);

CREATE INDEX idx_expediente_ediciones_radicado ON public.expediente_ediciones USING btree (radicado_interno, fecha DESC);

CREATE INDEX idx_expensas_ph_obligacion ON public.expensas_ph USING btree (obligation_id);

CREATE INDEX idx_gestiones_crm_identificacion ON public.gestiones_crm USING btree (identificacion_deudor, fecha DESC);

CREATE INDEX idx_gestiones_crm_inmueble ON public.gestiones_crm USING btree (inmueble_id, fecha DESC);

CREATE INDEX idx_gestiones_crm_inmueble_anulado ON public.gestiones_crm USING btree (inmueble_id, anulado);

CREATE INDEX idx_gestiones_crm_obligacion ON public.gestiones_crm USING btree (obligacion_id, fecha DESC);

CREATE INDEX idx_gestiones_crm_radicado ON public.gestiones_crm USING btree (radicado_interno, fecha DESC);

CREATE INDEX idx_inmueble_propietarios_contacto ON public.inmueble_propietarios USING btree (contacto_id);

CREATE INDEX idx_inmueble_propietarios_inmueble ON public.inmueble_propietarios USING btree (inmueble_id);

CREATE INDEX idx_inmuebles_ph_conjunto_id ON public.inmuebles_ph USING btree (conjunto_id);

CREATE INDEX idx_mensajes_agente_conversacion ON public.mensajes_agente USING btree (conversacion_id, fecha DESC);

CREATE INDEX idx_obligacion_movimientos_obligacion_fecha ON public.obligacion_movimientos USING btree (obligacion_id, fecha, id);

CREATE INDEX idx_obligacion_partes_contacto ON public.obligacion_partes USING btree (contacto_id);

CREATE INDEX idx_obligacion_partes_obligacion ON public.obligacion_partes USING btree (obligacion_id);

CREATE INDEX idx_obligaciones_acreedor_contacto ON public.obligaciones USING btree (acreedor_contacto_id);

CREATE INDEX idx_obligaciones_inmueble ON public.obligaciones USING btree (inmueble_id);

CREATE INDEX idx_obligaciones_numero_documento ON public.obligaciones USING btree (numero_documento);

CREATE INDEX idx_obligaciones_proceso ON public.obligaciones USING btree (proceso_id);

CREATE INDEX idx_obligaciones_tipo_obligacion ON public.obligaciones USING btree (tipo_obligacion_id);

CREATE INDEX idx_obligaciones_tipo_proceso ON public.obligaciones USING btree (tipo_proceso_id);

CREATE INDEX idx_paz_salvo_obligacion ON public.solicitudes_paz_y_salvo USING btree (obligacion_id);

CREATE INDEX idx_proceso_obligaciones_obligacion ON public.proceso_obligaciones USING btree (obligacion_id);

CREATE INDEX idx_proceso_obligaciones_proceso ON public.proceso_obligaciones USING btree (radicado_interno);

CREATE INDEX idx_proceso_partes_sms_candidato ON public.proceso_partes USING btree (contacto_id, rol, radicado_interno);

CREATE INDEX idx_procesos_tipo_cartera ON public.procesos USING btree (tipo_cartera);

CREATE INDEX idx_procesos_tipo_proceso ON public.procesos USING btree (tipo_proceso_id);

CREATE INDEX idx_recaudos_estado ON public.recaudos_contabilidad USING btree (estado_conciliacion);

CREATE INDEX idx_recaudos_fecha ON public.recaudos_contabilidad USING btree (fecha_pago);

CREATE INDEX idx_recaudos_inmueble ON public.recaudos_contabilidad USING btree (inmueble_id);

CREATE INDEX idx_recaudos_obligacion ON public.recaudos_contabilidad USING btree (obligacion_id, fecha_pago DESC);

CREATE INDEX idx_sms_cola_contacto_fecha ON public.sms_cola_envios USING btree (contacto_id, fecha_envio);

CREATE INDEX idx_sms_cola_estado ON public.sms_cola_envios USING btree (estado);

CREATE INDEX idx_sms_cola_proceso ON public.sms_cola_envios USING btree (estado, fecha_proceso);

CREATE INDEX idx_sms_cola_saldo_verificado ON public.sms_cola_envios USING btree (estado, saldo_verificado);

CREATE INDEX idx_sms_cola_telefono ON public.sms_cola_envios USING btree (telefono);

CREATE INDEX idx_sms_obligacion ON public.sms_cola_envios USING btree (obligacion_id, estado);

CREATE INDEX idx_solicitudes_pys_estado ON public.solicitudes_paz_y_salvo USING btree (estado);

CREATE INDEX idx_solicitudes_pys_inmueble ON public.solicitudes_paz_y_salvo USING btree (inmueble_id);

CREATE INDEX idx_tipos_proceso_familia ON public.tipos_proceso USING btree (familia);

CREATE INDEX idx_tipos_proceso_subtipo ON public.tipos_proceso USING btree (subtipo);

CREATE INDEX idx_usuario_conjuntos_conjunto ON public.usuario_conjuntos USING btree (conjunto_id);

CREATE INDEX idx_vencimientos_fecha ON public.vencimientos USING btree (fecha_vencimiento, completado);

CREATE INDEX idx_vencimientos_obligacion ON public.vencimientos USING btree (obligacion_id, fecha_vencimiento);

CREATE INDEX idx_whatsapp_agenda_nombre ON public.whatsapp_agenda USING btree (nombre);

CREATE UNIQUE INDEX uq_contactos_identificacion_idx ON public.contactos USING btree (identificacion);

CREATE UNIQUE INDEX uq_expensas_ph_periodo ON public.expensas_ph USING btree (inmueble_id, concepto, periodo_anio, periodo_mes);

CREATE UNIQUE INDEX uq_historico_tasas_periodo_modalidad ON public.historico_tasas USING btree (anio, mes, COALESCE(modalidad, 'Consumo y ordinario'::character varying));

CREATE UNIQUE INDEX uq_obligacion_parte_principal ON public.obligacion_partes USING btree (obligacion_id) WHERE (es_principal = true);

CREATE UNIQUE INDEX uq_proceso_obligacion_principal ON public.proceso_obligaciones USING btree (radicado_interno) WHERE (es_principal = true);

CREATE UNIQUE INDEX uq_procesos_radicado_rama_real ON public.procesos USING btree (radicado_rama) WHERE (radicado_rama IS NOT NULL);

CREATE UNIQUE INDEX uq_sms_cola_inm_tel_campana_activo ON public.sms_cola_envios USING btree (inmueble_id, telefono, tipo_campana) WHERE ((estado)::text = ANY ((ARRAY['PENDIENTE'::character varying, 'EN_PROCESO'::character varying])::text[]));

CREATE TRIGGER trg_bloquear_actuaciones_prejuridicas BEFORE INSERT ON public.actuaciones FOR EACH ROW EXECUTE FUNCTION public.fn_bloquear_actuaciones_prejuridicas();

CREATE TRIGGER trg_sync_tipo_cartera_proceso BEFORE INSERT OR UPDATE OF radicado_rama ON public.procesos FOR EACH ROW EXECUTE FUNCTION public.fn_sync_tipo_cartera_proceso();

ALTER TABLE ONLY public.abogados
    ADD CONSTRAINT abogados_perfil_id_fkey FOREIGN KEY (perfil_id) REFERENCES public.perfiles(id);

ALTER TABLE ONLY public.acuerdos_pago_cuotas
    ADD CONSTRAINT acuerdos_pago_cuotas_acuerdo_id_fkey FOREIGN KEY (acuerdo_id) REFERENCES public.acuerdos_pago(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.control_agente
    ADD CONSTRAINT control_agente_conversacion_id_fkey FOREIGN KEY (conversacion_id) REFERENCES public.conversaciones_agente(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.acuerdos_pago
    ADD CONSTRAINT fk_acuerdo_obligacion FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.conjuntos_residenciales
    ADD CONSTRAINT fk_conjunto_contacto FOREIGN KEY (contacto_id) REFERENCES public.contactos(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.gestiones_crm
    ADD CONSTRAINT fk_crm_obligacion FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.expensas_ph
    ADD CONSTRAINT fk_expensa_inmueble FOREIGN KEY (inmueble_id) REFERENCES public.inmuebles_ph(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.expensas_ph
    ADD CONSTRAINT fk_expensa_obligacion FOREIGN KEY (obligation_id) REFERENCES public.obligaciones(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.inmuebles_ph
    ADD CONSTRAINT fk_inmueble_conjunto FOREIGN KEY (conjunto_id) REFERENCES public.conjuntos_residenciales(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.inmuebles_ph
    ADD CONSTRAINT fk_inmueble_contacto FOREIGN KEY (contacto_id) REFERENCES public.contactos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.obligacion_movimientos
    ADD CONSTRAINT fk_movimiento_obligacion FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.obligaciones
    ADD CONSTRAINT fk_obligacion_acreedor_contacto FOREIGN KEY (acreedor_contacto_id) REFERENCES public.contactos(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.obligaciones
    ADD CONSTRAINT fk_obligacion_inmueble FOREIGN KEY (inmueble_id) REFERENCES public.inmuebles_ph(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.obligaciones
    ADD CONSTRAINT fk_obligacion_proceso FOREIGN KEY (proceso_id) REFERENCES public.procesos(radicado_interno) ON DELETE SET NULL;

ALTER TABLE ONLY public.obligaciones
    ADD CONSTRAINT fk_obligacion_tipo_obligacion FOREIGN KEY (tipo_obligacion_id) REFERENCES public.tipos_obligacion(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.obligaciones
    ADD CONSTRAINT fk_obligacion_tipo_proceso FOREIGN KEY (tipo_proceso_id) REFERENCES public.tipos_proceso(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.solicitudes_paz_y_salvo
    ADD CONSTRAINT fk_paz_salvo_obligacion FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.procesos
    ADD CONSTRAINT fk_proceso_abogado FOREIGN KEY (abogado_id) REFERENCES public.abogados(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.procesos
    ADD CONSTRAINT fk_proceso_inmueble FOREIGN KEY (inmueble_id) REFERENCES public.inmuebles_ph(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.proceso_obligaciones
    ADD CONSTRAINT fk_proceso_obligacion_obligacion FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.proceso_obligaciones
    ADD CONSTRAINT fk_proceso_obligacion_proceso FOREIGN KEY (radicado_interno) REFERENCES public.procesos(radicado_interno) ON DELETE CASCADE;

ALTER TABLE ONLY public.proceso_partes
    ADD CONSTRAINT fk_proceso_partes_contacto FOREIGN KEY (contacto_id) REFERENCES public.contactos(id);

ALTER TABLE ONLY public.proceso_partes
    ADD CONSTRAINT fk_proceso_partes_proceso FOREIGN KEY (radicado_interno) REFERENCES public.procesos(radicado_interno);

ALTER TABLE ONLY public.procesos
    ADD CONSTRAINT fk_proceso_tipo_proceso FOREIGN KEY (tipo_proceso_id) REFERENCES public.tipos_proceso(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.recaudos_contabilidad
    ADD CONSTRAINT fk_recaudo_obligacion FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sms_cola_envios
    ADD CONSTRAINT fk_sms_obligacion FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.vencimientos
    ADD CONSTRAINT fk_vencimiento_obligacion FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.mensajes_agente
    ADD CONSTRAINT mensajes_agente_conversacion_id_fkey FOREIGN KEY (conversacion_id) REFERENCES public.conversaciones_agente(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.obligacion_partes
    ADD CONSTRAINT obligacion_partes_contacto_id_fkey FOREIGN KEY (contacto_id) REFERENCES public.contactos(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.obligacion_partes
    ADD CONSTRAINT obligacion_partes_obligacion_id_fkey FOREIGN KEY (obligacion_id) REFERENCES public.obligaciones(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.recaudos_contabilidad
    ADD CONSTRAINT recaudos_contabilidad_inmueble_id_fkey FOREIGN KEY (inmueble_id) REFERENCES public.inmuebles_ph(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sms_cola_envios
    ADD CONSTRAINT sms_cola_envios_contacto_id_fkey FOREIGN KEY (contacto_id) REFERENCES public.contactos(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sms_cola_envios
    ADD CONSTRAINT sms_cola_envios_inmueble_id_fkey FOREIGN KEY (inmueble_id) REFERENCES public.inmuebles_ph(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.solicitudes_paz_y_salvo
    ADD CONSTRAINT solicitudes_paz_y_salvo_inmueble_id_fkey FOREIGN KEY (inmueble_id) REFERENCES public.inmuebles_ph(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.solicitudes_paz_y_salvo
    ADD CONSTRAINT solicitudes_paz_y_salvo_recaudo_id_fkey FOREIGN KEY (recaudo_id) REFERENCES public.recaudos_contabilidad(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.usuario_conjuntos
    ADD CONSTRAINT usuario_conjuntos_conjunto_id_fkey FOREIGN KEY (conjunto_id) REFERENCES public.conjuntos_residenciales(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.usuario_conjuntos
    ADD CONSTRAINT usuario_conjuntos_usuario_id_fkey FOREIGN KEY (usuario_id) REFERENCES public.abogados(id) ON DELETE CASCADE;


-- Catalogos semilla
INSERT INTO public.perfiles VALUES (1, 'ADMIN', 'Administrador', 'Todo el ERP: usuarios, menús, configuración y acciones fuertes.', true) ON CONFLICT DO NOTHING;
INSERT INTO public.perfiles VALUES (2, 'ABOGADO', 'Abogado', 'Expedientes, cartera, cobro completo, acuerdos, cartas y SMS.', true) ON CONFLICT DO NOTHING;
INSERT INTO public.perfiles VALUES (3, 'AUXILIAR_COBRO', 'Auxiliar de cobro', 'Seguimiento diario de cobro (CRM, SMS, cartas, vencimientos); sin borrar lo crítico ni administrar usuarios.', true) ON CONFLICT DO NOTHING;
INSERT INTO public.perfiles VALUES (4, 'CONSULTA', 'Solo consulta', 'Ver cartera e informes; no editar ni enviar SMS/cartas.', true) ON CONFLICT DO NOTHING;
INSERT INTO public.perfiles VALUES (233, 'CLIENTE_PH', 'Cliente PH', 'Portal PH: solo unidades (torre/apto + demandados) de los conjuntos que el Admin le habilite.', true) ON CONFLICT DO NOTHING;

INSERT INTO public.tipos_obligacion VALUES (1, 'CUOTAS_ADMINISTRACION', 'Cuotas de administración', true, false, true, true, 'EXPENSAS_PH', '2026-09-18 20:41:15.94139') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_obligacion VALUES (2, 'PAGARE', 'Pagaré', true, true, false, false, 'OBLIGACION', '2026-09-18 20:41:15.94139') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_obligacion VALUES (3, 'LETRA_CAMBIO', 'Letra de cambio', true, true, false, false, 'OBLIGACION', '2026-09-18 20:41:15.94139') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_obligacion VALUES (4, 'FACTURA', 'Factura', true, true, false, false, 'OBLIGACION', '2026-09-18 20:41:15.94139') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_obligacion VALUES (5, 'OTRO', 'Otra obligación', true, false, false, false, 'OBLIGACION', '2026-09-18 20:41:15.94139') ON CONFLICT DO NOTHING;

INSERT INTO public.tipos_proceso VALUES (6, 'EJECUTIVO', 'Proceso ejecutivo', true, 'JURIDICO', false, false, false, false, NULL, '2026-09-18 20:41:15.94139', 'PROCEDIMIENTO', 'EJECUTIVO') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_proceso VALUES (7, 'VERBAL', 'Proceso verbal', true, 'JURIDICO', false, false, false, false, NULL, '2026-09-18 20:41:15.94139', 'PROCEDIMIENTO', 'VERBAL') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_proceso VALUES (1, 'CUOTAS_ADMINISTRACION', 'Cuotas de administración', false, 'PREJURIDICO', true, true, false, false, 'EXPENSAS_PH', '2026-09-18 02:49:44.488671', 'EJECUTIVO', 'CUOTAS_ADMINISTRACION') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_proceso VALUES (2, 'PAGARE', 'Pagaré', false, 'JURIDICO', false, false, true, true, 'OBLIGACION', '2026-09-18 02:49:44.488671', 'EJECUTIVO', 'PAGARE') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_proceso VALUES (3, 'LETRA_CAMBIO', 'Letra de cambio', false, 'JURIDICO', false, false, true, true, 'OBLIGACION', '2026-09-18 02:49:44.488671', 'EJECUTIVO', 'LETRA_CAMBIO') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_proceso VALUES (4, 'FACTURA', 'Factura', false, 'JURIDICO', false, false, true, true, 'OBLIGACION', '2026-09-18 02:49:44.488671', 'EJECUTIVO', 'FACTURA') ON CONFLICT DO NOTHING;
INSERT INTO public.tipos_proceso VALUES (5, 'OTRO', 'Otro', false, 'JURIDICO', false, false, true, false, 'PROCESO', '2026-09-18 02:49:44.488671', 'EJECUTIVO', 'OTRO') ON CONFLICT DO NOTHING;

SELECT pg_catalog.setval('public.perfiles_id_seq', 468, true);

SELECT pg_catalog.setval('public.tipos_obligacion_id_seq', 5, true);

SELECT pg_catalog.setval('public.tipos_proceso_id_seq', 7, true);

