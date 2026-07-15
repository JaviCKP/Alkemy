-- Fixture: ciclos (variante diferible)
-- Riesgo cubierto: dos tablas con FK mutuas NOT NULL en ambos sentidos,
-- pero ambas DEFERRABLE INITIALLY DEFERRED. Ninguna FK admite NULL, así
-- que la única estrategia válida (§6.2, opción 2) es insertar todo el
-- ciclo dentro de una única transacción con las constraints diferidas
-- (SET CONSTRAINTS ALL DEFERRED) y resolverlas en el COMMIT.

CREATE TABLE pedidos (
  id         SERIAL PRIMARY KEY,
  fecha      DATE NOT NULL,
  factura_id INT NOT NULL
);

CREATE TABLE facturas (
  id        SERIAL PRIMARY KEY,
  numero    VARCHAR(20) NOT NULL UNIQUE,
  pedido_id INT NOT NULL REFERENCES pedidos(id) DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE pedidos
  ADD CONSTRAINT fk_pedidos_factura FOREIGN KEY (factura_id) REFERENCES facturas(id)
  DEFERRABLE INITIALLY DEFERRED;
