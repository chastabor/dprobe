from dataclasses import replace
from types import ModuleType
from typing import Any

from dprobe.connectors.base import ColumnInfo, Connector, Described, Result, TableInfo
from dprobe.sqltext import is_plsql, remove_trailing_line, remove_trailing_semicolon

# The current schema unless a schema was given, resolved in the query to save a round trip.
_OWNER = "NVL(:owner, SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'))"
# all_tab_comments lists every table and view, so it's cheaper than all_tables plus all_views.
_TABLES = f"""
SELECT owner, table_name, table_type, comments
FROM all_tab_comments
WHERE owner = {_OWNER} AND table_type IN ({{types}}) AND table_name NOT LIKE 'BIN$%'
  AND (:pattern IS NULL OR UPPER(table_name) LIKE UPPER(:pattern))
ORDER BY table_name
"""
# all_tab_cols rather than all_tab_columns for virtual_column; hidden columns are dropped.
_COLUMNS = f"""
SELECT c.owner, c.column_id, c.column_name, c.data_type, c.data_length, c.char_length,
       c.char_used, c.data_precision, c.data_scale, c.nullable, c.data_default,
       c.identity_column, c.virtual_column, p.position, m.comments
FROM all_tab_cols c
LEFT JOIN all_col_comments m
  ON m.owner = c.owner AND m.table_name = c.table_name AND m.column_name = c.column_name
LEFT JOIN (
  SELECT cc.column_name, cc.position
  FROM all_constraints k
  JOIN all_cons_columns cc ON cc.owner = k.owner AND cc.constraint_name = k.constraint_name
  WHERE k.owner = {_OWNER} AND k.table_name = :name AND k.constraint_type = 'P'
) p ON p.column_name = c.column_name
WHERE c.owner = {_OWNER} AND c.table_name = :name AND c.hidden_column = 'NO'
ORDER BY c.column_id
"""
# A private synonym in the current schema wins over a public one, as in Oracle.
_SYNONYM = """
SELECT table_owner, table_name FROM all_synonyms
WHERE synonym_name = :name AND db_link IS NULL
  AND owner IN (SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'), 'PUBLIC')
ORDER BY CASE owner WHEN 'PUBLIC' THEN 2 ELSE 1 END
"""


class OracleConnector(Connector):
    """python-oracledb in thin mode (no Instant Client needed); thin mode always uses UTF-8.

    url goes to connect() as dsn unchanged, so it can be Easy Connect
    ("host:1521/service"), a tnsnames.ora alias (set options.config_dir or
    $TNS_ADMIN) or a full connect descriptor.

    readonly uses SET TRANSACTION READ ONLY, which blocks DML only: DDL
    commits implicitly and still runs.
    """

    ddl_autocommits = True

    def _import_driver(self) -> ModuleType:
        import oracledb

        # LOB objects need an open cursor to read; fetch CLOB/BLOB as str/bytes instead.
        oracledb.defaults.fetch_lobs = False
        # Decimal keeps every digit of NUMBER values that a float would round.
        oracledb.defaults.fetch_decimals = True
        return oracledb

    def _connect_args(self) -> dict[str, Any]:
        return self._merge_options(user=self.config.user, password=self.password, dsn=self.config.url)

    def server_version(self) -> str:
        return f"Oracle Database {self.conn.version}"

    @classmethod
    def placeholder(cls, name: str) -> tuple[str, str]:
        # Oracle rejects reserved words such as :date or :level as bind names
        # (ORA-01745), and none of its reserved words end in "_".
        return f":{name}_", f"{name}_"

    @classmethod
    def identifier(cls, text: str, quoted: bool) -> str:
        # Unquoted names are stored upper-case; "Quoted" ones keep their case.
        return text if quoted else text.upper()

    def list_tables(self, schema: str | None, like: str | None, views: bool) -> list[TableInfo]:
        """schema defaults to the current schema (the user, unless ALTER SESSION changed it)."""
        sql = _TABLES.format(types="'TABLE', 'VIEW'" if views else "'TABLE'")
        return [TableInfo(*row) for row in self._catalog(sql, {"owner": schema, "pattern": like})]

    def describe(self, schema: str | None, name: str) -> Described | None:
        """Without a schema, looks in the current schema, then private and public synonyms."""
        if found := self._columns(schema, name):
            return found
        if schema is None and (targets := self._catalog(_SYNONYM, {"name": name})):
            found = self._columns(*targets[0])
            return replace(found, synonym=name) if found else None
        return None

    def raw_columns(self, table: Described) -> Result:
        sql = "SELECT * FROM all_tab_columns WHERE owner = :owner AND table_name = :name ORDER BY column_id"
        return self.execute(sql, {"owner": table.schema, "name": table.name})

    def _columns(self, owner: str | None, name: str) -> Described | None:
        rows = self._catalog(_COLUMNS, {"owner": owner, "name": name})
        if not rows:
            return None
        columns = [
            ColumnInfo(
                position=int(position),
                name=column,
                type=oracle_type(data_type, length, char_length, char_used, precision, scale),
                nullable=nullable == "Y",
                # data_default is a LONG holding the SQL text, often with trailing whitespace.
                default=default.strip() if default else None,
                pk=int(pk) if pk is not None else None,
                extra="identity" if identity == "YES" else "virtual" if virtual == "YES" else None,
                comment=comment,
            )
            for (_, position, column, data_type, length, char_length, char_used, precision, scale,
                 nullable, default, identity, virtual, pk, comment) in rows
        ]
        # The owner as resolved, i.e. the current schema when none was given.
        return Described(rows[0][0], name, columns)

    @classmethod
    def prepare(cls, sql: str) -> str:
        """Drop a trailing SQL*Plus "/" line, then the final ";" unless it's PL/SQL.

        Oracle before 23ai rejects a ";" after plain SQL (ORA-00911 or
        ORA-00933); 23ai accepts it. PL/SQL always needs it after END.
        """
        sql = remove_trailing_line(sql, "/", "oracle")
        return sql if is_plsql(sql) else remove_trailing_semicolon(sql, "oracle")

    def _start_readonly(self) -> None:
        with self.conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")

    def _new_cursor(self, arraysize: int) -> Any:
        cursor = super()._new_cursor(arraysize)
        # The default of 2 prefetched rows costs a second round trip for the first batch.
        cursor.prefetchrows = arraysize
        return cursor


def oracle_type(data_type: str, length: Any, char_length: Any, char_used: str | None,
                precision: Any, scale: Any) -> str:
    """Write a column type the way DDL would, e.g. VARCHAR2(20 CHAR), NUMBER(10,2).

    Byte-length strings get no suffix. NUMBER with no precision but scale 0
    is INTEGER, written NUMBER(*,0). TIMESTAMP and INTERVAL types already
    carry their precision in data_type.
    """
    if data_type in ("VARCHAR2", "CHAR"):
        return f"{data_type}({char_length}{' CHAR' if char_used == 'C' else ''})"
    if data_type in ("NVARCHAR2", "NCHAR"):
        return f"{data_type}({char_length})"
    if data_type == "NUMBER":
        if precision is None:
            return "NUMBER" if scale is None else f"NUMBER(*,{scale})"
        return f"NUMBER({precision},{scale})" if scale else f"NUMBER({precision})"
    if data_type == "FLOAT" and precision is not None:
        return f"FLOAT({precision})"
    if data_type in ("RAW", "UROWID"):
        return f"{data_type}({length})"
    return data_type
