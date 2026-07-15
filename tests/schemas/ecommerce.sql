-- Fixture: ecommerce
-- Riesgo cubierto: volumen (pensado para generarse a 10^5-10^6 filas),
-- distribución zipf en clientes/productos (pocos clientes/productos
-- concentran muchos pedidos/líneas) y la regla de conjunto
-- sum_over_group: pedidos.total ~= sum(lineas_pedido.cantidad *
-- lineas_pedido.precio_unitario) agrupado por pedido_id. Esa coherencia
-- no está en el DDL (no es expresable como CHECK de una fila) y debe
-- resolverse en el YAML/motor, no aquí.

CREATE TABLE clientes (
  id         SERIAL PRIMARY KEY,
  nombre     TEXT NOT NULL,
  email      TEXT NOT NULL UNIQUE,
  fecha_alta DATE NOT NULL
);

CREATE TABLE categorias (
  id     SERIAL PRIMARY KEY,
  nombre TEXT NOT NULL UNIQUE
);

CREATE TABLE productos (
  id           SERIAL PRIMARY KEY,
  categoria_id INT NOT NULL REFERENCES categorias(id),
  nombre       TEXT NOT NULL,
  precio       NUMERIC(10, 2) NOT NULL CHECK (precio > 0),
  stock        INT NOT NULL DEFAULT 0 CHECK (stock >= 0)
);

CREATE TABLE pedidos (
  id         SERIAL PRIMARY KEY,
  cliente_id INT NOT NULL REFERENCES clientes(id),
  fecha      TIMESTAMP NOT NULL,
  estado     TEXT NOT NULL CHECK (estado IN ('pendiente', 'pagado', 'enviado', 'entregado', 'cancelado')),
  total      NUMERIC(12, 2) NOT NULL CHECK (total >= 0)
);

CREATE TABLE lineas_pedido (
  id              SERIAL PRIMARY KEY,
  pedido_id       INT NOT NULL REFERENCES pedidos(id),
  producto_id     INT NOT NULL REFERENCES productos(id),
  cantidad        INT NOT NULL CHECK (cantidad > 0),
  precio_unitario NUMERIC(10, 2) NOT NULL CHECK (precio_unitario > 0),
  UNIQUE (pedido_id, producto_id)
);
