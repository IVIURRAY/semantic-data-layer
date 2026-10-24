"""Source connectors.

A connector turns (dataset, pushed-down filters) into a query in its source's
own dialect, runs it there, and returns a DataFrame with *logical* column
names. Only projection and simple predicates are pushed down; joins and
aggregation happen in the federation engine (DuckDB).

This is the same contract as a Trino connector or DuckDB's postgres_scanner,
kept small enough to read in one sitting.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pandas as pd

from .model import Dataset, Source


@dataclass(frozen=True)
class Filter:
    column: str  # logical column of the dataset
    op: str  # = | in | between | <= | < | >= | >
    value: Any


@dataclass
class SourceQuery:
    """What was sent to a physical source - surfaced by explain()."""

    dataset: str
    source: str
    source_type: str
    sql: str
    params: list = field(default_factory=list)
    rows: int | None = None
    elapsed_ms: float | None = None


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _lit(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


class Connector:
    placeholder = "?"

    def __init__(self, source: Source):
        self.source = source

    # -- dialect hooks ------------------------------------------------------
    def from_clause(self, ds: Dataset) -> str:
        raise NotImplementedError

    def adapt_param(self, value: Any) -> Any:
        return value

    def execute(self, sql: str, params: list) -> pd.DataFrame:
        raise NotImplementedError

    # -- shared -------------------------------------------------------------
    def render(self, ds: Dataset, filters: list[Filter]) -> tuple[str, list]:
        select = ",\n       ".join(
            expr if expr == _q(col) else f"{expr} AS {_q(col)}" for col, expr in ds.columns.items()
        )
        where, params = [], []
        if ds.where:
            where.append(ds.where)
        for f in filters:
            expr = ds.columns[f.column]
            ph = self.placeholder
            if f.op == "in":
                values = list(f.value)
                if not values:
                    where.append("1 = 0")
                    continue
                where.append(f"{expr} IN ({', '.join([ph] * len(values))})")
                params.extend(values)
            elif f.op == "between":
                where.append(f"{expr} BETWEEN {ph} AND {ph}")
                params.extend(f.value)
            else:
                where.append(f"{expr} {f.op} {ph}")
                params.append(f.value)
        sql = f"SELECT {select}\nFROM {self.from_clause(ds)}"
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        return sql, [self.adapt_param(p) for p in params]

    def fetch(self, ds: Dataset, filters: list[Filter]) -> tuple[pd.DataFrame, SourceQuery]:
        sql, params = self.render(ds, filters)
        started = time.perf_counter()
        df = self.execute(sql, params)
        sq = SourceQuery(ds.name, self.source.name, self.source.type, sql, params)
        sq.rows, sq.elapsed_ms = len(df), round((time.perf_counter() - started) * 1000, 1)
        return df, sq


class PostgresConnector(Connector):
    placeholder = "%s"

    def from_clause(self, ds: Dataset) -> str:
        return _q(ds.table)

    def execute(self, sql: str, params: list) -> pd.DataFrame:
        import psycopg
        from psycopg.types.numeric import FloatLoader

        with psycopg.connect(self.source.config["dsn"]) as conn:
            # NUMERIC -> float, so the federation engine sees plain doubles.
            conn.adapters.register_loader("numeric", FloatLoader)
            cur = conn.execute(sql, params)
            cols = [c.name for c in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)


class SQLiteConnector(Connector):
    def from_clause(self, ds: Dataset) -> str:
        return _q(ds.table)

    def adapt_param(self, value: Any) -> Any:
        return value.isoformat() if isinstance(value, dt.date) else value

    def execute(self, sql: str, params: list) -> pd.DataFrame:
        conn = sqlite3.connect(f"file:{self.source.config['path']}?mode=ro", uri=True)
        try:
            cur = conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
        finally:
            conn.close()


class _FileConnector(Connector):
    """Files are scanned with an isolated DuckDB instance standing in for the
    lake's own engine (Spark/Athena/etc.). Filters on hive partition columns
    prune whole files."""

    def execute(self, sql: str, params: list) -> pd.DataFrame:
        with duckdb.connect() as con:
            return con.execute(sql, params).df()


class ParquetConnector(_FileConnector):
    def from_clause(self, ds: Dataset) -> str:
        path = f"{self.source.config['root']}/{ds.table}"
        return f"read_parquet({_lit(path)}, hive_partitioning = true)"


class CSVConnector(_FileConnector):
    def from_clause(self, ds: Dataset) -> str:
        return f"read_csv({_lit(self.source.config['path'])}, header = true, all_varchar = true)"


CONNECTORS = {
    "postgres": PostgresConnector,
    "sqlite": SQLiteConnector,
    "parquet": ParquetConnector,
    "csv": CSVConnector,
}


def make_connector(source: Source) -> Connector:
    try:
        return CONNECTORS[source.type](source)
    except KeyError:
        raise ValueError(f"Unsupported source type {source.type!r}") from None
