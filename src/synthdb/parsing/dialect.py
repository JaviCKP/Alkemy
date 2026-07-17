"""Extensión del dialecto PostgreSQL de sqlglot (T1 revisión, ADR-004).

PostgreSQL 15 permite acotar la acción `ON DELETE SET NULL` / `SET DEFAULT` de
una FK a columnas concretas:

    FOREIGN KEY (inmobiliaria_id, entidad_id) REFERENCES entidades (…)
        ON DELETE SET NULL (entidad_id)

sqlglot 30.12.0 (la última publicada al escribir esto) reconoce `ON DELETE SET
NULL` pero rechaza la lista de columnas posterior con `Expecting )`, lo que
aborta el análisis del esquema completo. En lugar de preprocesar el texto SQL
con una expresión regular —prohibido por CLAUDE.md, porque reinterpretaría el
esquema fuera de la IR—, se extiende el parser por el **mecanismo oficial** de
sqlglot: una subclase del dialecto `Postgres`.

La subclase solo AÑADE capacidad: cuando el parser base termina de leer las
opciones de una `REFERENCES` y se detiene justo antes de un `(` (la lista de
columnas de PostgreSQL 15 que no sabe consumir), esta subclase la consume como
identificadores del AST y la adjunta a la `Reference` en el argumento
`set_null_columns`. Para cualquier DDL sin esa lista se comporta exactamente
igual que el dialecto base, así que es seguro usarla siempre para `postgres`.
"""

from __future__ import annotations

from sqlglot import TokenType, exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.parsers.postgres import PostgresParser

SET_NULL_COLUMNS_ARG = "set_null_columns"
"""Argumento donde la subclase adjunta las columnas de `SET NULL (…)` a la `Reference`.

Es un argumento fuera del `arg_types` de `exp.Reference`: sqlglot lo conserva en
`Reference.args` sin renderizarlo de vuelta a SQL (SynthDB nunca regenera SQL a
partir de estas referencias), y `parsing/ddl.py` lo lee para poblar
`RelationshipSpec.on_delete_set_columns`.
"""

_SET_NULL_ACTIONS: frozenset[str] = frozenset({"ON DELETE SET NULL", "ON DELETE SET DEFAULT"})
"""Opciones tras las que PostgreSQL 15+ admite una lista de columnas `(col, …)`.

El parser base de sqlglot las representa como estas cadenas exactas en
`Reference.args["options"]`. La lista de columnas solo es válida en `ON DELETE`
(no en `ON UPDATE`), de ahí que solo se contemplen esas dos.
"""


class _SetNullColumnsParser(PostgresParser):
    """Parser de PostgreSQL que además consume la lista de columnas de `SET NULL`/`SET DEFAULT`.

    Se subclasea `PostgresParser` directamente (el mismo mecanismo que usan los
    dialectos propios de sqlglot, p. ej. Redshift), no `Postgres.Parser`, para
    heredar toda la tokenización y el parseo específicos de PostgreSQL.
    """

    def _parse_references(self, match: bool = True) -> exp.Reference | None:
        """Delega en el parser base y consume la lista de columnas que deja sin leer.

        El parser base lee `REFERENCES tabla(…)` y sus opciones (incluida
        `ON DELETE SET NULL`), pero se detiene ante el `(` de la lista de
        columnas de PostgreSQL 15. Aquí se consume ese `(col, …)` como
        identificadores del AST y se adjunta a la `Reference`; si detrás vinieran
        más opciones (`ON UPDATE …`, `MATCH …`, `DEFERRABLE`), se reanuda su
        parseo para no perderlas.
        """
        reference: exp.Reference | None = super()._parse_references(match)
        if reference is None or not self._match(TokenType.L_PAREN, advance=False):
            return reference

        # sqlglot conserva la grafía original de DELETE/UPDATE en la opción
        # (p. ej. "ON delete SET NULL" si el DDL la escribió en minúsculas),
        # así que se normaliza antes de comparar, igual que hace parsing/ddl.py.
        options = reference.args.get("options") or []
        if not options or options[-1].upper() not in _SET_NULL_ACTIONS:
            return reference

        columns = self._parse_wrapped_id_vars()
        reference.set(SET_NULL_COLUMNS_ARG, columns)
        trailing = self._parse_key_constraint_options()
        if trailing:
            reference.set("options", [*options, *trailing])
        return reference


class PostgresSetNullColumns(Postgres):
    """Dialecto PostgreSQL que además acepta `ON DELETE SET NULL (columnas)` (PG15+)."""

    Parser = _SetNullColumnsParser


POSTGRES_SET_NULL_COLUMNS = PostgresSetNullColumns()
"""Instancia reutilizable del dialecto para pasar a `sqlglot.parse(..., read=…)`."""
