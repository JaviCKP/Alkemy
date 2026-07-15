-- Fixture: ciclos (variante anulable/rompible)
-- Riesgo cubierto: dos tablas con FK mutuas obligatorias en apariencia,
-- pero pedidos.factura_id es NULLABLE. Estrategia esperada (§6.2,
-- opción 1): insertar pedidos con factura_id=NULL, insertar facturas
-- (que sí exigen pedido_id), y luego UPDATE pedidos para enlazar la
-- factura real. Caso "fácil" de ciclo, control frente a las otras dos
-- variantes.

CREATE TABLE pedidos (
  id         SERIAL PRIMARY KEY,
  fecha      DATE NOT NULL,
  factura_id INT
);

CREATE TABLE facturas (
  id        SERIAL PRIMARY KEY,
  numero    VARCHAR(20) NOT NULL UNIQUE,
  pedido_id INT NOT NULL REFERENCES pedidos(id)
);

ALTER TABLE pedidos
  ADD CONSTRAINT fk_pedidos_factura FOREIGN KEY (factura_id) REFERENCES facturas(id);
