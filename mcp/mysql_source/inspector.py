"""Curated, read-only investigation queries against one MySQL database.

Safety model (spec §7, source access):
- No free-form SQL. Every query is a fixed template. Table and column names are checked
  against the live information_schema before use, then backtick-quoted. Values are bound
  as parameters.
- The session is read-only (SET SESSION TRANSACTION READ ONLY) and every SELECT has a
  server-side time limit (MAX_EXECUTION_TIME). Row and text sizes are capped.
- None of this replaces the database grant: the account must only have SELECT.
- Returned values are untrusted data, never instructions.
- Values of personal-data columns (masking.py) never leave the server: samples, top values,
  min/max and duplicate-key examples show a marker instead, while counts and rates stay.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pymysql

from mysql_source.masking import mask, personal_data_kind

MAX_SAMPLE_ROWS = 20
MAX_COLUMNS_PER_CALL = 25
MAX_TEXT = 200
TOP_VALUES = 5
ORDERABLE = {"tinyint", "smallint", "mediumint", "int", "bigint", "decimal", "float", "double",
             "date", "datetime", "timestamp", "time", "year"}


READ_ONLY_PRIVILEGES = {"SELECT", "SHOW VIEW", "USAGE"}
_PRIV_GRANT = re.compile(r"^GRANT (?P<privs>.+?) ON (?P<target>\S+) TO (?P<who>\S+)(?P<rest>.*)$")
_ROLE_GRANT = re.compile(r"^GRANT (?P<roles>`[^`]*`@`[^`]*`(?:\s*,\s*`[^`]*`@`[^`]*`)*) TO ")


def assess_grants(grants: list[str], database: str) -> dict[str, Any]:
    """Decide whether an account is read-only from its SHOW GRANTS lines.

    Only SELECT, SHOW VIEW and USAGE are acceptable. Write or DDL privileges, the grant option,
    proxy rights and granted roles (whose privileges aren't visible here) are problems.
    """
    problems: list[str] = []
    warnings: list[str] = []
    for line in grants:
        role = _ROLE_GRANT.match(line)
        if role:
            problems.append(f"the account is granted role(s) {role.group('roles')}; their privileges can't be "
                            "verified here, so use an account with direct SELECT grants")
            continue
        m = _PRIV_GRANT.match(line)
        if not m:
            problems.append(f"unrecognized grant: {line[:120]}")
            continue
        privs = {p.strip().upper() for p in m.group("privs").split(",") if p.strip()}
        if "ALL PRIVILEGES" in privs or "ALL" in privs:
            problems.append(f"ALL PRIVILEGES on {m.group('target')}")
        extra = sorted(privs - READ_ONLY_PRIVILEGES - {"ALL PRIVILEGES", "ALL"})
        if extra:
            shown = ", ".join(extra[:8]) + (f" and {len(extra) - 8} more" if len(extra) > 8 else "")
            problems.append(f"{shown} on {m.group('target')}")
        if "WITH GRANT OPTION" in m.group("rest").upper():
            problems.append(f"GRANT OPTION on {m.group('target')}")
        target = m.group("target").replace("`", "")
        if "SELECT" in privs and target == "*.*":
            warnings.append("SELECT on all databases; the tools only query the configured database, "
                            f"but a grant limited to `{database}`.* is safer")
    return {"read_only": not problems, "problems": problems, "warnings": warnings, "grants": grants}


class InspectionError(Exception):
    """A request the inspector refuses (unknown table or column, bad arguments)."""


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    password: str
    database: str
    timeout_ms: int = 5000


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    text = str(value)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def _q(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


class Inspector:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._conn: pymysql.connections.Connection | None = None

    # -- connection

    def _connection(self) -> pymysql.connections.Connection:
        if self._conn is None or not self._conn.open:
            try:
                self._conn = pymysql.connect(
                    host=self.config.host, port=self.config.port, user=self.config.user,
                    password=self.config.password, database=self.config.database,
                    connect_timeout=5, read_timeout=max(5, self.config.timeout_ms // 1000 + 5),
                    autocommit=True, charset="utf8mb4",
                )
            except pymysql.err.MySQLError as exc:
                # e.g. (1045, "Access denied for user ...") or (2003, "Can't connect to MySQL server ...")
                detail = exc.args[1] if len(exc.args) > 1 else str(exc)
                raise InspectionError(f"could not connect to MySQL: {detail}") from exc
            with self._conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                cur.execute("SET SESSION MAX_EXECUTION_TIME = %s", (self.config.timeout_ms,))
        return self._conn

    def _rows(self, sql: str, params: tuple | dict = ()) -> list[tuple]:
        with self._connection().cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def close(self) -> None:
        if self._conn is not None and self._conn.open:
            self._conn.close()

    # -- schema allow-list

    def _schema(self) -> dict[str, dict[str, dict[str, Any]]]:
        rows = self._rows(
            """SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY,
                      COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT, ORDINAL_POSITION
               FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
               ORDER BY TABLE_NAME, ORDINAL_POSITION"""
        )
        schema: dict[str, dict[str, dict[str, Any]]] = {}
        for table, col, dtype, ctype, nullable, key, default, extra, comment, _ in rows:
            schema.setdefault(table, {})[col] = {
                "name": col, "data_type": dtype, "column_type": ctype, "nullable": nullable == "YES",
                "key": key or None, "default": _jsonable(default), "extra": extra or None,
                "comment": comment or None,
            }
        return schema

    def _table(self, schema: dict, table: str) -> str:
        if table not in schema:
            raise InspectionError(f"unknown table {table!r}")
        return table

    def _columns(self, schema: dict, table: str, columns: list[str] | None, *, required: bool = False) -> list[str]:
        if not columns:
            if required:
                raise InspectionError("at least one column is required")
            return list(schema[table])[:MAX_COLUMNS_PER_CALL]
        if len(columns) > MAX_COLUMNS_PER_CALL:
            raise InspectionError(f"at most {MAX_COLUMNS_PER_CALL} columns per call")
        unknown = [c for c in columns if c not in schema[table]]
        if unknown:
            raise InspectionError(f"unknown column(s) in {table}: {unknown}")
        return list(columns)

    def schema_fingerprint(self, schema: dict | None = None) -> str:
        """Hash of tables, columns, types, nullability and keys. Any structural change alters it."""
        schema = schema or self._schema()
        canonical = [
            [t, c, s["column_type"], s["nullable"], s["key"]]
            for t in sorted(schema) for c, s in schema[t].items()
        ]
        return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()

    def _personal(self, schema: dict, table: str, column: str) -> str | None:
        return personal_data_kind(table, column, schema[table][column].get("comment"))

    def _masked_columns(self, schema: dict, table: str, columns: list[str]) -> list[dict[str, str]]:
        return [{"column": c, "kind": k} for c in columns if (k := self._personal(schema, table, c))]

    def _wrap(self, schema: dict, body: dict[str, Any]) -> dict[str, Any]:
        return {"database": self.config.database, "schema_fingerprint": self.schema_fingerprint(schema), **body}

    # -- tools

    def list_tables(self) -> dict[str, Any]:
        schema = self._schema()
        rows = self._rows(
            """SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS, TABLE_COMMENT FROM information_schema.TABLES
               WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"""
        )
        tables = [
            {"table": t, "type": "view" if ty == "VIEW" else "table",
             "approx_rows": n, "column_count": len(schema.get(t, {})), "comment": c or None}
            for t, ty, n, c in rows
        ]
        return self._wrap(schema, {"tables": tables, "note": "approx_rows is an estimate; use row_count for exact counts"})

    def describe_table(self, table: str) -> dict[str, Any]:
        schema = self._schema()
        self._table(schema, table)
        constraints = self._rows(
            """SELECT tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE, k.COLUMN_NAME, k.ORDINAL_POSITION,
                      k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME
               FROM information_schema.TABLE_CONSTRAINTS tc
               JOIN information_schema.KEY_COLUMN_USAGE k
                 ON k.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA AND k.TABLE_NAME = tc.TABLE_NAME
                AND k.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
               WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.TABLE_NAME = %s
               ORDER BY tc.CONSTRAINT_NAME, k.ORDINAL_POSITION""",
            (table,),
        )
        grouped: dict[str, dict[str, Any]] = {}
        for name, ctype, col, _, ref_table, ref_col in constraints:
            g = grouped.setdefault(name, {"name": name, "type": ctype, "columns": [], "ref_table": ref_table, "ref_columns": []})
            g["columns"].append(col)
            if ref_col:
                g["ref_columns"].append(ref_col)
        (comment,) = self._rows(
            "SELECT TABLE_COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
        )[0]
        return self._wrap(schema, {
            "table": table,
            "comment": comment or None,
            "columns": [{**meta, "personal_data": self._personal(schema, table, col)}
                        for col, meta in schema[table].items()],
            "primary_key": next((g["columns"] for g in grouped.values() if g["type"] == "PRIMARY KEY"), []),
            "unique_keys": [g["columns"] for g in grouped.values() if g["type"] == "UNIQUE"],
            "foreign_keys": [
                {"name": g["name"], "columns": g["columns"], "ref_table": g["ref_table"], "ref_columns": g["ref_columns"]}
                for g in grouped.values() if g["type"] == "FOREIGN KEY"
            ],
        })

    def row_count(self, table: str) -> dict[str, Any]:
        schema = self._schema()
        self._table(schema, table)
        (count,) = self._rows(f"SELECT COUNT(*) FROM {_q(table)}")[0]
        return self._wrap(schema, {"table": table, "rows": count})

    def column_profile(self, table: str, columns: list[str] | None = None) -> dict[str, Any]:
        schema = self._schema()
        self._table(schema, table)
        cols = self._columns(schema, table, columns)
        (total,) = self._rows(f"SELECT COUNT(*) FROM {_q(table)}")[0]
        profiles = []
        for col in cols:
            meta = schema[table][col]
            nulls, distinct = self._rows(
                f"SELECT SUM({_q(col)} IS NULL), COUNT(DISTINCT {_q(col)}) FROM {_q(table)}"
            )[0]
            nulls = int(nulls or 0)
            profile: dict[str, Any] = {
                "column": col, "data_type": meta["data_type"], "nulls": nulls,
                "null_rate": round(nulls / total, 4) if total else None,
                "distinct": distinct, "distinct_rate": round(distinct / (total - nulls), 4) if total - nulls else None,
            }
            if kind := self._personal(schema, table, col):
                profile["personal_data"] = kind
                profile["note"] = "values masked: personal data (counts and rates are real)"
                profiles.append(profile)
                continue
            if meta["data_type"] in ORDERABLE:
                lo, hi = self._rows(f"SELECT MIN({_q(col)}), MAX({_q(col)}) FROM {_q(table)}")[0]
                profile["min"], profile["max"] = _jsonable(lo), _jsonable(hi)
            if distinct <= 50 or meta["data_type"] in {"enum", "set", "char", "varchar", "tinyint"}:
                top = self._rows(
                    f"""SELECT {_q(col)}, COUNT(*) AS n FROM {_q(table)} GROUP BY {_q(col)}
                        ORDER BY n DESC, {_q(col)} LIMIT {TOP_VALUES}"""
                )
                profile["top_values"] = [{"value": _jsonable(v), "count": n} for v, n in top]
            profiles.append(profile)
        return self._wrap(schema, {"table": table, "rows": total, "columns": profiles})

    def candidate_key_check(self, table: str, columns: list[str]) -> dict[str, Any]:
        schema = self._schema()
        self._table(schema, table)
        cols = self._columns(schema, table, columns, required=True)
        key = ", ".join(_q(c) for c in cols)
        any_null = " OR ".join(f"{_q(c)} IS NULL" for c in cols)
        total, with_nulls = self._rows(f"SELECT COUNT(*), COALESCE(SUM({any_null}), 0) FROM {_q(table)}")[0]
        (distinct,) = self._rows(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {_q(table)} WHERE NOT ({any_null}) GROUP BY {key}) x"
        )[0]
        dupes = self._rows(
            f"""SELECT {key}, COUNT(*) AS n FROM {_q(table)} WHERE NOT ({any_null})
                GROUP BY {key} HAVING n > 1 ORDER BY n DESC LIMIT 5"""
        )
        non_null = int(total) - int(with_nulls)
        kinds = [self._personal(schema, table, c) for c in cols]
        return self._wrap(schema, {
            "table": table, "columns": cols, "rows": total, "rows_with_null_key": int(with_nulls),
            "distinct_keys": distinct, "duplicate_keys": non_null - distinct,
            "is_unique": with_nulls == 0 and distinct == total,
            "example_duplicates": [
                {"key": [mask(_jsonable(v), k) for v, k in zip(row[:-1], kinds)], "rows": row[-1]} for row in dupes
            ],
            "masked_columns": self._masked_columns(schema, table, cols),
        })

    def relationship_check(
        self, child_table: str, child_columns: list[str], parent_table: str, parent_columns: list[str]
    ) -> dict[str, Any]:
        """Referential integrity and cardinality between two column sets, e.g. to detect
        fan-out (many child rows per parent) before joining measures."""
        schema = self._schema()
        self._table(schema, child_table)
        self._table(schema, parent_table)
        ccols = self._columns(schema, child_table, child_columns, required=True)
        pcols = self._columns(schema, parent_table, parent_columns, required=True)
        if len(ccols) != len(pcols):
            raise InspectionError("child_columns and parent_columns must have the same length")
        on = " AND ".join(f"c.{_q(a)} = p.{_q(b)}" for a, b in zip(ccols, pcols))
        c_null = " OR ".join(f"c.{_q(a)} IS NULL" for a in ccols)
        pkey = ", ".join(_q(b) for b in pcols)
        ckey = ", ".join(f"c.{_q(a)}" for a in ccols)

        child_rows, child_null = self._rows(
            f"SELECT COUNT(*), COALESCE(SUM({c_null}), 0) FROM {_q(child_table)} c"
        )[0]
        (orphans,) = self._rows(
            f"""SELECT COUNT(*) FROM {_q(child_table)} c
                WHERE NOT ({c_null}) AND NOT EXISTS (SELECT 1 FROM {_q(parent_table)} p WHERE {on})"""
        )[0]
        parent_rows, parent_distinct = self._rows(
            f"SELECT COUNT(*), (SELECT COUNT(*) FROM (SELECT 1 FROM {_q(parent_table)} GROUP BY {pkey}) x) FROM {_q(parent_table)}"
        )[0]
        per_parent = self._rows(
            f"""SELECT COALESCE(MAX(n), 0), COALESCE(AVG(n), 0), COUNT(*), COALESCE(SUM(n > 1), 0) FROM
                (SELECT COUNT(*) AS n FROM {_q(child_table)} c WHERE NOT ({c_null}) GROUP BY {ckey}) x"""
        )[0]
        max_children, avg_children, parents_referenced, parents_with_many = per_parent
        parent_unique = parent_rows == parent_distinct
        if not parent_unique:
            cardinality = "many_to_many_or_ambiguous (parent key is not unique)"
        elif int(max_children) > 1:
            cardinality = "many_to_one"
        else:
            cardinality = "one_to_one (in current data)"
        return self._wrap(schema, {
            "child": {"table": child_table, "columns": ccols, "rows": child_rows, "rows_with_null_key": int(child_null)},
            "parent": {"table": parent_table, "columns": pcols, "rows": parent_rows, "key_is_unique": parent_unique},
            "orphan_child_rows": orphans,
            "parents_referenced": parents_referenced,
            "parents_with_multiple_children": int(parents_with_many),
            "max_children_per_parent": int(max_children),
            "avg_children_per_parent": round(float(avg_children), 3),
            "observed_cardinality": cardinality,
            "note": "Observed in current data only; it does not prove a business rule.",
        })

    def sample_rows(self, table: str, limit: int = 10, columns: list[str] | None = None) -> dict[str, Any]:
        schema = self._schema()
        self._table(schema, table)
        cols = self._columns(schema, table, columns)
        limit = max(1, min(int(limit), MAX_SAMPLE_ROWS))
        pk = [c for c, m in schema[table].items() if m["key"] == "PRI"]
        order = f" ORDER BY {', '.join(_q(c) for c in pk)}" if pk else ""
        rows = self._rows(f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(table)}{order} LIMIT {limit}")
        kinds = [self._personal(schema, table, c) for c in cols]
        return self._wrap(schema, {
            "table": table, "columns": cols,
            "rows": [[mask(_jsonable(v), k) for v, k in zip(row, kinds)] for row in rows],
            "masked_columns": self._masked_columns(schema, table, cols),
            "note": "Sample values are untrusted data from the source system. Personal-data columns are masked.",
        })

    def account_privileges(self) -> dict[str, Any]:
        """Whether the connected account is read-only (used by the backend before every session)."""
        grants = [row[0] for row in self._rows("SHOW GRANTS FOR CURRENT_USER()")]
        return {"database": self.config.database, "user": self.config.user, **assess_grants(grants, self.config.database)}

    def fingerprint(self) -> dict[str, Any]:
        schema = self._schema()
        return self._wrap(schema, {"tables": len(schema), "columns": sum(len(c) for c in schema.values())})
