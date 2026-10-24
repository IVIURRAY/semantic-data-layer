"""Load and validate the semantic model and physical mapping (YAML -> dataclasses)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ModelError

PLACEHOLDER = re.compile(r"\{([a-z_]+(?:\.[a-z_]+)?)\}")


# --------------------------------------------------------------------- semantic


@dataclass(frozen=True)
class Dimension:
    name: str  # qualified, e.g. "security.sector"
    entity: str
    dataset: str
    column: str | None
    expr: str | None
    identifies: bool
    type: str
    description: str


@dataclass(frozen=True)
class Relationship:
    from_dataset: str
    from_column: str
    to_dataset: str
    to_column: str
    cardinality: str  # many_to_one | one_to_many
    point_in_time: dict | None = None

    def __str__(self) -> str:
        return f"{self.from_dataset}.{self.from_column} -> {self.to_dataset}.{self.to_column} ({self.cardinality})"


@dataclass(frozen=True)
class FactInput:
    dataset: str
    measures: tuple[str, ...]
    scope_column: str
    bridge: dict | None = None


@dataclass(frozen=True)
class Fact:
    name: str
    description: str
    grain: tuple[str, ...]
    time_column: str
    scope_dataset: str
    inputs: tuple[FactInput, ...]

    @property
    def measures(self) -> list[str]:
        return [m for i in self.inputs for m in i.measures]


@dataclass(frozen=True)
class Metric:
    name: str
    description: str
    unit: str
    kind: str  # simple | ratio | derived
    agg: str | None = None
    expr: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    derived: str | None = None
    non_additive: tuple[str, ...] = ()

    def expressions(self) -> list[str]:
        return [e for e in (self.expr, self.numerator, self.denominator) if e]

    def metric_refs(self, all_metrics: dict[str, Metric]) -> list[str]:
        if self.kind != "derived":
            return []
        return [t for t in re.findall(r"[a-z_]+", self.derived) if t in all_metrics]


@dataclass
class SemanticModel:
    fact: Fact
    relationships: list[Relationship]
    entities: dict[str, dict]
    dimensions: dict[str, Dimension]
    metrics: dict[str, Metric]


# --------------------------------------------------------------------- physical


@dataclass(frozen=True)
class Source:
    name: str
    type: str  # postgres | sqlite | parquet | csv
    config: dict
    description: str = ""


@dataclass(frozen=True)
class Dataset:
    name: str
    source: str
    table: str | None
    columns: dict[str, str]  # logical column -> physical expression
    where: str | None = None
    identifiers: dict[str, dict] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)


@dataclass
class PhysicalModel:
    sources: dict[str, Source]
    datasets: dict[str, Dataset]


# --------------------------------------------------------------------- loading


def _split(ref: str) -> tuple[str, str]:
    dataset, _, column = ref.partition(".")
    return dataset, column


def load_semantic(path: Path) -> SemanticModel:
    raw = yaml.safe_load(path.read_text())

    f = raw["fact"]
    fact = Fact(
        name=f["name"],
        description=f.get("description", ""),
        grain=tuple(f["grain"]),
        time_column=f["time_column"],
        scope_dataset=f["scope"]["dataset"],
        inputs=tuple(
            FactInput(i["dataset"], tuple(i["measures"]), i["scope_column"], i.get("bridge"))
            for i in f["inputs"]
        ),
    )

    rels = []
    for r in raw["relationships"]:
        fd, fc = _split(r["from"])
        td, tc = _split(r["to"])
        rels.append(Relationship(fd, fc, td, tc, r["cardinality"], r.get("point_in_time")))

    dims: dict[str, Dimension] = {}
    for ent_name, ent in raw["entities"].items():
        for dim_name, d in ent["dimensions"].items():
            q = f"{ent_name}.{dim_name}"
            dims[q] = Dimension(
                name=q,
                entity=ent_name,
                dataset=d.get("dataset", ent["dataset"]),
                column=d.get("column"),
                expr=d.get("expr"),
                identifies=bool(d.get("identifies", False)),
                type=d.get("type", "string"),
                description=d.get("description", ""),
            )

    metrics: dict[str, Metric] = {}
    for name, m in raw["metrics"].items():
        common = dict(
            name=name,
            description=m.get("description", ""),
            unit=m.get("unit", ""),
            non_additive=tuple(m.get("non_additive", [])),
        )
        if "derived" in m:
            metrics[name] = Metric(kind="derived", derived=m["derived"], **common)
        elif "ratio" in m:
            metrics[name] = Metric(
                kind="ratio", numerator=m["ratio"]["numerator"], denominator=m["ratio"]["denominator"], **common
            )
        else:
            metrics[name] = Metric(kind="simple", agg=m["agg"], expr=m["expr"], **common)

    return SemanticModel(fact, rels, raw["entities"], dims, metrics)


def _env_subst(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    return value


def _columns(spec: list | dict) -> dict[str, str]:
    """`columns` is a list: a bare name is a source column that already has the
    logical name; a `{name: expression}` entry is for anything the source stores
    differently (units, date formats, null conventions)."""
    if isinstance(spec, dict):
        return dict(spec)
    out: dict[str, str] = {}
    for item in spec:
        out.update(item if isinstance(item, dict) else {item: f'"{item}"'})
    return out


def load_physical(path: Path) -> PhysicalModel:
    raw = yaml.safe_load(path.read_text())
    sources = {
        name: Source(
            name,
            s["type"],
            {k: _env_subst(v) for k, v in s.items() if k not in ("type", "description")},
            s.get("description", ""),
        )
        for name, s in raw["sources"].items()
    }
    datasets = {
        name: Dataset(
            name=name,
            source=d["source"],
            table=d.get("table", name),  # defaults to a table named like the dataset
            columns=_columns(d["columns"]),
            where=d.get("where"),
            identifiers=d.get("identifiers", {}),
            types=d.get("types", {}),
        )
        for name, d in raw["datasets"].items()
    }
    return PhysicalModel(sources, datasets)


# --------------------------------------------------------------------- validation


def validate(sem: SemanticModel, phy: PhysicalModel) -> None:
    """Fail fast at startup if the two models don't line up."""
    errors: list[str] = []
    fact = sem.fact.name

    def columns_of(ds: str) -> set[str]:
        if ds == fact:
            return set(sem.fact.grain) | set(sem.fact.measures)
        d = phy.datasets[ds]
        return set(d.columns) | set(d.identifiers)

    for name, d in phy.datasets.items():
        if d.source not in phy.sources:
            errors.append(f"dataset {name}: unknown source {d.source}")
        for target, spec in d.identifiers.items():
            if spec["lookup"] not in phy.datasets:
                errors.append(f"dataset {name}: identifier lookup {spec['lookup']} is not a dataset")

    known = set(phy.datasets) | {fact}
    for inp in sem.fact.inputs:
        if inp.dataset not in phy.datasets:
            errors.append(f"fact input {inp.dataset} has no physical mapping")
            continue
        missing = (set(sem.fact.grain) - {"fund_id"} if inp.bridge else set(sem.fact.grain)) | set(inp.measures)
        missing -= columns_of(inp.dataset)
        if missing:
            errors.append(f"fact input {inp.dataset} is missing columns {sorted(missing)}")

    for r in sem.relationships:
        for ds, col in ((r.from_dataset, r.from_column), (r.to_dataset, r.to_column)):
            if ds not in known:
                errors.append(f"relationship {r}: unknown dataset {ds}")
            elif col not in columns_of(ds):
                errors.append(f"relationship {r}: {ds} has no column {col}")
        if r.cardinality not in ("many_to_one", "one_to_many"):
            errors.append(f"relationship {r}: bad cardinality")

    for d in sem.dimensions.values():
        if d.dataset not in known:
            errors.append(f"dimension {d.name}: unknown dataset {d.dataset}")
            continue
        cols = [d.column] if d.column else PLACEHOLDER.findall(d.expr or "")
        for c in cols:
            if c not in columns_of(d.dataset):
                errors.append(f"dimension {d.name}: {d.dataset} has no column {c}")

    for m in sem.metrics.values():
        for e in m.expressions():
            for ref in PLACEHOLDER.findall(e):
                if "." in ref and ref not in sem.dimensions:
                    errors.append(f"metric {m.name}: unknown dimension {ref}")
                if "." not in ref and ref not in columns_of(fact):
                    errors.append(f"metric {m.name}: fact has no measure/column {ref}")
        if m.kind == "derived" and not m.metric_refs(sem.metrics):
            errors.append(f"metric {m.name}: derived expression references no metrics")

    if errors:
        raise ModelError("Model validation failed:\n  " + "\n  ".join(errors))
