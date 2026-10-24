"""DataLayer: the single interface consumers use.

Execution of one query:

  1. plan       semantic query -> datasets, joins, pushdowns   (compiler)
  2. scope      load the fund dataset with fund filters pushed down
  3. pushdown   load fact inputs with scope keys + dates pushed down,
                load point-in-time dimensions with a validity window pushed down
  4. resolve    map source identifiers (SEDOL, ISIN) to canonical security_id
  5. federate   run the compiled SQL in DuckDB over the fetched data
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd

from .compiler import Compiler, Plan, Query
from .connectors import Filter, SourceQuery, _q, make_connector
from .model import PhysicalModel, SemanticModel, load_physical, load_semantic, validate


@dataclass
class QueryPlan:
    """Everything needed to see what a query did: explain() output."""

    source_queries: list[SourceQuery] = field(default_factory=list)
    federated_sql: str = ""
    federated_params: list = field(default_factory=list)
    joins: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: float | None = None

    def pretty(self) -> str:
        lines = []
        for i, sq in enumerate(self.source_queries, 1):
            lines.append(
                f"-- [{i}] {sq.dataset} <- {sq.source} ({sq.source_type}): {sq.rows} rows in {sq.elapsed_ms} ms"
            )
            lines.append(sq.sql)
            if sq.params:
                lines.append(f"-- params: {sq.params}")
            lines.append("")
        lines.append("-- federated (DuckDB)")
        lines.append(self.federated_sql)
        if self.federated_params:
            lines.append(f"-- params: {self.federated_params}")
        for w in self.warnings:
            lines.append(f"-- WARNING: {w}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "source_queries": [
                {
                    "dataset": s.dataset,
                    "source": s.source,
                    "source_type": s.source_type,
                    "sql": s.sql,
                    "params": [str(p) for p in s.params],
                    "rows": s.rows,
                    "elapsed_ms": s.elapsed_ms,
                }
                for s in self.source_queries
            ],
            "federated_sql": self.federated_sql,
            "federated_params": [str(p) for p in self.federated_params],
            "joins": self.joins,
            "warnings": self.warnings,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class QueryResult:
    df: pd.DataFrame
    plan: QueryPlan

    def explain(self) -> str:
        return self.plan.pretty()


class DataLayer:
    def __init__(self, semantic: SemanticModel, physical: PhysicalModel):
        validate(semantic, physical)
        self.sem = semantic
        self.phy = physical
        self.compiler = Compiler(semantic, physical)
        self.connectors = {name: make_connector(s) for name, s in physical.sources.items()}

    @classmethod
    def from_dir(cls, model_dir: str | Path) -> DataLayer:
        model_dir = Path(model_dir)
        return cls(load_semantic(model_dir / "semantic.yaml"), load_physical(model_dir / "physical.yaml"))

    @classmethod
    def from_env(cls) -> DataLayer:
        return cls.from_dir(os.environ.get("SEMLAYER_MODEL_DIR", "model"))

    # ------------------------------------------------------------------ catalog

    def catalog(self) -> dict:
        """The consumer-facing model: entities, dimensions, metrics. No physical detail."""
        entities = {}
        for name, ent in self.sem.entities.items():
            entities[name] = {
                "description": ent.get("description", ""),
                "dimensions": {
                    d.name: {"description": d.description, "type": d.type, "identifies": d.identifies}
                    for d in self.sem.dimensions.values()
                    if d.entity == name
                },
            }
        metrics = {
            m.name: {
                "description": m.description,
                "unit": m.unit,
                "kind": m.kind,
                "non_additive_over": list(m.non_additive),
            }
            for m in self.sem.metrics.values()
        }
        return {
            "fact": {"name": self.sem.fact.name, "grain": ["fund", "security", "as_of_date"],
                     "description": self.sem.fact.description},
            "entities": entities,
            "metrics": metrics,
        }

    # ------------------------------------------------------------------ query

    def query(
        self,
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: dict | None = None,
        as_of=None,
        date_range=None,
        order_by: list[str] | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        started = time.perf_counter()
        q = Query.build(metrics, dimensions or [], filters, as_of, date_range, order_by, limit)
        plan = self.compiler.plan(q)
        qp = QueryPlan(joins=[str(j) for j in plan.joins])

        with duckdb.connect() as con:
            loaded: set[str] = set()
            self._execute_sources(con, plan, qp, loaded)
            sql, params = self.compiler.sql(plan)
            qp.federated_sql, qp.federated_params = sql, params
            df = con.execute(sql, params).df()

        qp.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return QueryResult(df, qp)

    # ------------------------------------------------------------------ execution

    def _execute_sources(self, con, plan: Plan, qp: QueryPlan, loaded: set[str]) -> None:
        q, fact = plan.query, self.sem.fact
        scope = fact.scope_dataset

        # Scope first: small, and its keys become pushdown predicates for the facts.
        scope_df = self._load(con, scope, plan.scope_filters, qp, loaded)
        self.compiler.check_fund_additivity(plan, len(scope_df))

        time_filter = (
            Filter(fact.time_column, "=", q.start)
            if q.single_date
            else Filter(fact.time_column, "between", (q.start, q.end))
        )
        for inp in plan.inputs:
            keys = scope_df[inp.scope_column].dropna().unique().tolist()
            self._load(con, inp.dataset, [Filter(inp.scope_column, "in", keys), time_filter], qp, loaded)

        pit = {j.to_dataset: j for j in plan.joins if j.point_in_time}
        for ds in plan.datasets:
            if ds in loaded:
                continue
            filters = []
            if ds in pit:  # only fetch rows valid somewhere inside the query window
                vf, vt = pit[ds].point_in_time["valid_from"], pit[ds].point_in_time["valid_to"]
                filters = [Filter(vf, "<=", q.end), Filter(vt, ">", q.start)]
            self._load(con, ds, filters, qp, loaded)
            if ds in pit:
                self._check_overlaps(con, pit[ds], qp)

    def _load(self, con, name: str, filters: list[Filter], qp: QueryPlan, loaded: set[str]) -> pd.DataFrame:
        """Fetch a dataset from its source and expose it to DuckDB as view ds_<name>."""
        ds = self.phy.datasets[name]

        # Identifier lookups (e.g. the security master) must exist first.
        for spec in ds.identifiers.values():
            if spec["lookup"] not in loaded:
                self._load(con, spec["lookup"], [], qp, loaded)

        df, sq = self.connectors[ds.source].fetch(ds, filters)
        qp.source_queries.append(sq)
        con.register(f"src_{name}", df)

        select = ["s.*"]
        if ds.types:
            casts = ", ".join(f"CAST(s.{_q(c)} AS {t}) AS {_q(c)}" for c, t in ds.types.items())
            select = [f"s.* REPLACE ({casts})"]
        joins = []
        for i, (target, spec) in enumerate(ds.identifiers.items()):
            lk, on = spec["lookup"], spec["match_on"]
            select.append(f"x{i}.{_q(target)} AS {_q(target)}")
            joins.append(f"LEFT JOIN {_q('ds_' + lk)} x{i} ON s.{_q(on)} = x{i}.{_q(on)}")
        con.execute(f"CREATE VIEW {_q('ds_' + name)} AS SELECT {', '.join(select)} FROM {_q('src_' + name)} s {' '.join(joins)}")

        for target, spec in ds.identifiers.items():
            unresolved = con.execute(
                f"SELECT DISTINCT {_q(spec['match_on'])} FROM {_q('ds_' + name)} WHERE {_q(target)} IS NULL"
            ).fetchall()
            if unresolved:
                ids = ", ".join(str(r[0]) for r in unresolved[:5])
                qp.warnings.append(
                    f"{name}: {len(unresolved)} {spec['match_on']} value(s) not in {spec['lookup']} "
                    f"(no {target}): {ids}. Their rows carry NULL attributes."
                )
        loaded.add(name)
        return df

    def _check_overlaps(self, con, rel, qp: QueryPlan) -> None:
        """Point-in-time joins are only many_to_one if validity intervals don't overlap."""
        name, key = rel.to_dataset, _q(rel.to_column)
        vf, vt = _q(rel.point_in_time["valid_from"]), _q(rel.point_in_time["valid_to"])
        n = con.execute(
            f"""SELECT count(*) FROM (
                  SELECT {vf} AS vf, lag({vt}) OVER (PARTITION BY {key} ORDER BY {vf}) AS prev_vt
                  FROM {_q('ds_' + name)} WHERE {key} IS NOT NULL)
                WHERE vf < prev_vt"""
        ).fetchall()[0][0]
        if n:
            qp.warnings.append(f"{name}: {n} overlapping validity interval(s); point-in-time join may fan out.")
