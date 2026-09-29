# dprobe: implementation plan

Status: steps 1–6 done · updated 2026-09-28

dprobe is a command-line tool that:
- reads named database connections from a YAML file
- runs a SQL file against one of them and prints the results
- has commands for inspecting tables (like MySQL's `DESCRIBE`)
- supports bind variables

It targets Oracle (`oracledb`), SQL Server (`pymssql`) and MySQL (`mysql-connector-python`).

Steps 1–6 are built and tested against Oracle 23ai, SQL Server 2022, MySQL 8.4 (Percona) and MariaDB 11.5: 321 tests, 91 of them integration tests run through `tests/compose.yaml`. Section 10 lists what changed from the original plan and why.

## 0. Fixes before starting (done)

- **Remove `argparse` from `pyproject.toml`.** The PyPI package is a 2015 copy made for Python 2. Python 3.13 already loads the built-in `argparse` (checked in `.venv`), so the dependency does nothing.

## 1. Project layout

```
src/dprobe/
  __init__.py          # __version__
  __main__.py          # python -m dprobe
  cli.py               # argparse subcommands, exit codes
  config.py            # find/load/validate YAML, resolve secrets
  errors.py            # exceptions carrying exit codes, secret masking
  sqltext.py           # lexer (code / quoted / comment spans), statement-ending cleanup
  tty.py               # questions on /dev/tty (password and bind prompts)
  binds.py             # find :name placeholders (via sqltext), rewrite per driver, parse values
  output.py            # table / csv / tsv / json / jsonl
  connectors/
    __init__.py        # REGISTRY, create_connector()
    base.py            # Connector ABC, Result, TableInfo/ColumnInfo/IndexInfo/SchemaInfo/ResultColumn, Found
    oracle.py
    mssql.py
    mysql.py
tests/                 # unit tests; test_integration.py needs real databases
dprobe.example.yaml
```

- `pyproject.toml` has `[build-system]` (uv_build) and `[project.scripts] dprobe = "dprobe.cli:main"`, so `uv run dprobe …` and `uv tool install .` work.
- The hello-world `main.py` was deleted rather than moved.
- `pytest` is a dev dependency.

## 2. YAML config

The config file is found in this order: `--config PATH` → `$DPROBE_CONFIG` → `./dprobe.yaml` → `~/.config/dprobe/config.yaml` (`$XDG_CONFIG_HOME` is honored). A missing `--config` or `$DPROBE_CONFIG` file is an error rather than falling through to the next location.

```yaml
defaults:
  format: table
  max_rows: 1000

connections:
  hr-prod:
    driver: oracle
    url: db1.example.edu:1521/ORCLPDB1   # Easy Connect or TNS alias → oracledb dsn as-is
    user: hr_ro
    password: ${HR_PROD_PASS}            # env var substitution
    readonly: true

  sis:
    driver: mssql
    url: sqlsrv01:1433/sis               # host[:port][/database]
    user: svc_report
    password_cmd: secret-tool lookup db sis   # first line of stdout = password
    options: { encryption: require, tds_version: "7.4" }

  web:
    driver: mysql
    url: web-db:3306/webapp
    user: app_ro                         # no password → prompt if there's a terminal, else error
```

- **`url` for MySQL and SQL Server** is `host[:port][/database]`, with no scheme (`mysql://` is rejected).
  - The database part is optional, since both allow connecting without one.
  - IPv6 hosts need brackets: `[::1]:3306/db`.
  - A SQL Server named instance goes in the host: `sqlsrv01\SQLEXPRESS/sis`.
- **Override file:** as with Docker Compose, `<name>.override<suffix>` beside the config file (`dprobe.yaml` → `dprobe.override.yaml`) is merged over it when present. It's for passwords and other local changes, and `.gitignore` skips `*.override.yaml` / `*.override.yml`.
  - **Merging:** mappings merge key by key; other values replace; `null` removes a key. So `password_cmd: null` plus `password: …` swaps one for the other, and `<label>: null` drops a connection.
  - **Validation** runs on the merged data, with errors prefixed `dprobe.yaml with dprobe.override.yaml:`. `-v` shows both files.
- **`options`** is passed to the driver's `connect()`. It can fill in an argument dprobe leaves unset (e.g. `port`), but setting `host`, `user`, `password` and the like there is an error.
- **Validation:** each entry is loaded into a frozen `ConnectionConfig` dataclass. Errors name the file and field, e.g. `connections.sis.url: invalid port '99999'`.
  - Unknown keys are rejected, which catches typos like `pasword`.
  - Unquoted YAML values that aren't strings (`password: 12345`, `yes`, dates) in string fields are rejected with "put it in quotes".
- **`${VAR}`** is expanded in `url`, `user`, `password` and `options`, but only for the label being used. So an unset variable for one label doesn't break the others or `dprobe labels`. `password_cmd` isn't expanded, because the shell does that.
- **Passwords** are resolved in this order: `password`, then `password_cmd`, then an interactive prompt.
  - Setting both `password` and `password_cmd` is an error.
  - `password_cmd` runs through the shell, and the first line of stdout is the password (so multi-line `pass show` entries work). Its stderr isn't captured, so gpg or `op` prompts still show. A non-zero exit is an error.
  - The prompt reads `/dev/tty`, so it works even when the SQL comes from stdin.
  - A connection with no `user` gets no password and no prompt, which leaves authentication to `options` (e.g. an Oracle wallet).
  - A plaintext password is allowed, with no warning, whatever the file's permissions: some setups keep passwords in the file on purpose. `${VAR}` keeps it out of the file instead.
  - Passwords are masked (`***`) in all error and `--verbose` output.
  - `dprobe.yaml` is in `.gitignore`; only `dprobe.example.yaml` is committed.
- **MariaDB needs `options: {collation: utf8mb4_general_ci}`.** mysql-connector 26.x asks for `utf8mb4_0900_ai_ci`, which MariaDB doesn't have (error 1273). The connect error suggests this fix.
- **`keyring: SERVICE`** (step 6) reads the password from the OS keyring, stored under the connection's `user`.
  - It sits after `password_cmd` in the order, and only one of `password`, `password_cmd` and `keyring` may be set. `keyring` needs a `user`.
  - `dprobe keyring LABEL` prompts for the password and stores it; `--delete` removes it.
  - The `keyring` package is an optional extra, `dprobe[keyring]`, so the base install doesn't grow.
  - Headless Linux often has no backend: Secret Service needs a desktop session's D-Bus, and inside the sandbox keyring picked `fail.Keyring`. That failure is reported as "no usable keyring backend?".

## 3. Connector layer

```python
class Connector(ABC):
    # per driver
    def _import_driver(self) -> ModuleType          # lazy import of the DB-API module
    def _connect_args(self) -> dict                 # from url/user/password, merged with options
    def server_version(self) -> str
    @classmethod
    def prepare(cls, sql) -> str                    # statement-ending cleanup; --native skips it
    # shared
    def execute(self, sql, params=None, *, max_rows=None) -> Result
    def commit(self) -> None                        # closing without it rolls back
    @classmethod
    def placeholder(cls, name) -> tuple[str, str]   # how :name is sent: SQL text and params key
    @classmethod
    def identifier(cls, text, quoted) -> str        # Oracle upper-cases unquoted names
    def list_tables(self, schema, like, views) -> list[TableInfo]
    def describe(self, schema, name) -> Found[ColumnInfo] | None
    def raw_columns(self, table: Found) -> Result
    def indexes(self, schema, name) -> Found[IndexInfo] | None
    def schemas(self) -> list[SchemaInfo]
    def describe_result(self, sql, params) -> list[ResultColumn]   # --meta
```

`Result` has `columns` (None for statements without rows), `affected` (changed-row count, or None when the driver's rowcount doesn't mean that, e.g. DDL), `rows()` and `has_more_results()`, which stays False until every row has been read.

Drivers customize behavior through small hooks and one class attribute:
- `_start_readonly`: begin the read-only transaction
- `_error_text`: clean up the driver's error message
- `_error_hint`: advice appended to connect and query errors
- `_new_cursor`: cursor setup
- `_iter_rows`: how rows are fetched
- `_has_more_results`: whether a batch has more result sets
- `ddl_autocommits`: whether rollback can undo DDL

- **Lazy imports:** each connector imports its own driver when it connects. If one driver is broken, only its labels fail, and startup stays fast.
- **Safe by default:** autocommit is off, and every run rolls back unless `--commit` is passed. `--commit` on a `readonly` connection is a usage error.
- **`readonly: true`** starts a read-only transaction: `SET TRANSACTION READ ONLY` on Oracle, `START TRANSACTION READ ONLY` on MySQL.
  - This blocks DML only. DDL commits implicitly and still runs.
  - SQL Server has no read-only transaction, so there `readonly` only blocks `--commit`. DML still runs and holds locks until the rollback.
  - For a real guarantee, use a read-only database account.
- **Oracle settings:**
  - `oracledb.defaults.fetch_lobs = False`, so CLOBs and BLOBs come back as plain str/bytes, not LOB objects.
  - `fetch_decimals = True`, so NUMBER values keep every digit. JSON output writes them as exact numbers (section 7).
- **Fetching:** rows are fetched 500 at a time; pymssql and mysql-connector default to 1 row per round trip.
  - With a row limit, the batch size is `max_rows + 1` (capped at 5000), so the "more rows?" check doesn't cost another round trip.
  - Oracle sets `prefetchrows` to the batch size, so the first batch comes back with the execute call: 1 round trip instead of 2 (counted in `v$mystat`).
- **Rollback happens by closing.** Oracle, MySQL and SQL Server all discard an open transaction on disconnect, so there's no explicit `rollback()` call.
- **Driver quirks found on real servers:**
  - **MySQL, unread rows:** when output stops at `max_rows`, `close()`, `commit()` and `rollback()` all read every remaining row first. That took ~30 s for 3M rows on MySQL 8.4. The C extension doesn't implement `shutdown()`. So dprobe sends `KILL QUERY` from a second connection, which takes 0.01 s and leaves the first connection usable. With `--commit` it reads the rest instead, so the statement finishes before committing.
  - **pymssql, result sets:** `fetchmany()` runs on into the next result set of a batch. dprobe fetches with `fetchone()`, which returns `None` once at each result-set boundary.
  - **pymssql, rowless results:** pymssql already skips results with no columns, so `DECLARE @x int = 5; SELECT @x` shows the SELECT. Only the first result set is shown, with a warning if more follow.
  - **pymssql errors** arrive as `(code, b"...")` with every message repeated. dprobe flattens them into one line.
  - **Precision:** values keep the driver's Python types, so digits beyond microseconds (`DATETIME2`, `TIMESTAMP(9)`) are lost.

## 4. Commands

```
dprobe query    <label> <file.sql | -> | -e SQL
                [-f table|csv|tsv|json|jsonl] [-o out] [--max-rows N]
                [--max-width N] [--null TEXT] [--native] [--commit]
                [-b name[:type]=value ...] [--binds f.yaml|json] [--dry-run] [--meta]
dprobe tables   <label> [--schema S] [--like PAT] [--views]
dprobe describe <label> [schema.]table [--raw]
dprobe indexes  <label> [schema.]table
dprobe schemas  <label> [--all]
dprobe ping     <label> [<label> ...]   # connect, server version, timing
dprobe labels                           # list connections, no secrets
```

`--config` and `-v/--verbose` work before or after the command name. All the metadata commands take the same `-f`, `-o`, `--max-width` and `--null` options as `query`.

- **SQL input:** exactly one of a file, `-` for stdin, or `-e SQL`.
  - Files and stdin are read as UTF-8, and a byte-order mark (SSMS, Notepad) is dropped.
  - Anything else, e.g. UTF-16 from SSMS "Unicode" saves, is rejected with a hint to save as UTF-8.
- **UTF-8 everywhere, whatever the locale.** Under `LC_ALL=C` (e.g. in cron), Python would otherwise default to ASCII and fail on the first `é`.
  - The config file, SQL files, stdin, stdout, stderr and `password_cmd` output are all UTF-8.
  - Connections ask the server for UTF-8: `charset="UTF-8"` for pymssql, `utf8mb4` for MySQL (its plain `utf8` can't hold emoji), and oracledb's thin mode is always UTF-8.
  - An integration test sends `naïve ✓ 日本 😀` through all four databases and back.
- **`--native`** sends the SQL exactly as written, with no binds and no changes. See section 6.2.
- **`--max-rows N`:** the default is `defaults.max_rows` (1000) for table and no limit for the other formats. `0` means no limit. When output is cut short, the status line says so.
- **`--max-width N`:** table cells are cut to N characters (default 80) with `…`. `0` means no limit. The other formats always show full values.
- **`--null TEXT`:** the text for NULL. The default is `NULL` in table and empty in csv/tsv. JSON always writes `null`.
- **`--meta`** shows the columns a query returns: `position`, `name`, `type`, `nullable`, `size`, `precision`, `scale`.
  - **Oracle** only parses the statement (`cursor.parse()`), so nothing runs. Types are oracledb's names (`NUMBER`, `VARCHAR`, `DATE`). An unconstrained NUMBER's precision 0 and scale -127 are shown blank.
  - **SQL Server** asks `sp_describe_first_result_set`, so nothing runs either, and types are full SQL types (`varchar(20)`, `decimal(10,2)`). Bind values are first filled into the text with pymssql's own `substitute_params`.
  - **MySQL** has no way to describe without running, so dprobe runs the statement and reads `cursor.description` without fetching. It therefore only takes a read (`SELECT`, `WITH`, `SHOW`, …), because DML would run and DDL would commit. Types are mysql-connector's field names (`LONG`, `VAR_STRING`).
  - A statement without columns (e.g. an UPDATE on Oracle or SQL Server) reports `no result columns`.
- **`--dry-run`** prints the SQL exactly as it would be sent, then one `-- bind name = value (type)` line per value, and stops. It doesn't connect or ask for a password.
- **Large results:** csv, tsv, json and jsonl stream rows in batches. The table format needs every row to size its columns, so it stops at `max_rows`.
- **`-o FILE`** is only opened after the query succeeds, so a failed query doesn't create or overwrite it.
- **Status goes to stderr, so stdout carries only data.** Examples:
  - `(3 rows, 12 ms)`
  - `(first 1000 rows, 40 ms; --max-rows 0 shows all)`
  - `(2 rows affected; rolled back (use --commit to keep changes), 5 ms)`
  - `(statement executed; rolled back (this database commits DDL regardless), 9 ms)` on Oracle and MySQL. SQL Server says `use --commit to keep changes` instead, because its DDL does roll back.

  stdout is flushed first, so the status stays after the rows when both streams go to one pipe.
- **Statement cleanup** (default mode only; `--native` changes nothing), applied to each statement:

  | | removed | kept |
  |---|---|---|
  | Oracle | a trailing SQL*Plus `/` line; the final `;` | `;` after PL/SQL: `BEGIN`/`DECLARE` blocks and `CREATE [OR REPLACE] PROCEDURE/FUNCTION/PACKAGE/TRIGGER/TYPE` |
  | SQL Server | a trailing `GO` line, which SQL Server can read as a column alias | `;`, which `MERGE` requires |
  | MySQL | nothing | `;`, which MySQL accepts |

  - The lexer finds these endings, so they're removed even when comments follow them. A `;`, `/` or `GO` inside a comment or string is never touched.
  - Oracle 23ai accepts a trailing `;` (checked on 23.26). Earlier versions reject it with ORA-00911 or ORA-00933.
  - SQL Server batches such as `DECLARE …; SELECT …` run as one batch.
- **Several statements, `--script`** (step 6):
  - **Splitting:** the file is split by the lexer, so a `;`, `/` or `GO` in a string or comment never splits.
    - Oracle and MySQL split after each `;`.
    - An Oracle PL/SQL block (`BEGIN`, `DECLARE`, `CREATE PROCEDURE`, …) runs to a `/` line, as in SQL*Plus, and a stray `/` after plain SQL is ignored.
    - SQL Server splits on `GO` lines into batches.
    - MySQL's `DELIMITER` is a mysql-client command and is rejected.
  - **Running:** statements run in order in one transaction. The first error stops the script and nothing is committed, even with `--commit`. The error is prefixed with `statement N, line L`, where L is the line of the statement's first code, not its leading comment.
  - **Without `--script`**, a file with several statements is a usage error naming their lines, rather than the database's syntax error.
  - **Binds:** they're resolved once for the whole script, so a name used twice is asked for once, and every missing one is reported together.
  - **Not allowed with `--script`:** `--native` and `--meta`.
- **Several result sets** (step 6): every result set a statement returns is printed in turn: a SQL Server batch, or an Oracle PL/SQL block using `DBMS_SQL.RETURN_RESULT` (read with `cursor.getimplicitresults()`). MySQL `CALL`s that return result sets aren't read.
  - **`--max-rows`** applies to each result set. A set cut short has its unread rows dropped (with `KILL QUERY` on MySQL), so the next statement can run, and further sets of that statement aren't read.
  - **Status:** a single result keeps its one-line status. Otherwise each result gets a line as it finishes, `(result 2: 3 rows)` or `(statement 2, line 5: 3 rows)`, and a total follows, e.g. `(3 statements; rolled back (use --commit to keep changes), 40 ms)`.
- **Exit codes:** 0 success, 1 SQL error, 2 usage/config error (argparse's default), 3 connection/login error, 130 Ctrl-C. If the output pipe closes early (e.g. `| head`), dprobe exits with 1 and no traceback. `--verbose` shows tracebacks.

## 5. Metadata queries (built in steps 4–5)

Every metadata command returns the same dataclasses, so `tables` and `describe` look the same on all three databases and work with every output format.
- **`tables`:** `schema`, `name`, `type` (`TABLE` or `VIEW`), `comment`.
- **`describe`:** `position`, `name`, `type`, `nullable`, `default`, `pk`, `extra`, `comment`.
  - `type` is written the way DDL would write it: `VARCHAR2(20 CHAR)`, `NUMBER(*,0)`, `nvarchar(max)`, `decimal(10,2)`.
  - `default` is the default's SQL text (`'x'`, `0`, `CURRENT_TIMESTAMP`), not its value.
  - `pk` is the column's position in the primary key, blank if it isn't part of it.
  - `extra` is `identity`, `auto_increment`, `virtual`, `computed`, `on update CURRENT_TIMESTAMP`, …
- **`indexes`:** `name`, `columns`, `unique`, `primary`, `type`, `include`. The primary key comes first, then the rest by name.
  - `columns` is in key order, written as in DDL: `amount DESC`, `note(10)` (a MySQL prefix), `LOWER("EMAIL")` (an Oracle function-based index).
  - `include` holds SQL Server's included columns.
  - The table is found exactly as `describe` finds it, synonyms included. One query returns the table and its indexes, so a table without indexes shows `0 indexes` rather than "not found".
- **`schemas`:** `name`, `current`, `system`. System schemas are hidden unless `--all` is given, and the status line says how many were hidden.

Catalog queries pass schema and table names as bind variables, never as pasted text. Column types are formatted in Python (`oracle_type`, `mssql_type`), so they're unit-tested without a server.

| | MySQL | SQL Server | Oracle |
|---|---|---|---|
| columns | `information_schema.columns` (`column_type`, `extra`, `column_comment`) | `sys.columns` + `sys.default_constraints` + `sys.extended_properties` (MS_Description) | `all_tab_cols` (for `virtual_column`; hidden columns skipped) + `all_col_comments` |
| primary key | `information_schema.statistics`, index `PRIMARY` | `sys.indexes` (`is_primary_key`) + `sys.index_columns.key_ordinal` | `all_constraints` (type `P`) + `all_cons_columns.position` |
| tables | `information_schema.tables` (`BASE TABLE`, MariaDB `SYSTEM VERSIONED`, `VIEW`) | `sys.objects` types `U`, `V`, not `is_ms_shipped` | `all_tab_comments` alone (it lists every table and view; `BIN$` recycle-bin names skipped) |
| default for `tables` | the url's database | every schema in the current database | the current schema |
| name lookup in `describe` and `indexes` | exact, in the url's database | `OBJECT_ID()`: default schema, then `dbo` | current schema, then a private synonym, then a public one |
| indexes | `information_schema.statistics` (`expression` on MySQL 8.0.13+, not MariaDB; `sub_part`; collation `D`) | `sys.indexes` + `sys.index_columns` (`is_included_column`, `is_descending_key`); heaps skipped | `all_indexes` (LOB indexes skipped) + `all_ind_columns` + `all_ind_expressions`, primary via `all_constraints` |
| schemas | `information_schema.schemata`; system: `information_schema`, `mysql`, `performance_schema`, `sys` | `sys.schemas` of the current database; system: `sys`, `INFORMATION_SCHEMA`, `guest`, the `db_*` role schemas | `all_users`, `oracle_maintained` as system |

Gotchas:
- **Oracle `DESCRIBE`** is a SQL*Plus command, not SQL, so dprobe can't pass it through.
- **Oracle views:** use `ALL_*` rather than `DBA_*`, which need extra privileges. `ALL_*` shows only what the user can access.
- **Oracle name case:** Oracle stores unquoted names in uppercase, so dprobe uppercases input unless it's in `"quotes"`. That's the same rule SQL follows, so `describe x '"MixedCase"'` finds a table created with a quoted name.
- **Oracle synonyms:** in systems like Banner, most tables are reached through public synonyms (e.g. `SPRIDEN` for `SATURN.SPRIDEN`). `describe` follows one private or public synonym (database links are skipped), and the status line says `via synonym SPRIDEN`.
- **Oracle `data_default`** is a LONG column holding SQL text, often with trailing whitespace. It reads fine but can't be used in a `WHERE` clause.
- **SQL Server:** `sys.*` is used instead of `INFORMATION_SCHEMA`, because only `sys.*` shows identity columns and column comments. Defaults come wrapped in parentheses, e.g. `((0))`, which are removed when balanced. `sp_help` returns several result sets, which are awkward to parse.
- **MySQL:** table names are case-sensitive on Linux, and `describe` matches them exactly, as the server would.
  - A text default is stored as its bare value, so dprobe quotes it; numbers and expressions stay as they are. The internal `DEFAULT_GENERATED` flag is dropped from `extra`.
  - MariaDB quotes defaults itself, writes a NULL default as the text `NULL`, and still reports integer display widths such as `int(11)`, which MySQL 8.0.19 dropped.
- **MySQL without a database in `url`:** `tables` needs `--schema` and `describe` needs `schema.table`; the error says so.
- **Round trips:** `tables` and a `describe` of a table take one query each on every database.
  - The current schema is resolved inside the query (`NVL(:owner, SYS_CONTEXT(...))` on Oracle); on MySQL it's the url's database.
  - SQL Server resolves the name with `OBJECT_ID()` in the same query as the columns.
  - An Oracle synonym costs two more queries, after the direct lookup finds nothing.
  - Catalog queries fetch 5000 rows per round trip.
- **Not found:** `describe` looks for the same name in any case (as a `LIKE` pattern) and suggests it, e.g. `no table or view orders; did you mean sales.orders?`. This catches a wrong-case name on MySQL, a SQL Server table outside `dbo`, and an Oracle table created with a quoted name.
- **MySQL `schemas`** lists only the schemas the user has privileges on; `information_schema` is always visible.
- **Oracle `schemas`** comes from `all_users`, so it lists every user, including those that own nothing.
- **Oracle descending indexes** are stored as function-based ones on `"COLUMN"`, so dprobe drops quotes around a bare column name: `AMOUNT DESC`.
- **`--raw`** on `describe` shows the catalog's own rows: `all_tab_columns`, `information_schema.columns`, or `sys.columns` plus `type_name`.

## 6. Bind variables (built in steps 3 and 5)

Driver bind styles (checked in `.venv`):

| driver | `paramstyle` | placeholder |
|---|---|---|
| oracledb | `named` | `:emp_id` |
| pymssql | `pyformat` | `%(emp_id)s` |
| mysql-connector | `pyformat` | `%(emp_id)s` |

### 6.1 Default mode: `:name` everywhere, translated by dprobe

Write `:name` in every SQL file. dprobe translates it for the target driver, so the same file runs on any database.

- **How it works:** `binds.py` finds `:name` in the code spans from the lexer in `sqltext.py`. The lexer already skips:
  - string literals (`'..''..'`, `N'..'`, Oracle `q'[..]'`, MySQL backslash escapes and `"…"` strings)
  - quoted identifiers (`"…"`, `` `…` ``, `[…]`)
  - comments: `--`, `/* */`, MySQL `#`, and SQL Server's nested `/* */`. In MySQL, `--` only starts a comment when followed by whitespace, so `5--3` is arithmetic.

  `binds.py` also skips a `:` that follows a letter, digit or another colon, so `:=` (PL/SQL assignment), `::` (old SQL Server function syntax) and `12:30` aren't binds.
- **DDL is never scanned** (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `GRANT`, `REVOKE`, `COMMENT`, `RENAME`). None of the three databases accepts bind variables in DDL, and Oracle trigger bodies use `:NEW` and `:OLD`, which aren't binds.
- **Names are case-insensitive**, as in Oracle: `:ID`, `:id` and `-b Id=1` are the same bind.
- **For MySQL and SQL Server**, each `:name` becomes `%(name)s`.
- **No `%` escaping needed:** both drivers were tested. When binds are passed as a dict, both leave other `%` sequences alone, e.g. `LIKE 'A%'`, `DATE_FORMAT(d, '%Y-%m-%d')`, `'100%'`, even `'%s'`.
  - pymssql quotes strings as `N'…'` (Unicode-safe) with doubled quotes; mysql-connector backslash-escapes them. Both insert the values into the SQL text on the client.
  - The one trap: both drivers also replace a literal `%(name)s` inside a string, and there's no escape for it.
- **Per driver:** each connector's `placeholder(name)` class method decides the text and params key, so `binds.py` holds no driver knowledge.
- **For Oracle**, each `:name` is sent as `:name_`. Oracle rejects any of its 104 reserved words as a bind name (ORA-01745), and common names are among them: `:date`, `:level`, `:size`, `:user`, `:mode`, `:uid`. None of the reserved words ends in `_`. Quoted names (`:"date"`) don't work, because oracledb can't match them to values. `--dry-run` shows the renamed SQL.
- **Only the names a statement uses are passed**, because oracledb rejects extra names (DPY-4008). A `-b` value the statement doesn't use gets a warning, in case it's a typo. Extra values in a `--binds` file are ignored silently, so one file can serve several queries.
- **Missing values:** every missing name is reported in one usage error (exit 2).
- **Also in this mode:** the statement cleanup from section 4.
- **Size:** about 60 lines on top of the lexer, and the part with the most unit tests.

### 6.2 Native mode: `--native`, no binds and no changes (built in step 2)

For testing a single hand-written query exactly as it will run elsewhere. dprobe treats the file as a plain string.

- **The text is sent byte for byte:** no `:name` rewriting, no removal of `;`, `/` or `GO`, and no `@bind` header processing. Only a file's byte-order mark is dropped, since that's encoding, not SQL.
- **No bind values:** `cursor.execute(sql)` is called with no parameters. Passing `--native` together with `-b` or `--binds` is a usage error (exit code 2).
- **The drivers leave the text alone too:** when no parameters are passed, mysql-connector and pymssql do no `%` substitution. Checked on real servers: `'A%'` and `'50%%'` come back exactly as written.
- **Hint:** if a native-mode statement fails and default mode would have changed it, the error adds "without --native, dprobe removes a trailing ';', '/' or 'GO'". Oracle 23ai runs a trailing `;` fine, so on 23ai native mode can't warn you that the same query would fail on 19c.
- **`--dry-run`** prints the SQL unchanged.

### 6.3 Where values come from

Default mode only. Highest priority first:
1. **`-b name[:type]=value`** on the command line (can be repeated). Only the first `=` splits, so `-b s=a=b` binds `"a=b"`.
2. **`--binds file.yaml|json`**, a mapping of names to values. The file is read as JSON when it ends in `.json`, otherwise as YAML.
   - YAML already gives you ints, floats, booleans, dates (`2024-01-01` → `date`), datetimes and `null`.
   - A key can carry a type, e.g. `"amount:decimal": "12.50"`, and the value is then converted from its text.
   - Lists and mappings are rejected (list expansion is in 6.5).
3. **`-- @bind NAME [TYPE] [= DEFAULT]` comments** in the SQL file. They're usually at the top, but any `--` comment counts:
   ```sql
   -- @bind emp_id int
   -- @bind start_date date = 2024-01-01
   SELECT * FROM emp WHERE emp_id = :emp_id AND hired >= :start_date
   ```
   - **A declared type** also converts values that arrive as untyped text: `-b emp_id=5` becomes the int 5, as does a string value in a `--binds` file. An explicit `-b emp_id:str=5` or a non-string file value is left alone.
   - **A default** fills a value nobody gave. Quotes keep leading or trailing spaces: `= ' x '`.
   - **Checking:** a malformed line or an unknown type is a usage error, and a declared name the statement doesn't use gets a warning, like an unused `-b`.
4. **A prompt** on the terminal for anything still missing, one name at a time in order of first use, e.g. `:emp_id (int): `.
   - **Invalid answers:** a value that doesn't convert is re-asked with the reason, and Ctrl-D cancels.
   - **Why `/dev/tty`:** the prompt reads there like the password prompt, so it works with SQL on stdin.
   - **Order:** bind prompts come before connecting, so before any password prompt.
   - **Without a terminal:** dprobe stops with one error listing every missing name.

### 6.4 Value types

- **Command-line values are strings** unless typed, e.g. `-b id:int=100` or `-b d:date=2024-01-01`.
- **Types:** `str int float decimal date datetime bool null`.
  - `date` and `datetime` take ISO format (`2024-01-02`, `2024-01-02 03:04:05` or with `T`).
  - `bool` takes `true`/`false`/`1`/`0` in any case; `yes`/`no` are rejected as ambiguous.
  - `null` takes no text: `-b x:null=`.
  - A bad value names the argument, e.g. `-b id:int=abc: 'abc' is not a valid int`.
- **Command-line values aren't parsed as YAML.** PyYAML turns `01234` into 668 (octal) and `NO` into `False`. In a `--binds` YAML file the same applies, so quote such strings or add a `:str` type to the key.
- **Why offer types:** letting the database convert strings mostly works, but it can prevent index use (e.g. Oracle converting a string to a number), which slows queries.

### 6.5 Lists (step 6)

- **`TYPE[]`** makes a list: `-b ids:int[]=1,2,3`, or `[]` for text. Elements are comma-separated and parsed like CSV, so `"a,b",c` holds a comma. A bind file can give a YAML/JSON list (with a `:int[]` key type to convert it), and `-- @bind ids int[] = 1,2` declares one.
- **Expansion:** a list value turns each `:ids` into one placeholder per element, `:ids__0, :ids__1, …`, for `IN (:ids)`. Oracle's are sent as `:ids__0_`.
- **Errors:** an empty list is an error, since `IN ()` isn't valid SQL, and so is an element name that clashes with another bind. Oracle allows at most 1000 items in an IN list (ORA-01795).

### 6.6 Later options

- **`--define name=value` with `{{name}}` text substitution**, for schema or table names, which can't be bound. This inserts raw text and gives no SQL-injection protection. Keep it separate from binds, clearly named, and off by default.

## 7. Output

Plain Python, no new dependencies.

- **table:** aligned columns under a dashed rule.
  - Columns holding only numbers are right-aligned, headers included.
  - Widths are display widths, so CJK characters and emoji (two terminal columns) and combining marks (none) keep rows aligned.
  - Newlines and tabs in values show as `\n` and `\t`.
  - Cells are cut at `--max-width` with `…`.
  - On a terminal (stdout is a tty, no `-o`), the widest columns also shrink until a row fits the terminal's width, but never below 6 columns; pipes and files get every column in full.
- **csv / tsv:** the `csv` module, with a header row and `\n` line endings (not the module's default `\r\n`).
- **Several result sets:** table, csv and tsv put a blank line between them; json writes one array per set (a stream `jq` reads); jsonl carries on with the rows.
- **json / jsonl:** rows are encoded by hand rather than with `json.dumps(default=…)`, so Decimals stay exact bare numbers (`12.50`, not `"12.50"` or `12.5`).
  - `json` is an array with one object per line; `jsonl` is one object per line.
  - Non-ASCII text is written as UTF-8, not `\u` escapes.
  - Keys are unique: `SELECT a.id, b.id` gives `id`, `id_2`, and unnamed columns (SQL Server `SELECT 1`) become `column1`, `column2`, …

Value conversions:

| value | text formats | JSON |
|---|---|---|
| NULL | `--null` text | `null` |
| bool | `true` / `false` | `true` / `false` |
| bytes (BLOB, RAW, VARBINARY) | `0x6162` | `"0x6162"` |
| Decimal | plain digits, never exponent (`1E+2` → `100`) | bare number |
| datetime | `2024-01-02 03:04:05` | `"2024-01-02T03:04:05"` |
| timedelta (MySQL TIME, Oracle INTERVAL) | `25:00:00`, hours not wrapped into days | `"25:00:00"` |
| MySQL SET | `a,b` | `["a", "b"]` |
| dict/list (Oracle JSON) | JSON text | nested JSON |
| NaN / Infinity | as text | string (not valid JSON numbers) |

`rich` or `tabulate` can come later if nicer tables are wanted.

## 8. Testing

- **Unit tests (no database):**
  - lexer and statement cleanup per database (`test_sqltext.py`)
  - config loading, substitution and errors; password sources
  - value conversions and every output format (`test_output.py`)
  - the CLI, run against SQLite through a small test connector, so it exercises real DB-API behavior without a server: formats, `-o`, stdin, `--max-rows`, rollback vs `--commit`, readonly, `--native`, usage errors
  - pymssql error flattening and `options` merging
  - binds (`test_binds.py`): which `:name`s count as placeholders in each dialect, DDL skipping, rewriting per driver, missing-name errors, every value type and its errors, YAML/JSON bind files
  - binds in the CLI: `-b` over `--binds`, unused-value warnings, `--native` rejecting binds, `--dry-run` not connecting
  - `@bind` declarations and their errors, the value priority with declared types, and prompting (re-ask on a bad value, Ctrl-D cancelling)
  - type and default formatting for each database (`test_catalog_types.py`), dotted-name splitting, and `tables`/`describe` in the CLI through SQLite's `sqlite_master` and `pragma_table_info`
- **`./test.sh`** runs the unit tests.
  - `-i` also starts the compose databases (idempotent, waiting for health checks) and runs the integration tests.
  - `--down` stops the databases afterwards.
  - Any other arguments go to pytest.
- **Integration tests** (`@pytest.mark.integration`): every `it-*` label in the config named by `DPROBE_IT_CONFIG` runs each test, which creates and drops `dprobe_*` tables.
  ```
  docker compose -f tests/compose.yaml up -d --wait
  DPROBE_IT_CONFIG=tests/it.example.yaml uv run pytest
  docker compose -f tests/compose.yaml down
  ```
  - `tests/compose.yaml` runs the four images below with health checks, so `--wait` returns once each one accepts connections. They need about 5 GB of memory.
  - `tests/it.example.yaml` holds the matching labels.
  - Verified with `gvenzl/oracle-free:23-slim-faststart` (23.26), `mcr.microsoft.com/mssql/server:2022-latest`, `percona/percona-server:8.4` and `mariadb:latest` (11.5).
  - Covered: `ping`; awkward types as JSON (CLOB, DECIMAL, RAW/VARBINARY, DATE, NULL); `%` unchanged in native mode; binds next to `LIKE 'b%'`; bound int, decimal, NULL and a string with a quote, backslash, Unicode and emoji all round-tripping; reserved-word bind names (`:date`, `:level`); `tables`, `tables --views`, and `describe` of a table (identity, composite key, default, comments) and a view; `--raw`; name case per database; an Oracle public synonym to another schema (needs an `admin-oracle` label); `indexes` with a composite primary key, descending, unique, prefix (MySQL) and included-column (SQL Server) indexes, and on a table without any; `schemas` with and without `--all`; `--meta` with binds, and `--meta` on an UPDATE leaving the data untouched; declared binds; Oracle's trailing `;` by server version; stopping early on a 300,000-row result in under 5 s; rollback vs `--commit`; readonly blocking DML; SQL Server batches showing every result set.
  - Step 6: scripts on each database (Oracle SQL plus PL/SQL with `/`, MySQL `;`, SQL Server `GO` batches) rolling back; every result set of a SQL Server batch; Oracle implicit results; list binds.

## 9. Build order

1. **Done.** Fixes from section 0, src layout and entry point, config loader, `labels` and `ping`.
2. **Done.** `query` with table/csv/tsv/json/jsonl output and `--native`, no binds yet.
3. **Done.** Bind layer: tokenizer, `-b`, `--binds`, typed values, `--dry-run`.
4. **Done.** `describe` and `tables` for all three databases.
5. **Done.** `indexes`, `schemas`, `--meta`, `@bind` declarations in SQL files, prompting.
6. **Done.** Extras:
   - list expansion
   - several statements per file (`--script`)
   - `keyring`
   - Oracle implicit results (`DBMS_SQL.RETURN_RESULT`)
   - showing every result set of a SQL Server batch
   - a compose file for the integration databases

## Decisions

- **Raw drivers vs SQLAlchemy:** decided, raw drivers. SQLAlchemy's `text()` and `Inspector` would replace most of sections 5–6.1. But it's a large dependency, gives less control over output, and adds a layer between dprobe and the driver. That layer would also have hidden the driver quirks in section 3.
- **Plaintext passwords in YAML:** decided, allowed without a warning. `${VAR}` covers setups that want the password out of the file.
- **Default `max_rows`:** decided, 1000 for table and no limit for the other formats.

## 10. Changes from the original plan

Made while building steps 1 and 2:

1. **The trailing `;` is removed for Oracle only.** SQL Server's `MERGE` requires it and MySQL accepts it. (Section 4)
2. **A trailing `GO` line is removed for SQL Server.** Left in, SQL Server can read `GO` as a column alias and silently rename a column. (Section 4)
3. **The native-mode hint is generic.** It fires when default mode would have changed the failing statement, rather than being an Oracle `;` check. Oracle 23ai accepts a trailing `;` anyway (checked on 23.26: the server receives it and runs the statement). (Sections 4, 6.2)
4. **The lexer lives in its own `sqltext.py`.** Step 2's cleanup needed it to skip comments and strings, and step 3's bind tokenizer builds on it. `errors.py` was also added. (Section 1)
5. **`fetch_decimals` is on, not optional.** JSON output writes Decimals as exact numbers, so nothing is lost. (Sections 3, 7)
6. **New `--max-width`** (default 80) keeps table output readable when a column holds long text. (Section 4)
7. **Status and row counts go to stderr**, so stdout carries only data; `-o` is only opened after the query succeeds. (Section 4)
8. **Config details:**
   - the database in `url` is optional for MySQL and SQL Server
   - `password` plus `password_cmd` is an error
   - no `user` means no prompt
   - `${VAR}` is expanded only for the label being used
   - `ping` takes several labels
   - (Section 2)
9. **MariaDB needs a collation option** because of the driver's default collation. (Section 2)
10. **MySQL uses `KILL QUERY` to stop unread results** instead of reading them or calling `shutdown()`, which the C extension lacks. (Section 3)
11. **pymssql rows are fetched with `fetchone()`** because `fetchmany()` crosses result-set boundaries. Only the first result set of a batch is shown, with a warning. (Section 3)
12. **Time values print as `H:MM:SS`, and JSON keeps non-ASCII text**, replacing Python's `1 day, 1:00:00` and `\u` escapes. (Section 7)
13. **Integration tests use real servers** (Oracle 23ai, SQL Server 2022, MySQL 8.4, MariaDB 11.5) selected by `it-*` labels, instead of the planned `mysql:8` image. CLI unit tests use SQLite as a stand-in database. (Section 8)
14. **A cleanup pass after step 2:**
    - `commit()` replaces `finish(commit)`, and rollback happens by closing.
    - "Rows affected" and "DDL commits anyway" are decided by the connector, not the CLI.
    - Fetch sizes follow `max_rows`, and Oracle prefetches its first batch.
    - JSON output reuses one encoder (about 6x faster on 1M values).
    - `/` and `GO` removal ignores trailing comments.
    - (Sections 3, 4)
15. **UTF-8 is forced for all input, output and connections** instead of following the locale. (Section 4)
16. **Step 3 additions:**
    - Oracle binds are sent as `:name_` to get around reserved-word bind names.
    - DDL isn't scanned for binds.
    - Bind names are case-insensitive.
    - Unused `-b` values get a warning.
    - Bind-file keys can carry a `:type`.
    - (Section 6)
17. **Step 4 additions:**
    - `describe` follows Oracle synonyms, resolves SQL Server names with `OBJECT_ID()`, and suggests near matches when nothing is found.
    - MySQL text defaults are quoted to match the others.
    - Booleans print as `true`/`false` in every format.
    - `tables` on SQL Server lists every schema by default.
    - (Sections 5, 7)
18. **A cleanup pass after step 4:**
    - `paramstyle` is replaced by `placeholder()`, and `identifier()` is a class method, so names are checked before connecting.
    - One `read_utf8()` reads the config, SQL and bind files (bind files now accept a byte-order mark).
    - The catalog queries are one round trip each.
    - (Sections 3, 5, 6.1)
19. **Step 5 additions:**
    - `--meta` doesn't run the query on Oracle (parse) or SQL Server (`sp_describe_first_result_set`); on MySQL, which must run it, it takes only reads.
    - `schemas` hides system schemas unless `--all`.
    - Declared types also convert untyped `-b` and file values.
    - Prompts use a shared `tty` module.
    - `Described` became the generic `Found`, shared by `describe` and `indexes`.
    - (Sections 3–6)
20. **Step 6 additions:**
    - Several statements run only with `--script`; without it, a multi-statement file is a clear usage error.
    - Every result set is printed, replacing the "only the first is shown" warning.
    - List elements are named `name__N`.
    - `keyring` is an optional extra with a `dprobe keyring LABEL` command.
    - `bind()` no longer returns the names it used; the CLI collects names across the whole script first.
    - (Sections 2, 4, 6.5, 7, 8)
21. **A cleanup pass after step 6:**
    - **Splitting is linear:** `split_statements` lexes once and uses binary search. A 4.7 MB, 50,000-statement script splits in 0.2 s; 2,000 Oracle statements used to take 22 s.
    - **Connectors own splitting:** they have a `dialect` attribute and a `statements()` class method that splits and cleans up.
    - **Result sets:** one `_result_sets()` hook yields every set, the first one included.
    - **Shared code:**
      - `schemas()` is written once over each database's SQL.
      - `group_indexes` appends `DESC`.
      - `bind_script()` resolves a script's binds in one pass.
      - `ResultWriter` separates result sets.
      - One `prompt_password()` serves connecting and `dprobe keyring`, which now expands `${VAR}` in the user.
    - **MySQL:** one `KILL QUERY` connection is reused per connector.
    - (Sections 4, 6, 7)
22. **After the build order:**
    - Table output right-aligns number columns, measures display width, and fits to the terminal.
    - `README.md` documents installing, configuring, every command and the tests.
    - (Section 7)
23. **Plaintext passwords are allowed without a warning,** which settles the open decision. The `Config.warnings` mechanism, used only for that, is gone. `${VAR}` in `password` has a CLI test that follows it all the way to the connect call. (Section 2, Decisions)
24. **Override files:** `<name>.override.yaml` beside the config is merged over it, as with Docker Compose, so passwords can live outside the shared file. (Section 2)
25. **`test.sh`** runs the unit tests, or with `-i` starts the compose databases and runs everything; `--down` stops them afterwards. (Section 8)
