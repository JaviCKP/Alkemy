-- Fixture: cementerio
-- Riesgo cubierto: restricciones temporales duras entre columnas de una
-- misma fila (fecha_fallecimiento > fecha_nacimiento, expresable como CHECK)
-- y entre tablas (entierro => persona y sepultura ya deben existir, y
-- fecha_entierro >= fecha_fallecimiento de la persona; esto último NO es
-- expresable como CHECK de PostgreSQL porque cruza tablas, así que debe
-- resolverse en el YAML con el mini-DSL, p. ej.:
--   "fecha_entierro >= parent(persona_id).fecha_fallecimiento"
-- Dominio deliberadamente "incómodo": sirve para comprobar que el LLM
-- clasifica columnas y propone reglas sin negarse ni divagar.
-- También ejercita el parseo de COMMENT ON (T1.3).

CREATE TABLE personas (
  id                   SERIAL PRIMARY KEY,
  nombre               TEXT NOT NULL,
  fecha_nacimiento     DATE NOT NULL,
  fecha_fallecimiento  DATE NOT NULL,
  CHECK (fecha_fallecimiento > fecha_nacimiento)
);

COMMENT ON TABLE personas IS 'Personas fallecidas registradas para inhumación';
COMMENT ON COLUMN personas.fecha_fallecimiento IS 'Fecha de defunción, siempre posterior al nacimiento';

CREATE TABLE sepulturas (
  id        SERIAL PRIMARY KEY,
  codigo    VARCHAR(20) NOT NULL UNIQUE,
  tipo      TEXT NOT NULL CHECK (tipo IN ('nicho', 'panteon', 'fosa', 'columbario')),
  capacidad INT NOT NULL CHECK (capacidad > 0)
);

COMMENT ON COLUMN sepulturas.capacidad IS 'Número máximo de restos que admite la sepultura';

CREATE TABLE entierros (
  id             SERIAL PRIMARY KEY,
  persona_id     INT NOT NULL UNIQUE REFERENCES personas(id),
  sepultura_id   INT NOT NULL REFERENCES sepulturas(id),
  fecha_entierro DATE NOT NULL,
  responsable    TEXT NOT NULL
);

COMMENT ON TABLE entierros IS 'Un entierro por persona; ocupa una plaza en una sepultura con capacidad limitada';
