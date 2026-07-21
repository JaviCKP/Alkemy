-- Fixture: FK que referencia una UNIQUE distinta de la PK (revisión sesión E,
-- hallazgo 3).
-- Riesgo cubierto: RelationshipSpec.ref_columns puede apuntar a cualquier
-- PK/UNIQUE del padre, no solo a su primary_key. La validación por lote y el
-- cierre referencial final deben comparar contra esas columnas exactas, en su
-- orden declarado, nunca asumir parent.primary_key.

-- Reproducción mínima: FK simple hacia una UNIQUE de una sola columna.
CREATE TABLE parent (
  id     SERIAL PRIMARY KEY,
  code   INT NOT NULL UNIQUE,
  nombre TEXT NOT NULL
);

CREATE TABLE child (
  id          SERIAL PRIMARY KEY,
  parent_code INT NOT NULL REFERENCES parent (code)
);

-- Segundo salto para probar el cierre transitivo de la cuarentena.
CREATE TABLE grandchild (
  id       SERIAL PRIMARY KEY,
  child_id INT NOT NULL REFERENCES child (id)
);

-- FK compuesta hacia una UNIQUE compuesta (no la PK).
CREATE TABLE parent_composite (
  id SERIAL PRIMARY KEY,
  a  INT NOT NULL,
  b  INT NOT NULL,
  UNIQUE (a, b)
);

CREATE TABLE child_composite (
  id SERIAL PRIMARY KEY,
  x  INT NOT NULL,
  y  INT NOT NULL,
  FOREIGN KEY (x, y) REFERENCES parent_composite (a, b)
);

-- ref_columns en orden distinto al de la PK compuesta del padre.
CREATE TABLE parent_reordered (
  a INT NOT NULL,
  b INT NOT NULL,
  PRIMARY KEY (a, b)
);

CREATE TABLE child_reordered (
  id SERIAL PRIMARY KEY,
  x  INT NOT NULL,
  y  INT NOT NULL,
  FOREIGN KEY (x, y) REFERENCES parent_reordered (b, a)
);

-- Ciclo diferible (mismo mecanismo que ciclos_deferrable.sql: ambas FK NOT
-- NULL DEFERRABLE INITIALLY DEFERRED, así que las dos tablas entran sin
-- validar al KeyStore antes del cierre final) donde una dirección referencia
-- una UNIQUE, no la PK. Permite reproducir la cuarentena de un padre
-- referenciado por UNIQUE con cierre transitivo: un tercer salto aciclico
-- (detalle_uk) fuera del ciclo comprueba que la cascada llega más allá de la
-- fase diferida.
CREATE TABLE pedidos_uk (
  id         SERIAL PRIMARY KEY,
  codigo     INT NOT NULL UNIQUE,
  fecha      DATE NOT NULL,
  factura_id INT NOT NULL
);

CREATE TABLE facturas_uk (
  id            SERIAL PRIMARY KEY,
  numero        VARCHAR(20) NOT NULL UNIQUE,
  pedido_codigo INT NOT NULL REFERENCES pedidos_uk (codigo) DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE pedidos_uk
  ADD CONSTRAINT fk_pedidos_uk_factura FOREIGN KEY (factura_id) REFERENCES facturas_uk (id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE detalle_uk (
  id         SERIAL PRIMARY KEY,
  factura_id INT NOT NULL REFERENCES facturas_uk (id)
);
