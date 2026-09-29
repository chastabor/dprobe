# dprobe

Run SQL files and inspect tables on Oracle, SQL Server and MySQL/MariaDB from the command line. Connections are named in a YAML file, so a query is just `dprobe query hr-prod report.sql`.

- One `:name` bind syntax for every database, with typed values, lists for `IN (:ids)`, defaults declared in the SQL file, and prompts for anything missing.
- `tables`, `describe`, `indexes` and `schemas` read each database's catalog and print the same columns everywhere.
- Output as an aligned table, csv, tsv, json or jsonl; to the terminal or a file.
- Safe by default: every run rolls back unless you pass `--commit`.
- UTF-8 throughout, whatever the locale.

## Install

Python 3.13 and [uv](https://docs.astral.sh/uv/):

```sh
uv tool install .                # the dprobe command
uv tool install '.[keyring]'     # with OS keyring support
```

From a checkout, `uv run dprobe …` works too. The drivers need no system libraries: oracledb runs in thin mode (no Instant Client), pymssql bundles FreeTDS (no ODBC driver), and mysql-connector-python also talks to MariaDB.

## Configure connections

dprobe reads the first of `--config FILE`, `$DPROBE_CONFIG`, `./dprobe.yaml` and `~/.config/dprobe/config.yaml`. A connection looks like this:

```yaml
defaults:
  format: table        # table, csv, tsv, json, jsonl
  max_rows: 1000       # row limit for table output

connections:
  hr-prod:
    driver: oracle
    url: db1.example.edu:1521/ORCLPDB1   # Easy Connect, a tnsnames.ora alias, or a descriptor
    user: hr_ro
    password: ${HR_PROD_PASS}            # read from the environment when used
    readonly: true
```

[`dprobe.example.yaml`](dprobe.example.yaml) has SQL Server and MySQL entries too, with `password_cmd`, driver `options` and the keyring. For those two, `url` is `host[:port][/database]`; a SQL Server named instance goes in the host, `sqlsrv01\SQLEXPRESS/sis`.

**YAML 1.2 values.** The config and bind files follow the YAML 1.2 core schema, a superset of JSON, rather than PyYAML's YAML 1.1: only `true`/`false` are booleans, so `off`, `yes` and `no` are text (`encryption: off` just works); dates and times stay text; `01234` is 1234, not octal. Merge keys (`<<: *anchor`) still work.

**Override file.** As with Docker Compose, a file with the same name plus `.override` is merged over the config when it exists beside it: `dprobe.yaml` gets `dprobe.override.yaml`, and `tests/it.example.yaml` gets `tests/it.example.override.yaml`. It's the usual place for passwords, and `.gitignore` already skips `*.override.yaml`.

```yaml
# dprobe.override.yaml
connections:
  hr-prod:
    password: s3cret        # merged into hr-prod; the rest of hr-prod stays as it is
  sis:
    password_cmd: null      # null removes a key, so password can replace password_cmd
    password: other-s3cret
```

- **Merging:** mappings merge key by key, so `options` can gain one setting. Any other value is replaced, and `null` removes the key, including a whole connection.
- **Validation** runs on the merged result, and errors name both files. `-v` shows which files were read.

**Passwords** come from the first of these that is set:

| setting | where the password comes from |
|---|---|
| `password` | the file; `${VAR}` reads an environment variable |
| `password_cmd` | the first line a shell command prints, e.g. `pass show db/sis` |
| `keyring: SERVICE` | the OS keyring, under `user`; store it with `dprobe keyring LABEL` |
| none of them | a prompt on the terminal |

A plaintext `password` in the file is fine where that suits the setup. To keep it out of the file, use `${VAR}`: the variable is read when the connection is used, so an unset variable for one connection doesn't affect the others, and a missing one is reported by name. `${VAR}` also works in `url`, `user` and `options`. Passwords of 4 or more characters are masked (`***`) in error messages and tracebacks.

**Other settings**
- `options` is passed to the driver's `connect()`, for things dprobe doesn't set itself (TLS, timeouts, `config_dir` for tnsnames.ora).
- `readonly: true` starts a read-only transaction on Oracle and MySQL, and refuses `--commit` everywhere. It blocks DML but not DDL, which commits implicitly; use a read-only account for a real guarantee.
- MariaDB needs `options: {collation: utf8mb4_general_ci}`; the connect error says so.

`dprobe labels` lists the connections and where each password comes from. `dprobe ping LABEL…` connects and shows the server version.

## Run SQL

```sh
dprobe query hr-prod report.sql
dprobe query hr-prod -e "SELECT * FROM emp WHERE dept = 'IT'"
cat report.sql | dprobe query hr-prod -
```

```
emp_id  name       salary  hired
------  -------  --------  -------------------
   101  Ann Lee  81250.00  2021-03-01 00:00:00
   102  Bo Chen  67000.50  2023-09-18 00:00:00
   103  Cy Diaz      NULL  2024-01-08 00:00:00
(3 rows, 14 ms)
```

The status line goes to stderr, so stdout holds only the data.

**Output**
- `-f csv|tsv|json|jsonl` picks a format; `-o FILE` writes to a file, which isn't created if the query fails.
- Tables stop at `--max-rows` (default 1000; `0` for no limit). Other formats have no limit and stream in batches.
- In a terminal, the widest columns shrink to fit, and cells over `--max-width` (default 80) are cut with `…`. Numbers are right-aligned, and wide characters (CJK, emoji) keep columns aligned.
- JSON keeps every digit of decimals, and binary values print as `0x…` hex.
- `--null TEXT` sets how NULL is shown.

**Transactions:** every run rolls back unless you add `--commit`. DDL on Oracle and MySQL commits anyway; the status line says so.

### Bind variables

Write `:name` in any SQL file; dprobe translates it for each driver.

```sh
dprobe query hr-prod -e "SELECT * FROM emp WHERE emp_id = :id" -b id:int=101
dprobe query hr-prod -e "SELECT * FROM emp WHERE emp_id IN (:ids)" -b ids:int[]=101,102
dprobe query hr-prod report.sql --binds values.yaml
```

- **Types:** values are text unless typed: `str`, `int`, `float`, `decimal`, `date`, `datetime`, `bool`, `null`, or `TYPE[]` for a comma-separated list.
- **A binds file** is a YAML or JSON (`.json`) mapping. A key can carry a type (`"amount:decimal": "12.50"`, `"hired:date": 2024-01-02`). Dates stay text unless typed, and a number with leading zeros like `01234` needs quotes to keep them. `-b` wins over the file.
- **Declarations** in the SQL file give a type and a default:

  ```sql
  -- @bind dept str = IT
  -- @bind hired_after date
  SELECT * FROM emp WHERE dept = :dept AND hired > :hired_after
  ```

- **Prompts:** anything still missing is asked for on the terminal, e.g. `:hired_after (date): `. Without a terminal, dprobe lists every missing name in one error.
- **`--dry-run`** prints the SQL exactly as it would be sent, and the values, without connecting.
- **Placeholders** inside strings and comments are left alone, as are `:=`, `::` and `12:30`. Names are case-insensitive, and DDL is never scanned (so Oracle trigger bodies keep `:NEW` and `:OLD`).
- **Oracle** receives `:name_`, because it rejects reserved words such as `:date` or `:level` as bind names.

### Scripts, native SQL and result columns

- **`--script`** runs every statement in a file in order, in one transaction. It stops at the first error (naming the statement and line) and commits nothing unless `--commit` is given.
  - Oracle and MySQL split after `;`. Oracle PL/SQL blocks run to a `/` line, as in SQL*Plus.
  - SQL Server splits on `GO` lines.
  - Without `--script`, a file holding several statements is an error.
- **Every result set is printed:** a SQL Server batch, or Oracle PL/SQL that returns rows with `DBMS_SQL.RETURN_RESULT`. Tables and csv put a blank line between sets; json writes one array per set.
- **Statement cleanup:** by default dprobe removes a trailing `;` for Oracle (before 23ai it rejects one) and a trailing `/` or `GO` line.
- **`--native`** sends the SQL exactly as written, with no cleanup and no binds, to test a query as another application would send it.
- **`--meta`** shows the columns a query returns instead of its rows.
  - Oracle and SQL Server work this out without running the query.
  - MySQL has to run it, so there `--meta` only takes a query (`SELECT`, `WITH`, `SHOW`, …).

## Inspect tables

```sh
dprobe tables hr-prod --like 'EMP%' --views
dprobe describe hr-prod emp
dprobe indexes hr-prod hr.emp
dprobe schemas hr-prod --all
```

```
position  name    type                nullable  default    pk  extra     comment
--------  ------  ------------------  --------  -------  ----  --------  ---------
       1  EMP_ID  NUMBER(10)          false     NULL        1  identity  NULL
       2  NAME    VARCHAR2(100 CHAR)  false     NULL     NULL  NULL      Full name
       3  SALARY  NUMBER(10,2)        true      NULL     NULL  NULL      NULL
       4  HIRED   DATE                false     SYSDATE  NULL  NULL      NULL
(HR.EMP: 4 columns, 31 ms)
```

- **Output:** every database prints the same columns, written the way its DDL would write them. `pk` is the column's position in the primary key. `describe --raw` shows the catalog's own rows instead.
- **`indexes`** lists each index's columns in key order (with `DESC`, prefix lengths and expressions), whether it's unique or the primary key, and SQL Server's included columns.
- **`schemas`** marks the current one, and hides system schemas unless `--all` is given.
- **Names are found the way each database finds them:**
  - Oracle upper-cases unquoted names (`'"MixedCase"'` keeps its case) and follows private and public synonyms.
  - SQL Server tries your default schema, then `dbo`.
  - MySQL matches exactly, since table names are case-sensitive on Linux.
  - A name that isn't found suggests near matches: `no table or view orders; did you mean sales.orders?`
- **`tables` defaults to** the current schema on Oracle, the URL's database on MySQL, and every schema of the current database on SQL Server.

## Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | the database rejected a statement |
| 2 | bad arguments, input or config |
| 3 | couldn't connect or log in |
| 130 | interrupted (Ctrl-C) |

Add `-v` for tracebacks and the config file in use.

## Development

```sh
./test.sh               # unit tests, no database needed
./test.sh -i            # also the integration tests against Oracle 23ai, SQL Server 2022, MySQL 8.4, MariaDB
./test.sh -i --down     # the same, then stop the databases
./test.sh -k binds -x   # anything else goes to pytest
```

- **Databases:** `-i` starts them with `tests/compose.yaml` and waits until they're healthy. They stay up for the next run, and together need about 5 GB of memory.
- **Integration config:** the tests use `$DPROBE_IT_CONFIG`, by default `tests/it.example.yaml`, with `tests/it.example.override.yaml` merged in if you add one.
- **By hand:** `docker compose -f tests/compose.yaml up -d --wait`, then `DPROBE_IT_CONFIG=tests/it.example.yaml uv run pytest`. The design, the reasons behind it and the driver quirks found along the way are in [`plans/dprobe-plan.md`](plans/dprobe-plan.md).
