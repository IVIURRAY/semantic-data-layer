"""A guided tour of the data layer. Run with `make demo` (or locally with OMS_DSN/DATA_DIR set).

Every query below is expressed purely in business terms. The data behind it
lives in Postgres, Parquet, SQLite and a CSV feed, using three identifier
schemes and two weight conventions; none of that appears here.
"""

import pandas as pd

from semlayer import DataLayer, QueryError

pd.set_option("display.width", 160)
pd.set_option("display.max_columns", 12)
pd.set_option("display.float_format", lambda v: f"{v:,.4f}")

AS_OF = "2026-09-30"
dl = DataLayer.from_env()


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


section("1. Sector active weights for one fund")
res = dl.query(
    metrics=["portfolio_weight", "benchmark_weight", "active_weight"],
    dimensions=["security.sector"],
    filters={"fund.code": "GLB_EQ"},
    as_of=AS_OF,
    order_by=["-active_weight"],
)
print(res.df)
print("\nSectors with zero portfolio weight still show up as underweights (full outer join).")

section("2. ...and what actually ran (explain)")
print(res.explain())

section("3. Fund summary over time: active share, credit quality, ESG tilt")
print(
    dl.query(
        metrics=["market_value", "holding_count", "active_share", "active_rating_score", "active_esg_score", "esg_coverage"],
        dimensions=["fund.code", "time.as_of_date"],
        date_range=("2026-06-30", AS_OF),
    ).df
)

section("4. Point-in-time: S0001 was reclassified on 2026-08-15")
print(
    dl.query(
        metrics=["portfolio_weight"],
        dimensions=["time.as_of_date", "security.name", "security.sector"],
        filters={"fund.code": "GLB_EQ", "security.id": "S0001"},
        date_range=("2026-06-30", AS_OF),
        order_by=["time.as_of_date"],
    ).df
)

section("5. Credit quality: S0003 is cut to high yield on 2026-09-10 (a fallen angel)")
print(
    dl.query(
        metrics=["portfolio_weight", "benchmark_weight", "active_weight"],
        dimensions=["time.as_of_date", "security.rating_grade"],
        filters={"fund.code": "GLB_EQ"},
        date_range=("2026-08-31", AS_OF),
    ).df
)
print("\nEmpty rating_grade = no credit rating. ESG ratings are separate: security.esg_rating.")

section("6. Guard rails: queries that would return wrong numbers are refused")
for desc, kwargs in [
    ("weights summed across funds", dict(metrics=["portfolio_weight"], dimensions=["security.sector"], as_of=AS_OF)),
    ("weights by PM (one-to-many)", dict(metrics=["portfolio_weight"], dimensions=["fund.code", "fund.portfolio_manager"], as_of=AS_OF)),
    ("market value summed over dates", dict(metrics=["market_value"], dimensions=["fund.code"], date_range=("2026-06-30", AS_OF))),
]:
    try:
        dl.query(**kwargs)
    except QueryError as e:
        print(f"- {desc}:\n    {e}")

section("7. Data quality surfaced, not hidden")
res = dl.query(metrics=["portfolio_weight"], dimensions=["security.region"], filters={"fund.code": "UK_EQ"}, as_of=AS_OF)
print(res.df)
for w in res.plan.warnings:
    print("WARNING:", w)
