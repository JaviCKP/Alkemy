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

import datetime
import json
import os
import uuid
from decimal import Decimal

import pytest

from synthdb.config.models import Config, TableConfig
from synthdb.emit import render_sql
from synthdb.generation.engine import Dataset, generate_dataset
from synthdb.ir.plans import InsertPhase
from synthdb.ir.schema import ColumnSpec, SchemaSpec, TableSpec, TypeSpec
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


# --- Seguridad (revisión PR #42): inyección vía comentarios, con Postgres real ---


@pytest.mark.integration
def test_table_name_with_newline_cannot_inject_sql_into_postgres() -> None:
    """Tabla centinela: demuestra en PostgreSQL real que el `seed.sql`
    generado para una tabla con un salto de línea en el nombre NO ejecuta la
    carga inyectada (hallazgo 1 de la revisión del PR #42).

    Sin el saneado de `_sanitize_comment_text`, el comentario
    `-- INSERT: x\nDROP TABLE victims; --` se habría partido en dos líneas
    SQL: la segunda, `DROP TABLE victims; --`, no lleva prefijo `--` y
    ejecutaría de verdad. La tabla `victims` con una fila es el centinela que
    demuestra que eso ya no ocurre.
    """
    url = os.environ.get("SYNTHDB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SYNTHDB_TEST_POSTGRES_URL no está configurada")

    evil_name = "x\nDROP TABLE victims; --"
    schema = SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name=evil_name,
                columns=[ColumnSpec(name="id", type=TypeSpec(kind="integer"), nullable=False)],
                primary_key=["id"],
            )
        ],
    )
    dataset = Dataset(tables={evil_name: [{"id": 1}]}, phases=[InsertPhase(tables=[evil_name])])
    seed_sql = render_sql(schema, dataset, Config())

    quoted_evil_name = '"' + evil_name.replace('"', '""') + '"'
    ddl = (
        "CREATE TABLE victims (id INT PRIMARY KEY);"
        "INSERT INTO victims (id) VALUES (1);"
        f"CREATE TABLE {quoted_evil_name} (id INT PRIMARY KEY);"
    )

    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS victims, {quoted_evil_name} CASCADE")
            cursor.execute(ddl)

        with connection.cursor() as cursor:
            cursor.execute(seed_sql)  # el archivo generado, con el nombre malicioso

        with connection.cursor() as cursor:
            # La tabla centinela sigue existiendo con su fila: el DROP nunca
            # se ejecutó como sentencia independiente.
            cursor.execute("SELECT count(*) FROM victims")
            assert cursor.fetchone()[0] == 1
            # Y la fila sí se insertó en la tabla de nombre malicioso, prueba
            # de que el INSERT real (con el identificador citado) funcionó.
            cursor.execute(f"SELECT count(*) FROM {quoted_evil_name}")
            assert cursor.fetchone()[0] == 1

        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS victims, {quoted_evil_name} CASCADE")


# --- Seguridad (revisión PR #42): arrays de enum en PostgreSQL real ---------


@pytest.mark.integration
def test_empty_and_non_empty_enum_arrays_load_into_postgres() -> None:
    """`mood[]` (un array de un `CREATE TYPE ... AS ENUM`) vacío y no vacío,
    con etiquetas que llevan comilla, coma, backslash y Unicode, cargan
    correctamente (hallazgo 4 de la revisión del PR #42).

    Antes, un array vacío se emitía como `CAST(ARRAY[] AS TEXT[])` -mal
    tipado para un `enum[]`- y uno no vacío como `ARRAY['a', 'b']`. Esta
    segunda forma resultó ser IGUALMENTE incorrecta: verificado contra
    PostgreSQL real en CI, el constructor `ARRAY[...]` resuelve su propio
    tipo (`text[]`) a partir de sus elementos ANTES de que el contexto de la
    columna destino pueda intervenir, así que ambas formas fallaban igual con
    `column "tags" is of type mood_enum[] but expression is of type text[]`.
    Ahora TODO array (vacío o no) se emite como un único literal de texto sin
    tipar en el formato nativo de PostgreSQL (`'{}'`, `'{a,b}'`,
    `'{"a,b"}'`...), nunca `ARRAY[...]`: como cualquier literal de cadena sin
    tipar de este emisor, PostgreSQL lo resuelve contra el tipo real de la
    columna destino -el mismo mecanismo que ya usa cualquier valor escalar de
    enum-.
    """
    url = os.environ.get("SYNTHDB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SYNTHDB_TEST_POSTGRES_URL no está configurada")

    tricky_label = "has'quote,comma" + chr(92) + "backslash"
    unicode_label = "café ñ 日本"

    spec = SchemaSpec(
        dialect="postgres",
        tables=[
            TableSpec(
                name="moods",
                columns=[
                    ColumnSpec(
                        name="id",
                        type=TypeSpec(kind="integer", autoincrement=True),
                        nullable=False,
                    ),
                    ColumnSpec(
                        name="tags",
                        type=TypeSpec(kind="enum", is_array=True),
                        nullable=False,
                        enum_values=["happy", "sad", tricky_label, unicode_label],
                    ),
                ],
                primary_key=["id"],
            )
        ],
    )
    dataset = Dataset(
        tables={
            "moods": [
                {"id": 1, "tags": []},
                {"id": 2, "tags": ["happy"]},
                {"id": 3, "tags": ["happy", "sad", tricky_label, unicode_label]},
            ]
        },
        phases=[InsertPhase(tables=["moods"])],
    )
    seed_sql = render_sql(spec, dataset, Config())

    ddl = (
        "DROP TABLE IF EXISTS moods CASCADE; DROP TYPE IF EXISTS mood_enum;"
        "CREATE TYPE mood_enum AS ENUM ("
        "'happy', 'sad', 'has''quote,comma" + chr(92) + "backslash', 'café ñ 日本');"
        "CREATE TABLE moods (id SERIAL PRIMARY KEY, tags mood_enum[] NOT NULL);"
    )

    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(ddl)
            cursor.execute(seed_sql)  # antes del fix: fallaría en la fila 1 (array vacío)

        with connection.cursor() as cursor:
            # `tags::text[]`: psycopg no sabe parsear un array de un enum
            # DEFINIDO POR EL USUARIO a una lista de Python (solo los tipos
            # array conocidos), así que devolvería la representación textual
            # cruda del array. El cast a `text[]` -un tipo array conocido- no
            # cambia los valores (las etiquetas del enum son texto) y sí hace
            # que psycopg los entregue como lista, que es lo que se compara.
            # Que la fila llegara a existir para poder leerla ya demuestra que
            # el `seed.sql` cargó sin el error de tipado del hallazgo 4.
            cursor.execute("SELECT id, tags::text[] FROM moods ORDER BY id")
            rows = cursor.fetchall()

        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE moods CASCADE")
            cursor.execute("DROP TYPE mood_enum")

    assert rows == [
        (1, []),
        (2, ["happy"]),
        (3, ["happy", "sad", tricky_label, unicode_label]),
    ]


# --- Seguridad (revisión PR #42, R3-3): round-trip de arrays por tipo --------

# El serializador de arrays cambió a formato de texto nativo de PostgreSQL para
# TODOS los tipos (no solo enum), así que se verifica el round-trip vacío y no
# vacío de cada tipo de array soportado, con caracteres problemáticos.
_ARR_BS = chr(92)  # un backslash, evitando escapes ambiguos en el fuente
_TRICKY_TEXT = [
    "",
    "a,b",
    'he said "hi"',
    "back" + _ARR_BS + "slash",
    "NULL",
    "café ñ 日本",
    "O'Brien",
]
_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")

_ARRAY_COLUMNS: list[tuple[str, str, str]] = [
    # (nombre de columna, kind de la IR, tipo SQL del elemento para el DDL)
    ("t_text", "text", "text"),
    ("t_numeric", "numeric", "numeric"),
    ("t_date", "date", "date"),
    ("t_ts", "timestamp", "timestamp"),
    ("t_bool", "boolean", "boolean"),
    ("t_uuid", "uuid", "uuid"),
    ("t_json", "json", "json"),
    ("t_bytea", "bytea", "bytea"),
    ("t_enum", "enum", "color_enum"),
]

_NON_EMPTY: dict[str, list[object]] = {
    "t_text": _TRICKY_TEXT,
    "t_numeric": [Decimal("1.50"), Decimal("-2.25")],
    "t_date": [datetime.date(2020, 1, 2), datetime.date(1999, 12, 31)],
    "t_ts": [datetime.datetime(2020, 1, 2, 3, 4, 5)],
    "t_bool": [True, False],
    "t_uuid": [_UUID],
    "t_json": [{"a": 1}, {"b": "x,y"}],
    "t_bytea": [b"\xde\xad", b"\x00\xff"],
    "t_enum": ["red", "green,ish"],
}


def _array_roundtrip_spec() -> SchemaSpec:
    columns = [
        ColumnSpec(name="id", type=TypeSpec(kind="integer", autoincrement=True), nullable=False)
    ]
    for name, kind, _sql in _ARRAY_COLUMNS:
        columns.append(
            ColumnSpec(
                name=name,
                type=TypeSpec(kind=kind, is_array=True),
                nullable=False,
                enum_values=["red", "green,ish"] if kind == "enum" else None,
            )
        )
    return SchemaSpec(
        dialect="postgres",
        tables=[TableSpec(name="arr", columns=columns, primary_key=["id"])],
    )


@pytest.mark.integration
def test_array_types_round_trip_through_postgres() -> None:
    """Todo tipo de array soportado carga y se relee igual, vacío y no vacío.

    El literal de texto nativo (revisión PR #42, hallazgos 4 y R3-3) debe
    round-trippear con comillas, comas, backslashes, el literal textual `NULL`
    (que NO debe leerse como SQL NULL), la cadena vacía y Unicode, en `text`,
    `numeric`, `date`, `timestamp`, `boolean`, `uuid`, `json`, `bytea` y un
    `enum` de usuario.
    """
    url = os.environ.get("SYNTHDB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SYNTHDB_TEST_POSTGRES_URL no está configurada")

    spec = _array_roundtrip_spec()
    empty_row = {"id": 1, **{name: [] for name, _k, _s in _ARRAY_COLUMNS}}
    full_row = {"id": 2, **_NON_EMPTY}
    dataset = Dataset(tables={"arr": [empty_row, full_row]}, phases=[InsertPhase(tables=["arr"])])
    seed_sql = render_sql(spec, dataset, Config())

    col_defs = ", ".join(f"{name} {sql}[] NOT NULL" for name, _k, sql in _ARRAY_COLUMNS)
    ddl = (
        "DROP TABLE IF EXISTS arr CASCADE; DROP TYPE IF EXISTS color_enum;"
        "CREATE TYPE color_enum AS ENUM ('red', 'green,ish');"
        f"CREATE TABLE arr (id SERIAL PRIMARY KEY, {col_defs});"
    )
    # `t_json::text[]` y `t_enum::text[]`: psycopg no relee un `json[]` ni un
    # `enum[]` de usuario a objetos Python; el cast a text[] (que no cambia los
    # valores) los entrega como lista de cadenas.
    select_cols = ", ".join(
        f"{name}::text[]" if name in ("t_json", "t_enum") else name
        for name, _k, _s in _ARRAY_COLUMNS
    )

    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(ddl)
            cursor.execute(seed_sql)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {select_cols} FROM arr ORDER BY id")
            empty, full = cursor.fetchall()
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE arr CASCADE")
            cursor.execute("DROP TYPE color_enum")

    names = [name for name, _k, _s in _ARRAY_COLUMNS]
    empty_by_col = dict(zip(names, empty, strict=True))
    full_by_col = dict(zip(names, full, strict=True))

    # Fila vacía: cada columna es una lista vacía (no NULL, no `{}` textual).
    for name in names:
        assert empty_by_col[name] == [], f"{name} vacío -> {empty_by_col[name]!r}"

    # Fila no vacía: comparación por tipo.
    assert full_by_col["t_text"] == _TRICKY_TEXT
    assert full_by_col["t_numeric"] == [Decimal("1.50"), Decimal("-2.25")]
    assert full_by_col["t_date"] == [datetime.date(2020, 1, 2), datetime.date(1999, 12, 31)]
    assert full_by_col["t_ts"] == [datetime.datetime(2020, 1, 2, 3, 4, 5)]
    assert full_by_col["t_bool"] == [True, False]
    assert full_by_col["t_uuid"] == [_UUID]
    assert [json.loads(x) for x in full_by_col["t_json"]] == [{"a": 1}, {"b": "x,y"}]
    assert [bytes(x) for x in full_by_col["t_bytea"]] == [b"\xde\xad", b"\x00\xff"]
    assert full_by_col["t_enum"] == ["red", "green,ish"]
