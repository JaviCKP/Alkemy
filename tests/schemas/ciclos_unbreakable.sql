-- Fixture: ciclos (variante irrompible)
-- Riesgo cubierto: dos tablas con FK mutuas NOT NULL y NINGUNA diferible.
-- No existe ninguna secuencia de INSERT/UPDATE válida sin modificar el
-- DDL. Comportamiento esperado (§6.2, opción 3): synthdb debe detenerse
-- con UnbreakableCycle y un diagnóstico accionable (marcar una FK como
-- anulable o diferible), nunca inventar datos ni desactivar constraints
-- por defecto. El DDL en sí carga sin problemas en PostgreSQL: el
-- problema es de generación de datos, no de esquema.

CREATE TABLE pedidos (
  id         SERIAL PRIMARY KEY,
  fecha      DATE NOT NULL,
  factura_id INT NOT NULL
);

CREATE TABLE facturas (
  id        SERIAL PRIMARY KEY,
  numero    VARCHAR(20) NOT NULL UNIQUE,
  pedido_id INT NOT NULL REFERENCES pedidos(id)
);

ALTER TABLE pedidos
  ADD CONSTRAINT fk_pedidos_factura FOREIGN KEY (factura_id) REFERENCES facturas(id);
