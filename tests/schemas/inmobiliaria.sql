-- Fixture: inmobiliaria
-- Riesgo cubierto: correlaciones numéricas entre columnas de una misma fila
-- (precio ~ f(superficie, tipo)) y reglas temporales contra la fila padre
-- (compraventas.fecha >= viviendas.anio_construccion). Es también el
-- esquema del ejemplo trabajado de la especificación (§16).
-- Dependencias: clientes <- viviendas <- compraventas <- pagos (sin ciclos).

CREATE TABLE clientes (
  id         SERIAL PRIMARY KEY,
  nombre     TEXT NOT NULL,
  email      TEXT NOT NULL UNIQUE,
  fecha_alta DATE NOT NULL
);

CREATE TABLE viviendas (
  id                SERIAL PRIMARY KEY,
  direccion         TEXT NOT NULL,
  tipo              TEXT NOT NULL CHECK (tipo IN ('piso', 'chalet', 'adosado')),
  superficie_m2     NUMERIC(7, 2) NOT NULL CHECK (superficie_m2 > 0),
  anio_construccion INT NOT NULL CHECK (anio_construccion BETWEEN 1900 AND 2026),
  propietario_id    INT NOT NULL REFERENCES clientes(id)
);

CREATE TABLE compraventas (
  id           SERIAL PRIMARY KEY,
  vivienda_id  INT NOT NULL REFERENCES viviendas(id),
  comprador_id INT NOT NULL REFERENCES clientes(id),
  fecha        DATE NOT NULL,
  precio       NUMERIC(12, 2) NOT NULL CHECK (precio > 0)
);

CREATE TABLE pagos (
  id             SERIAL PRIMARY KEY,
  compraventa_id INT NOT NULL REFERENCES compraventas(id),
  num_plazo      INT NOT NULL,
  fecha          DATE NOT NULL,
  importe        NUMERIC(12, 2) NOT NULL CHECK (importe > 0),
  UNIQUE (compraventa_id, num_plazo)
);
