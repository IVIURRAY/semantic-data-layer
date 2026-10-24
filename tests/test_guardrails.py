"""Queries that would silently return wrong numbers are refused."""

import pytest

from semlayer import QueryError


def test_weights_cannot_be_summed_across_funds(dl):
    with pytest.raises(QueryError, match="across funds"):
        dl.query(metrics=["portfolio_weight"], dimensions=["security.sector"], as_of="2026-09-30")


def test_grouping_by_non_identifying_fund_attribute_is_still_across_funds(dl):
    with pytest.raises(QueryError, match="across funds"):
        dl.query(metrics=["active_weight"], dimensions=["fund.strategy"], as_of="2026-09-30")


def test_single_fund_scope_allows_dropping_fund_dimension(dl):
    df = dl.query(metrics=["portfolio_weight"], dimensions=["security.sector"], filters={"fund.code": "UK_EQ"}, as_of="2026-09-30").df
    assert df.portfolio_weight.sum() == pytest.approx(1.0, abs=1e-4)


def test_market_value_is_additive_across_funds(dl):
    df = dl.query(metrics=["market_value"], as_of="2026-09-30").df
    assert df.market_value[0] == pytest.approx(2_200_000_000, rel=1e-6)


def test_stocks_cannot_be_summed_across_dates(dl):
    with pytest.raises(QueryError, match="across dates"):
        dl.query(metrics=["market_value"], dimensions=["fund.code"], date_range=("2026-06-30", "2026-09-30"))


def test_one_to_many_dimension_is_refused(dl):
    # GLB_EQ has two co-PMs; joining PMs would double its weights.
    with pytest.raises(QueryError, match="one-to-many"):
        dl.query(metrics=["portfolio_weight"], dimensions=["fund.code", "fund.portfolio_manager"], as_of="2026-09-30")


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(metrics=["nope"], as_of="2026-09-30"), "Unknown metric"),
        (dict(metrics=["market_value"], dimensions=["fund.colour"], as_of="2026-09-30"), "Unknown dimension"),
        (dict(metrics=["market_value"]), "exactly one of as_of or date_range"),
        (dict(metrics=["market_value"], as_of="30/09/2026"), "Invalid date"),
        (dict(metrics=["market_value"], as_of="2026-09-30", order_by=["fund.code"]), "order_by"),
    ],
)
def test_invalid_queries(dl, kwargs, match):
    with pytest.raises(QueryError, match=match):
        dl.query(**kwargs)


def test_unresolved_identifiers_are_reported_not_dropped(dl):
    res = dl.query(metrics=["portfolio_weight"], dimensions=["security.sector"], filters={"fund.code": "UK_EQ"}, as_of="2026-09-30")
    assert any("BYXQ0N7" in w for w in res.plan.warnings)
    assert res.df.portfolio_weight.sum() == pytest.approx(1.0, abs=1e-4)  # still in the totals
    assert res.df["security.sector"].isna().any()  # but with no attributes


def test_other_classification_schemes_are_excluded(dl):
    res = dl.query(metrics=["portfolio_weight"], dimensions=["security.sector"], filters={"fund.code": "GLB_EQ"}, as_of="2026-09-30")
    assert res.df.portfolio_weight.sum() == pytest.approx(1.0, abs=1e-4)
    assert not res.plan.warnings


def test_short_term_credit_ratings_are_excluded(dl):
    res = dl.query(
        metrics=["portfolio_weight"],
        dimensions=["security.rating"],
        filters={"fund.code": "GLB_EQ"},
        as_of="2026-09-30",
    )
    assert res.df.portfolio_weight.sum() == pytest.approx(1.0, abs=1e-4)
    assert not {"A-1", "A-2"} & set(res.df["security.rating"].dropna())


def test_credit_and_esg_ratings_are_separate(dl):
    """Both use AAA-style letters; they must never be confused."""
    cat = dl.catalog()["entities"]["security"]["dimensions"]
    assert "credit" in cat["security.rating"]["description"].lower()
    assert "esg" in cat["security.esg_rating"]["description"].lower()
    df = dl.query(
        metrics=["portfolio_rating_score", "portfolio_esg_score", "rating_coverage", "esg_coverage"],
        filters={"fund.code": "GLB_EQ"},
        as_of="2026-09-30",
    ).df
    assert 1 <= df.portfolio_rating_score[0] <= 22
    assert 1 <= df.portfolio_esg_score[0] <= 7
