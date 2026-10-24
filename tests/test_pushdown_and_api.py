"""The physical layer is hidden from consumers but visible in explain()."""

from fastapi.testclient import TestClient

from semlayer.api import create_app


def test_fund_filter_is_pushed_into_every_fact_source(dl):
    res = dl.query(metrics=["active_weight"], dimensions=["security.sector"], filters={"fund.code": "UK_EQ"}, as_of="2026-09-30")
    by_ds = {s.dataset: s for s in res.plan.source_queries}
    assert by_ds["fund"].params == ["UK_EQ"]
    assert by_ds["holding"].params[0] == "UK_EQ"  # fund key pushed into the OMS
    assert by_ds["benchmark_constituent"].params[0] == "IDX_UK_ALL"  # derived benchmark key pushed into the lake
    assert by_ds["benchmark_constituent"].rows == 19  # one partition, one benchmark (19 UK names)
    assert {s.source_type for s in res.plan.source_queries} == {"postgres", "parquet", "sqlite"}


def test_only_needed_sources_are_touched(dl):
    res = dl.query(metrics=["market_value"], dimensions=["fund.code"], as_of="2026-09-30")
    assert {s.dataset for s in res.plan.source_queries} == {"fund", "security", "holding"}


def test_catalog_exposes_no_physical_detail(dl):
    text = str(dl.catalog())
    for physical in ("weight_percent", "classification_code", "postgres", "parquet", "secmaster", "esg_vendor"):
        assert physical not in text


def test_api_query_and_errors(dl):
    client = TestClient(create_app(dl))
    assert client.get("/health").json() == {"status": "ok"}
    assert "active_weight" in client.get("/catalog").json()["metrics"]

    r = client.post("/query", json={
        "metrics": ["active_weight"], "dimensions": ["fund.code", "security.sector"],
        "filters": {"fund.code": "GLB_EQ"}, "as_of": "2026-09-30", "explain": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == ["fund.code", "security.sector", "active_weight"]
    assert len(body["rows"]) == 11
    assert "federated_sql" in body["plan"]

    r = client.post("/query", json={"metrics": ["portfolio_weight"], "as_of": "2026-09-30"})
    assert r.status_code == 400 and "across funds" in r.json()["detail"]


def test_api_serialises_dates_as_iso_dates(dl):
    client = TestClient(create_app(dl))
    r = client.post("/query", json={
        "metrics": ["market_value"], "dimensions": ["fund.code", "time.as_of_date"],
        "date_range": ["2026-06-30", "2026-07-31"],
    })
    assert {row["time.as_of_date"] for row in r.json()["rows"]} == {"2026-06-30", "2026-07-31"}


def test_api_is_read_only(dl):
    """Only /catalog and /query (plus /health): nothing that writes."""
    paths = {r.path for r in create_app(dl).routes if not r.path.startswith(("/docs", "/redoc", "/openapi"))}
    assert paths == {"/health", "/catalog", "/query"}
