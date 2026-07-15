-- Fixture: taller
-- Riesgo cubierto: tabla puente CON atributos propios (reparacion_piezas:
-- cantidad, precio_aplicado), no solo el par de FKs. La PK compuesta está
-- formada íntegramente por FKs hacia dos tablas distintas (reparaciones,
-- piezas) => debe detectarse kind=bridge. Estrategia de generación
-- esperada: quota (cada reparación usa entre 1 y 8 piezas), no independencia
-- ingenua de cada FK.

CREATE TABLE clientes (
  id       SERIAL PRIMARY KEY,
  nombre   TEXT NOT NULL,
  telefono VARCHAR(20)
);

CREATE TABLE vehiculos (
  id         SERIAL PRIMARY KEY,
  cliente_id INT NOT NULL REFERENCES clientes(id),
  matricula  VARCHAR(10) NOT NULL UNIQUE,
  marca      TEXT NOT NULL,
  modelo     TEXT NOT NULL,
  anio       INT NOT NULL CHECK (anio BETWEEN 1980 AND 2026)
);

CREATE TABLE piezas (
  id              SERIAL PRIMARY KEY,
  nombre          TEXT NOT NULL,
  precio_unitario NUMERIC(10, 2) NOT NULL CHECK (precio_unitario > 0)
);

CREATE TABLE reparaciones (
  id            SERIAL PRIMARY KEY,
  vehiculo_id   INT NOT NULL REFERENCES vehiculos(id),
  fecha_entrada DATE NOT NULL,
  fecha_salida  DATE,
  descripcion   TEXT NOT NULL,
  CHECK (fecha_salida IS NULL OR fecha_salida >= fecha_entrada)
);

CREATE TABLE reparacion_piezas (
  reparacion_id   INT NOT NULL REFERENCES reparaciones(id),
  pieza_id        INT NOT NULL REFERENCES piezas(id),
  cantidad        INT NOT NULL CHECK (cantidad > 0),
  precio_aplicado NUMERIC(10, 2) NOT NULL CHECK (precio_aplicado > 0),
  PRIMARY KEY (reparacion_id, pieza_id)
);
