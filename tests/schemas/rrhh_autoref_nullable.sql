-- Fixture: rrhh_autoref (variante anulable)
-- Riesgo cubierto: autorreferencia manager_id NULLABLE. Estrategia
-- esperada (§6.3): generación por niveles, L0 = raíces con manager_id
-- NULL, L1 apunta a L0, etc. Caso "fácil" de autorreferencia, sirve de
-- control frente a la variante NOT NULL.

CREATE TABLE empleados (
  id                 SERIAL PRIMARY KEY,
  nombre             TEXT NOT NULL,
  puesto             TEXT NOT NULL,
  fecha_contratacion DATE NOT NULL,
  manager_id         INT REFERENCES empleados(id)
);
