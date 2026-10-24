"""Active weight = portfolio - benchmark, over a FULL OUTER join of the two sides."""

import pytest

from tests.conftest import rows

AS_OF = "2026-09-30"


def test_weights_sum_to_one_and_active_nets_to_zero(dl):
    df = dl.query(
        metrics=["portfolio_weight", "benchmark_weight", "active_weight"],
        dimensions=["fund.code"],
        as_of=AS_OF,
    ).df
    for _, r in df.iterrows():
        assert r.portfolio_weight == pytest.approx(1.0, abs=1e-4)
        assert r.benchmark_weight == pytest.approx(1.0, abs=1e-4)
        assert r.active_weight == pytest.approx(0.0, abs=1e-4)


def test_benchmark_only_names_are_underweights(dl):
    """The classic bug: inner-joining holdings to the benchmark drops names you
    don't own, so their negative active weight disappears."""
    df = dl.query(
        metrics=["portfolio_weight", "benchmark_weight", "active_weight"],
        dimensions=["security.id"],
        filters={"fund.code": "GLB_EQ"},
        as_of=AS_OF,
    ).df
    unheld = df[df.portfolio_weight == 0]
    assert len(df) == 60  # whole benchmark universe present, not just the 28 holdings
    assert len(unheld) == 32
    assert (unheld.active_weight < 0).all()
    assert unheld.active_weight.sum() == pytest.approx(-unheld.benchmark_weight.sum())


def test_off_benchmark_holding_is_pure_overweight(dl):
    df = dl.query(
        metrics=["portfolio_weight", "benchmark_weight", "active_weight"],
        dimensions=["security.country"],
        filters={"fund.code": "UK_EQ"},
        as_of=AS_OF,
    )
    by_country = rows(df, "security.country")
    assert by_country["US"]["benchmark_weight"] == 0
    assert by_country["US"]["active_weight"] == pytest.approx(by_country["US"]["portfolio_weight"])


def test_active_share_is_half_sum_abs_security_active_weights(dl):
    sec = dl.query(metrics=["active_weight"], dimensions=["security.id"], filters={"fund.code": "GLB_EQ"}, as_of=AS_OF).df
    share = dl.query(metrics=["active_share"], filters={"fund.code": "GLB_EQ"}, as_of=AS_OF).df
    assert share.active_share[0] == pytest.approx(0.5 * sec.active_weight.abs().sum())


def test_active_share_by_sector_sums_to_fund_total(dl):
    """active_share is evaluated per security row, so it decomposes by any grouping."""
    by_sector = dl.query(metrics=["active_share"], dimensions=["security.sector"], filters={"fund.code": "GLB_EQ"}, as_of=AS_OF).df
    total = dl.query(metrics=["active_share"], filters={"fund.code": "GLB_EQ"}, as_of=AS_OF).df
    assert by_sector.active_share.sum() == pytest.approx(total.active_share[0])
