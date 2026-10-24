"""Semantic query -> logical plan -> federated SQL.

The compiler is where the domain rules live:

* join paths are derived from declared relationships, never written by users;
* any path through a one_to_many edge is refused (fan-out protection);
* slowly-changing attributes are joined point-in-time on the position date;
* metrics declare the axes they are non-additive over, and queries that would
  sum across them are refused;
* portfolio and benchmark sides are combined as a full outer join.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import deque
from dataclasses import dataclass, field

from .connectors import Filter, _q
from .errors import ModelError, QueryError
from .model import PLACEHOLDER, Dimension, FactInput, Metric, PhysicalModel, Relationship, SemanticModel


def _as_date(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise QueryError(f"Invalid date {value!r}; expected YYYY-MM-DD") from None


@dataclass
class Query:
    metrics: list[str]
    dimensions: list[str]
    filters: dict[str, list]
    start: dt.date
    end: dt.date
    order_by: list[str] = field(default_factory=list)
    limit: int | None = None

    @classmethod
    def build(cls, metrics, dimensions=(), filters=None, as_of=None, date_range=None, order_by=None, limit=None):
        if not metrics:
            raise QueryError("At least one metric is required")
        if (as_of is None) == (date_range is None):
            raise QueryError("Provide exactly one of as_of or date_range")
        if as_of is not None:
            start = end = _as_date(as_of)
        else:
            start, end = (_as_date(d) for d in date_range)
            if start > end:
                raise QueryError("date_range start is after end")
        norm_filters = {k: (list(v) if isinstance(v, (list, tuple, set)) else [v]) for k, v in (filters or {}).items()}
        return cls(list(metrics), list(dimensions), norm_filters, start, end, list(order_by or []), limit)

    @property
    def single_date(self) -> bool:
        return self.start == self.end


@dataclass
class Plan:
    query: Query
    metrics: dict[str, Metric]  # requested + dependencies, dependency order
    inputs: list[FactInput]
    measures: list[str]
    joins: list[Relationship]  # ordered parent-before-child
    scope_filters: list[Filter]
    datasets: list[str]  # non-fact datasets the SQL references


class Compiler:
    def __init__(self, sem: SemanticModel, phy: PhysicalModel):
        self.sem = sem
        self.phy = phy
        self.fact = sem.fact.name
        self._edges: dict[str, list[Relationship]] = {}
        for r in sem.relationships:
            self._edges.setdefault(r.from_dataset, []).append(r)

    # ------------------------------------------------------------------ plan

    def plan(self, q: Query) -> Plan:
        dims = self.sem.dimensions
        for name in q.dimensions + list(q.filters):
            if name not in dims:
                raise QueryError(f"Unknown dimension {name!r}. See catalog() for available dimensions.")
        for name in q.metrics:
            if name not in self.sem.metrics:
                raise QueryError(f"Unknown metric {name!r}. See catalog() for available metrics.")
        outputs = set(q.dimensions) | set(q.metrics)
        for o in q.order_by:
            if o.lstrip("-") not in outputs:
                raise QueryError(f"order_by {o!r} is not one of the selected dimensions/metrics")

        metrics = self._metric_closure(q.metrics)

        # Time additivity: weights/MVs are stocks; summing them over dates is meaningless.
        if not q.single_date and "time.as_of_date" not in q.dimensions:
            bad = [m for m in q.metrics if "time" in self.sem.metrics[m].non_additive]
            if bad:
                raise QueryError(
                    f"{', '.join(bad)} cannot be aggregated across dates. "
                    "Add 'time.as_of_date' to dimensions or query a single as_of date."
                )

        # Which fact inputs and dimension datasets does this query touch?
        measures: list[str] = []
        dim_refs: list[str] = list(q.dimensions) + list(q.filters)
        for m in metrics.values():
            for e in m.expressions():
                for ref in PLACEHOLDER.findall(e):
                    if "." in ref:
                        dim_refs.append(ref)
                    elif ref in self.sem.fact.measures and ref not in measures:
                        measures.append(ref)
        inputs = [i for i in self.sem.fact.inputs if set(i.measures) & set(measures)]
        measures = [m for i in inputs for m in i.measures]  # an input brings all its measures

        joins: list[Relationship] = []
        for ref in dict.fromkeys(dim_refs):
            for rel in self._path_to(dims[ref].dataset, ref):
                if rel not in joins:
                    joins.append(rel)

        scope = self.sem.fact.scope_dataset
        scope_filters = []
        for name, values in q.filters.items():
            d = dims[name]
            if d.dataset == scope and d.column:
                scope_filters.append(Filter(d.column, "in", values))

        datasets = list(dict.fromkeys([scope] + [i.dataset for i in inputs] + [j.to_dataset for j in joins]))
        return Plan(q, metrics, inputs, measures, joins, scope_filters, datasets)

    def check_fund_additivity(self, plan: Plan, funds_in_scope: int) -> None:
        """Weights are relative to each fund's NAV, so summing them across funds is
        meaningless. Grouping by fund.strategy doesn't help: it still sums funds.
        Only a dimension that identifies a fund (or a single-fund scope) makes it valid."""
        fund_identified = any(
            self.sem.dimensions[d].entity == "fund" and self.sem.dimensions[d].identifies
            for d in plan.query.dimensions
        )
        bad = [m for m in plan.query.metrics if "fund" in self.sem.metrics[m].non_additive]
        if funds_in_scope > 1 and bad and not fund_identified:
            raise QueryError(
                f"{', '.join(bad)} cannot be aggregated across funds ({funds_in_scope} funds in scope). "
                "Add 'fund.code' to dimensions or filter to a single fund."
            )

    def _metric_closure(self, names: list[str]) -> dict[str, Metric]:
        out: dict[str, Metric] = {}

        def visit(n: str, stack: tuple = ()):
            if n in stack:
                raise ModelError(f"Circular metric definition: {' -> '.join(stack + (n,))}")
            m = self.sem.metrics[n]
            for dep in m.metric_refs(self.sem.metrics):
                visit(dep, stack + (n,))
            out.setdefault(n, m)

        for n in names:
            visit(n)
        return out

    def _path_to(self, target: str, for_dim: str) -> list[Relationship]:
        if target == self.fact:
            return []
        seen, queue = {self.fact}, deque([(self.fact, [])])
        while queue:
            node, path = queue.popleft()
            for rel in self._edges.get(node, []):
                if rel.to_dataset in seen:
                    continue
                new_path = path + [rel]
                if rel.to_dataset == target:
                    fanout = [r for r in new_path if r.cardinality != "many_to_one"]
                    if fanout:
                        raise QueryError(
                            f"Dimension {for_dim!r} is reached via a one-to-many relationship ({fanout[0]}). "
                            "Joining it would duplicate positions and double count every additive metric."
                        )
                    return new_path
                seen.add(rel.to_dataset)
                queue.append((rel.to_dataset, new_path))
        raise ModelError(f"No join path from {self.fact} to {target} (needed for {for_dim})")

    # ------------------------------------------------------------------ SQL

    def dim_sql(self, d: Dimension) -> str:
        if d.column:
            return f"{_q(d.dataset)}.{_q(d.column)}"
        return PLACEHOLDER.sub(lambda m: f"{_q(d.dataset)}.{_q(m.group(1))}", d.expr)

    def _expr_sql(self, expr: str) -> str:
        def sub(m: re.Match) -> str:
            ref = m.group(1)
            if "." in ref:
                return f"({self.dim_sql(self.sem.dimensions[ref])})"
            return f"{_q(self.fact)}.{_q(ref)}"

        return PLACEHOLDER.sub(sub, expr)

    def sql(self, plan: Plan) -> tuple[str, list]:
        q, fact = plan.query, self.sem.fact
        grain = list(fact.grain)

        # 1. Conformed fact: UNION ALL of inputs, then collapse to grain (== full outer join).
        branches = []
        for inp in plan.inputs:
            ds = self.phy.datasets[inp.dataset]
            available = set(ds.columns) | set(ds.identifiers)
            cols = []
            for g in grain:
                if inp.bridge and g not in available:  # e.g. fund_id comes from the fund bridge
                    cols.append(f"{_q(inp.bridge['dataset'])}.{_q(g)}")
                else:
                    cols.append(f"i.{_q(g)}")
            for m in plan.measures:
                cols.append(f"i.{_q(m)}" if m in inp.measures else f"0.0 AS {_q(m)}")
            sql = f"    SELECT {', '.join(cols)}\n    FROM {_q('ds_' + inp.dataset)} AS i"
            if inp.bridge:
                b, on = inp.bridge["dataset"], inp.bridge["key"]
                sql += f"\n    JOIN {_q('ds_' + b)} AS {_q(b)} ON i.{_q(on)} = {_q(b)}.{_q(on)}"
            branches.append(sql)
        g_cols = ", ".join(_q(g) for g in grain)
        m_cols = ", ".join(f"SUM({_q(m)}) AS {_q(m)}" for m in plan.measures)
        position_cte = (
            f"{_q(self.fact)} AS (\n  SELECT {g_cols}, {m_cols}\n  FROM (\n"
            + "\n    UNION ALL\n".join(branches)
            + f"\n  ) AS u\n  GROUP BY {g_cols}\n)"
        )

        # 2. Join dimensions, filter, aggregate metrics.
        select = [f"{self.dim_sql(self.sem.dimensions[d])} AS {_q(d)}" for d in q.dimensions]
        for m in plan.metrics.values():
            if m.kind == "simple":
                e = self._expr_sql(m.expr)
                agg = f"COUNT(DISTINCT {e})" if m.agg == "count_distinct" else f"{m.agg.upper()}({e})"
                select.append(f"{agg} AS {_q(m.name)}")
            elif m.kind == "ratio":
                select.append(f"SUM({self._expr_sql(m.numerator)}) AS {_q(m.name + '__num')}")
                select.append(f"SUM({self._expr_sql(m.denominator)}) AS {_q(m.name + '__den')}")

        joins = []
        for r in plan.joins:
            on = f"{_q(r.from_dataset)}.{_q(r.from_column)} = {_q(r.to_dataset)}.{_q(r.to_column)}"
            if r.point_in_time:
                pit, at = r.point_in_time, r.point_in_time["at"].split(".")
                at_sql = f"{_q(at[0])}.{_q(at[1])}"
                on += (
                    f"\n   AND {_q(r.to_dataset)}.{_q(pit['valid_from'])} <= {at_sql}"
                    f"\n   AND {at_sql} < {_q(r.to_dataset)}.{_q(pit['valid_to'])}"
                )
            joins.append(f"  LEFT JOIN {_q('ds_' + r.to_dataset)} AS {_q(r.to_dataset)}\n    ON {on}")

        where, params = [], []
        for name, values in q.filters.items():
            where.append(f"{self.dim_sql(self.sem.dimensions[name])} IN ({', '.join(['?'] * len(values))})")
            params.extend(values)

        agg_cte = "agg AS (\n  SELECT " + ",\n         ".join(select) + f"\n  FROM {_q(self.fact)}"
        if joins:
            agg_cte += "\n" + "\n".join(joins)
        if where:
            agg_cte += "\n  WHERE " + "\n    AND ".join(where)
        agg_cte += "\n  GROUP BY ALL\n)"

        # 3. Final projection: ratios and derived metrics are computed after aggregation.
        final = [_q(d) for d in q.dimensions] + [f"{self._outer(m)} AS {_q(m)}" for m in q.metrics]
        order = q.order_by or q.dimensions
        sql = f"WITH {position_cte},\n{agg_cte}\nSELECT " + ",\n       ".join(final) + "\nFROM agg"
        if order:
            sql += "\nORDER BY " + ", ".join(
                f"{_q(o[1:])} DESC" if o.startswith("-") else _q(o) for o in order
            )
        if q.limit:
            sql += f"\nLIMIT {int(q.limit)}"
        return sql, params

    def _outer(self, name: str) -> str:
        m = self.sem.metrics[name]
        if m.kind == "simple":
            return _q(name)
        if m.kind == "ratio":
            return f"({_q(name + '__num')} / NULLIF({_q(name + '__den')}, 0))"
        return "(" + re.sub(
            r"[a-z_]+", lambda t: self._outer(t.group(0)) if t.group(0) in self.sem.metrics else t.group(0), m.derived
        ) + ")"
