"""Slowly-changing attributes resolve as of each position's date."""

from tests.conftest import rows

RANGE = ("2026-06-30", "2026-09-30")


def test_reclassification_applies_from_effective_date(dl):
    # S0001 moves from IT to Communication Services on 2026-08-15.
    res = dl.query(
        metrics=["portfolio_weight"],
        dimensions=["time.as_of_date", "security.sector"],
        filters={"fund.code": "GLB_EQ", "security.id": "S0001"},
        date_range=RANGE,
    )
    sectors = {str(k.date()) if hasattr(k, "date") else str(k): v["security.sector"] for k, v in rows(res, "time.as_of_date").items()}
    assert sectors == {
        "2026-06-30": "Information Technology",
        "2026-07-31": "Information Technology",
        "2026-08-31": "Communication Services",
        "2026-09-30": "Communication Services",
    }


def test_credit_downgrade_applies_from_effective_date(dl):
    # S0003 is cut BBB- -> BB+ on 2026-09-10: a fallen angel, investment grade to high yield.
    df = dl.query(
        metrics=["benchmark_weight"],
        dimensions=["time.as_of_date", "security.rating", "security.rating_grade"],
        filters={"fund.code": "GLB_EQ", "security.id": "S0003"},
        date_range=RANGE,
    ).df
    assert list(df["security.rating"]) == ["BBB-", "BBB-", "BBB-", "BB+"]
    assert list(df["security.rating_grade"]) == ["Investment grade"] * 3 + ["High yield"]


def test_esg_downgrade_applies_from_effective_date(dl):
    # S0002's ESG rating is downgraded AA -> BB on 2026-08-20.
    df = dl.query(
        metrics=["portfolio_weight"],
        dimensions=["time.as_of_date", "security.esg_rating"],
        filters={"fund.code": "GLB_SUS", "security.id": "S0002"},
        date_range=RANGE,
    ).df
    assert list(df["security.esg_rating"]) == ["AA", "AA", "BB", "BB"]


def test_point_in_time_join_does_not_fan_out(dl):
    """Securities with rating history must still be counted once per date."""
    df = dl.query(
        metrics=["portfolio_weight", "benchmark_weight"],
        dimensions=["fund.code", "time.as_of_date", "security.rating", "security.esg_rating", "security.sector"],
        date_range=RANGE,
    ).df
    totals = df.groupby(["fund.code", "time.as_of_date"])[["portfolio_weight", "benchmark_weight"]].sum()
    assert ((totals - 1.0).abs() < 1e-4).all().all()


def test_validity_window_is_pushed_down(dl):
    res = dl.query(
        metrics=["portfolio_weight"],
        dimensions=["security.rating", "security.esg_rating"],
        filters={"fund.code": "GLB_EQ"},
        as_of="2026-07-31",
    )
    for name in ("credit_rating", "esg_rating"):
        sq = next(s for s in res.plan.source_queries if s.dataset == name)
        assert '"valid_from"' in sq.sql and '"valid_to"' in sq.sql
    assert not res.plan.warnings
