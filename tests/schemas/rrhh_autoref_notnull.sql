-- Fixture: rrhh_autoref (variante NOT NULL)
-- Riesgo cubierto: autorreferencia manager_id NOT NULL y no diferible.
-- Sin DDL adicional, la única salida válida (§6.3) es que las raíces se
-- referencien a sí mismas (manager_id = id) con aviso registrado en el
-- plan; el resto de niveles se genera igual que en la variante anulable.
-- Deliberadamente NO lleva CHECK (manager_id <> id): esa combinación
-- convertiría el ciclo en irrompible y ese caso ya lo cubre ciclos.sql.

CREATE TABLE empleados (
  id                 SERIAL PRIMARY KEY,
  nombre             TEXT NOT NULL,
  puesto             TEXT NOT NULL,
  fecha_contratacion DATE NOT NULL,
  manager_id         INT NOT NULL REFERENCES empleados(id)
);
