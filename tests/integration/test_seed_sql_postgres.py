"""El `seed.sql` generado carga en un PostgreSQL real, sin violaciones (T2.14).

Marcado `@integration`: se salta si no hay una base de datos configurada en
`SYNTHDB_TEST_POSTGRES_URL` (misma convención que `test_numeric_postgres.py`).
El objetivo del criterio de aceptación del Hito 2 es que el archivo exportado
sea cargable en PostgreSQL y quede íntegro: cero FKs huérfanas.

El esquema de `crm_real_minimo` es un ciclo de tablas (`clientes` ↔ `matches`),
que PostgreSQL no puede crear en un solo orden. El esquema se crea aquí en un
orden cargable —las tablas primero y la FK del ciclo con un `ALTER TABLE`
después—, fiel a la IR del fixture. El `seed.sql` no crea DDL: solo `INSERT` y
`UPDATE`, y su fase de `INSERT` con `match_id = NULL` seguida del `UPDATE`
permite cargarlo aunque la FK no sea diferible.
"""

from __future__ import annotations

import os

import pytest

from synthdb.config.models import Config, TableConfig
from synthdb.emit import render_sql
from synthdb.generation.engine import generate_dataset
from synthdb.parsing.ddl import parse_ddl

psycopg = pytest.importorskip("psycopg")

_FIXTURE = (
    "CREATE TABLE inmobiliarias (id BIGINT PRIMARY KEY, nombre TEXT NOT NULL);"
    "CREATE TABLE clientes ("
    " inmobiliaria_id BIGINT NOT NULL REFERENCES inmobiliarias (id),"
    " id BIGINT NOT NULL, roles TEXT[] NOT NULL, match_id BIGINT,"
    " PRIMARY KEY (inmobiliaria_id, id),"
    " FOREIGN KEY (inmobiliaria_id, match_id) REFERENCES matches (inmobiliaria_id, id)"
    " ON DELETE SET NULL (match_id));"
    "CREATE TABLE matches ("
    " inmobiliaria_id BIGINT NOT NULL REFERENCES inmobiliarias (id),"
    " id BIGINT NOT NULL, cliente_id BIGINT NOT NULL,"
    " PRIMARY KEY (inmobiliaria_id, id),"
    " FOREIGN KEY (inmobiliaria_id, cliente_id) REFERENCES clientes (inmobiliaria_id, id));"
)
"""IR de partida (idéntica al fixture `crm_real_minimo.sql`)."""

# DDL cargable: mismas tablas y columnas, pero la FK del ciclo (clientes → matches)
# se añade con ALTER tras crear ambas tablas.
_LOADABLE_DDL = """
DROP TABLE IF EXISTS clientes, matches, inmobiliarias CASCADE;
CREATE TABLE inmobiliarias (id BIGINT PRIMARY KEY, nombre TEXT NOT NULL);
CREATE TABLE clientes (
    inmobiliaria_id BIGINT NOT NULL REFERENCES inmobiliarias (id),
    id BIGINT NOT NULL,
    roles TEXT[] NOT NULL,
    match_id BIGINT,
    PRIMARY KEY (inmobiliaria_id, id)
);
CREATE TABLE matches (
    inmobiliaria_id BIGINT NOT NULL REFERENCES inmobiliarias (id),
    id BIGINT NOT NULL,
    cliente_id BIGINT NOT NULL,
    PRIMARY KEY (inmobiliaria_id, id),
    FOREIGN KEY (inmobiliaria_id, cliente_id) REFERENCES clientes (inmobiliaria_id, id)
);
ALTER TABLE clientes
    ADD FOREIGN KEY (inmobiliaria_id, match_id) REFERENCES matches (inmobiliaria_id, id);
"""

_COUNTS = {"inmobiliarias": 5, "clientes": 30, "matches": 30}


@pytest.mark.integration
def test_crm_seed_sql_loads_into_postgres_without_violations() -> None:
    url = os.environ.get("SYNTHDB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SYNTHDB_TEST_POSTGRES_URL no está configurada")

    spec = parse_ddl(_FIXTURE)
    config = Config(seed=23, tables={name: TableConfig(rows=n) for name, n in _COUNTS.items()})
    dataset = generate_dataset(spec, config)
    assert dataset.quarantine == {}  # generación limpia: nada apartado
    seed_sql = render_sql(spec, dataset, config)

    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_LOADABLE_DDL)  # esquema cargable
            cursor.execute(seed_sql)  # el archivo generado (INSERT/UPDATE por fases)

        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clientes")
            assert cursor.fetchone()[0] == len(dataset.tables["clientes"])
            cursor.execute("SELECT count(*) FROM matches")
            assert cursor.fetchone()[0] == len(dataset.tables["matches"])

            # Ninguna FK del ciclo queda huérfana en ninguno de los dos sentidos.
            cursor.execute(
                "SELECT count(*) FROM clientes c WHERE c.match_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM matches m WHERE m.inmobiliaria_id = c.inmobiliaria_id "
                "AND m.id = c.match_id)"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) FROM matches m WHERE NOT EXISTS "
                "(SELECT 1 FROM clientes c WHERE c.inmobiliaria_id = m.inmobiliaria_id "
                "AND c.id = m.cliente_id)"
            )
            assert cursor.fetchone()[0] == 0

        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS clientes, matches, inmobiliarias CASCADE")
