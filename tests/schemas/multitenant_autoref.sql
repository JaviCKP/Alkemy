-- Fixture público mínimo para la autorreferencia compuesta multi-tenant.
-- El discriminador tenant_id se comparte entre dos FKs compuestas de offers
-- y entre la FK jerárquica y su padre de la misma tabla.

CREATE TABLE tenants (
  id UUID PRIMARY KEY
);

CREATE TABLE operations (
  tenant_id UUID NOT NULL,
  id UUID NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE customers (
  tenant_id UUID NOT NULL,
  id UUID NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE offers (
  id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  operation_id UUID NOT NULL,
  customer_id UUID,
  previous_id UUID,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, operation_id)
    REFERENCES operations(tenant_id, id),
  FOREIGN KEY (tenant_id, customer_id)
    REFERENCES customers(tenant_id, id),
  FOREIGN KEY (tenant_id, previous_id)
    REFERENCES offers(tenant_id, id)
);
