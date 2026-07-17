-- crm_real_minimo.sql — fixture 11 (ADR-004)
--
-- Reproduce en pequeño los tres patrones del primer esquema real de 20 tablas
-- (una inmobiliaria multi-tenant sobre PostgreSQL 15; ver docs/validaciones/),
-- sin incluir el esquema real del usuario (es de otro proyecto):
--
--   1. Ciclo de FKs compuestas multi-inmobiliaria: la clave típica es
--      (inmobiliaria_id NOT NULL, entidad_id NULL). La FK entera no es anulable,
--      pero la columna que cierra el ciclo (match_id / cliente_id) sí; bajo
--      MATCH SIMPLE (defecto) el ciclo se rompe anulando SOLO esa columna.
--   2. ON DELETE SET NULL (columna) de PostgreSQL 15+, acotado a la columna de
--      entidad (no a inmobiliaria_id).
--   3. Una columna text[] (clientes.roles), como en el esquema real.

CREATE TABLE inmobiliarias (
    id BIGINT PRIMARY KEY,
    nombre TEXT NOT NULL
);

CREATE TABLE clientes (
    inmobiliaria_id BIGINT NOT NULL REFERENCES inmobiliarias (id),
    id BIGINT NOT NULL,
    roles TEXT[] NOT NULL,
    match_id BIGINT,
    PRIMARY KEY (inmobiliaria_id, id),
    -- FK compuesta al ciclo: inmobiliaria_id (NOT NULL) + match_id (NULL).
    -- Bajo MATCH SIMPLE basta anular match_id para romper el ciclo; la acción
    -- ON DELETE se acota a esa misma columna (PostgreSQL 15+).
    FOREIGN KEY (inmobiliaria_id, match_id) REFERENCES matches (inmobiliaria_id, id)
        ON DELETE SET NULL (match_id)
);

CREATE TABLE matches (
    inmobiliaria_id BIGINT NOT NULL REFERENCES inmobiliarias (id),
    id BIGINT NOT NULL,
    cliente_id BIGINT NOT NULL,
    PRIMARY KEY (inmobiliaria_id, id),
    -- Otra rama del ciclo, esta con ambas columnas NOT NULL: no es rompible por
    -- sí misma, así que el ciclo se rompe por la FK de clientes.
    FOREIGN KEY (inmobiliaria_id, cliente_id) REFERENCES clientes (inmobiliaria_id, id)
);
