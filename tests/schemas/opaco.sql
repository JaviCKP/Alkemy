-- Fixture: opaco
-- Riesgo cubierto: nombres opacos/engañosos y CERO metadatos (sin
-- COMMENT ON, sin nombres descriptivos). Comportamiento esperado: datos
-- 100% válidos estructuralmente, semantic_coverage baja, confianzas
-- bajas y avisos explícitos de ambigüedad -- nunca semántica inventada
-- con confianza alta. Este comentario de cabecera es metadato del
-- fixture para quien mantenga la suite de tests, no una pista para el
-- motor: adrede no hay ningún COMMENT ON ni nombre de columna
-- interpretable dentro del propio esquema.

CREATE TABLE t1 (
  c1    SERIAL PRIMARY KEY,
  c2    VARCHAR(50) NOT NULL,
  c3    INT,
  c7    NUMERIC(10, 2),
  cod_x CHAR(2)
);

CREATE TABLE t2 (
  c1     SERIAL PRIMARY KEY,
  ref_t1 INT NOT NULL REFERENCES t1(c1),
  c4     DATE,
  c5     BOOLEAN NOT NULL DEFAULT false,
  val    NUMERIC(12, 4)
);
