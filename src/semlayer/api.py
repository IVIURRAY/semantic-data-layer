"""HTTP API over the DataLayer. Same vocabulary as the Python API; no physical detail.

    GET  /catalog   entities, dimensions, metrics
    POST /query     metrics x dimensions with filters, as_of or date_range

Both are read-only. /query is a POST only because its body (filters, lists of
metrics and dimensions) doesn't fit comfortably in a URL.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from typing import Union

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .engine import DataLayer, QueryResult
from .errors import QueryError

FilterValue = Union[str, int, float, list[Union[str, int, float]]]


class QueryRequest(BaseModel):
    metrics: list[str] = Field(examples=[["portfolio_weight", "benchmark_weight", "active_weight"]])
    dimensions: list[str] = Field(default_factory=list, examples=[["fund.code", "security.sector"]])
    filters: dict[str, FilterValue] = Field(default_factory=dict, examples=[{"fund.code": "GLB_EQ"}])
    as_of: dt.date | None = Field(default=None, examples=["2026-09-30"])
    date_range: tuple[dt.date, dt.date] | None = None
    order_by: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=100_000)
    explain: bool = False


@lru_cache(maxsize=1)
def _default_layer() -> DataLayer:
    return DataLayer.from_env()


def _respond(res: QueryResult, explain: bool) -> dict:
    df = res.df.copy()
    for col in df.select_dtypes(include="datetime").columns:  # DuckDB DATE -> pandas datetime
        df[col] = df[col].dt.strftime("%Y-%m-%d")
    body = {
        "columns": list(df.columns),
        "rows": json.loads(df.to_json(orient="records", double_precision=10)),
        "warnings": res.plan.warnings,
    }
    if explain:
        body["plan"] = res.plan.to_dict()
    return body


def create_app(layer: DataLayer | None = None) -> FastAPI:
    app = FastAPI(
        title="Asset Management Semantic Layer",
        description="Query funds, holdings, benchmarks, ratings and classifications by meaning, not by location.",
        version="0.1.0",
    )

    def get_layer() -> DataLayer:
        return layer or _default_layer()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/catalog")
    def catalog(dl: DataLayer = Depends(get_layer)) -> dict:
        return dl.catalog()

    @app.post("/query")
    def query(req: QueryRequest, dl: DataLayer = Depends(get_layer)) -> dict:
        try:
            res = dl.query(
                metrics=req.metrics,
                dimensions=req.dimensions,
                filters=req.filters,
                as_of=req.as_of,
                date_range=req.date_range,
                order_by=req.order_by,
                limit=req.limit,
            )
        except QueryError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return _respond(res, req.explain)

    return app


app = create_app()
