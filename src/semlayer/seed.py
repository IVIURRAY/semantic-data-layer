"""Generate a deterministic, synthetic asset-management estate across four stores.

    Postgres (OMS)     fund, fund_manager, holding           keyed by SEDOL, weights in percent
    Parquet (lake)     benchmark + benchmark_constituents    keyed by ISIN and by benchmark (not fund),
                                                             weights as fractions, hive-partitioned by date
    SQLite (secmaster) security, classification, gics,       canonical security_id + crosswalk, dates as text,
                       credit_rating                         NULL = open-ended
    CSV (vendor)       esg_ratings.csv                       keyed by ISIN, dates as YYYYMMDD, blank = open-ended

Tables and columns use plain names. The differences that remain are the ones
real sources have and the layer must absorb: units, identifier schemes, date
formats, null conventions and tables that mix incompatible content.

All companies and identifiers are fictional. Planted scenarios used by the
tests and walkthrough:

  * S0001 is reclassified from Application Software (IT) to Interactive Media
    (Communication Services) effective 2026-08-15.
  * S0002's ESG rating is downgraded AA -> BB effective 2026-08-20.
  * S0003's credit rating is cut BBB- -> BB+ effective 2026-09-10: a fallen
    angel, moving from investment grade to high yield.
  * UK_EQ holds one off-benchmark US name, and on 2026-09-30 a new listing
    whose SEDOL is not yet in the security master.
  * GLB_EQ has two co-PMs (the fan-out trap).
  * classification also carries a second scheme (ICB) that must be filtered out.
  * credit_rating also carries short-term ratings (A-1, A-2, ...) that must be
    filtered out.
  * Some securities have no ESG rating, and some have no credit rating.

Usage: python -m semlayer.seed   (reads OMS_DSN and DATA_DIR)
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

DATES = [dt.date(2026, 6, 30), dt.date(2026, 7, 31), dt.date(2026, 8, 31), dt.date(2026, 9, 30)]
ESG_RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC"]
# Issuer long-term credit ratings, best to worst. Equity issuers cluster around A/BBB.
CREDIT_RATINGS = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
                  "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]
CREDIT_WEIGHTS = [1, 1, 2, 3, 4, 5, 6, 6, 6, 5, 4, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0]
UNRESOLVED_SEDOL = "BYXQ0N7"

GICS = [
    # sub_cd, sub_nm, ind_nm, grp_nm, sector_nm
    ("10102010", "Integrated Oil & Gas", "Oil, Gas & Consumable Fuels", "Energy", "Energy"),
    ("15101050", "Specialty Chemicals", "Chemicals", "Materials", "Materials"),
    ("20101010", "Aerospace & Defense", "Aerospace & Defense", "Capital Goods", "Industrials"),
    ("20106010", "Construction Machinery & Heavy Transportation Equipment", "Machinery", "Capital Goods", "Industrials"),
    ("25503030", "Broadline Retail", "Broadline Retail", "Consumer Discretionary Distribution & Retail", "Consumer Discretionary"),
    ("30202030", "Packaged Foods & Meats", "Food Products", "Food, Beverage & Tobacco", "Consumer Staples"),
    ("35202010", "Pharmaceuticals", "Pharmaceuticals", "Pharmaceuticals, Biotechnology & Life Sciences", "Health Care"),
    ("40101010", "Diversified Banks", "Banks", "Banks", "Financials"),
    ("40301040", "Property & Casualty Insurance", "Insurance", "Insurance", "Financials"),
    ("45103010", "Application Software", "Software", "Software & Services", "Information Technology"),
    ("45301020", "Semiconductors", "Semiconductors & Semiconductor Equipment", "Semiconductors & Semiconductor Equipment", "Information Technology"),
    ("50203010", "Interactive Media & Services", "Interactive Media & Services", "Media & Entertainment", "Communication Services"),
    ("55101010", "Electric Utilities", "Electric Utilities", "Utilities", "Utilities"),
    ("60106010", "Office REITs", "Office REITs", "Equity Real Estate Investment Trusts (REITs)", "Real Estate"),
]
SUFFIX = {
    "Energy": "Energy", "Materials": "Chemicals", "Industrials": "Industries", "Consumer Discretionary": "Retail",
    "Consumer Staples": "Foods", "Health Care": "Pharma", "Financials": "Financial", "Information Technology": "Systems",
    "Communication Services": "Media", "Utilities": "Power", "Real Estate": "Properties",
}
WORDS = (
    "Northwind Albion Halcyon Meridian Corvid Larchmont Pellucid Ostrava Brightwater Kestrel Thistle Vantor "
    "Calloway Ember Quillon Saffron Tamsin Wexford Aurelia Blackmoor Cinder Dunmore Fenwick Galloway Harrowgate "
    "Ironbridge Juniper Kingsmere Lumen Marlow Netherby Orrin Penrose Quarry Rowan Solent Tidewater Umber "
    "Verity Wyvern Yarrow Zephyr Ashdown Bramble Cobalt Drayton Elmstead Foxglove Granite Heron Inkwell "
    "Jasper Kelpie Lowther Mistral Nimbus Oakham Pippin Redwing Sable"
).split()
COUNTRIES = ["GB"] * 20 + ["US"] * 22 + ["DE"] * 5 + ["FR"] * 5 + ["JP"] * 5 + ["CH"] * 3
REGION = {"GB": "UK", "US": "North America", "DE": "Europe ex-UK", "FR": "Europe ex-UK", "CH": "Europe ex-UK", "JP": "Asia Pacific"}
CCY = {"GB": "GBP", "US": "USD", "DE": "EUR", "FR": "EUR", "CH": "CHF", "JP": "JPY"}

FUNDS = [
    # code, name, strategy, base ccy, benchmark, AUM USD
    ("GLB_EQ", "Global Equity Fund", "Core Global Equity", "USD", "IDX_GLB_ALL", 1_200_000_000),
    ("GLB_SUS", "Global Sustainable Equity Fund", "ESG Tilt Global Equity", "USD", "IDX_GLB_ALL", 600_000_000),
    ("UK_EQ", "UK Equity Income Fund", "UK Equity Income", "GBP", "IDX_UK_ALL", 400_000_000),
]
PMS = [("GLB_EQ", "A. Patel", "Lead"), ("GLB_EQ", "J. Okafor", "Co-PM"), ("GLB_SUS", "M. Rossi", "Lead"), ("UK_EQ", "L. Chen", "Lead")]
BENCHMARKS = [("IDX_GLB_ALL", "Synthetic Global All Cap Index", "Example Index Co"),
              ("IDX_UK_ALL", "Synthetic UK All Share Index", "Example Index Co")]


# ------------------------------------------------------------------ identifiers


def _isin(country: str, rng: random.Random) -> str:
    body = country + "".join(rng.choice("0123456789") for _ in range(9))
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return body + str((10 - total % 10) % 10)


def _sedol(rng: random.Random) -> str:
    body = "B" + "".join(rng.choice("0123456789BCDFGHJKLMNPQRSTVWXYZ") for _ in range(5))
    total = sum(int(c, 36) * w for c, w in zip(body, (1, 3, 1, 7, 3, 9)))
    return body + str((10 - total % 10) % 10)


# ------------------------------------------------------------------ universe


def build_universe(rng: random.Random) -> list[dict]:
    secs = []
    for i, country in enumerate(COUNTRIES):
        sub = GICS[9] if i == 0 else (GICS[0] if i == 1 else rng.choice(GICS))  # S0001 starts in App Software
        if i == 0:
            country = "US"
        secs.append({
            "sec_id": f"S{i + 1:04d}",
            "country": country,
            "isin": _isin(country, rng),
            "sedol": _sedol(rng),
            "sub": sub,
            "name": f"{WORDS[i]} {SUFFIX[sub[4]]}",
            "cap": rng.lognormvariate(10, 1.0),
            "esg_rating": rng.choices(ESG_RATINGS, weights=[1, 3, 4, 4, 3, 2, 1])[0],
        })
    secs[1]["esg_rating"] = "AA"  # S0002, downgraded later
    assert len({s["sedol"] for s in secs} | {UNRESOLVED_SEDOL}) == len(secs) + 1
    return secs


def benchmark_weights(secs, rng) -> dict[tuple[str, dt.date], dict[str, float]]:
    """(benchmark_id, date) -> {sec_id: weight}, cap weighted with monthly drift."""
    caps = {s["sec_id"]: s["cap"] for s in secs}
    out = {}
    for d in DATES:
        for b, members in (("IDX_GLB_ALL", secs), ("IDX_UK_ALL", [s for s in secs if s["country"] == "GB"])):
            total = sum(caps[s["sec_id"]] for s in members)
            out[(b, d)] = {s["sec_id"]: caps[s["sec_id"]] / total for s in members}
        caps = {k: v * rng.gauss(1.0, 0.04) for k, v in caps.items()}
    return out


def build_holdings(secs, bench, rng) -> list[tuple]:
    by_id = {s["sec_id"]: s for s in secs}
    good = [s["sec_id"] for s in secs if ESG_RATINGS.index(s["esg_rating"]) <= 2]
    uk = [s["sec_id"] for s in secs if s["country"] == "GB"]
    us = [s["sec_id"] for s in secs if s["country"] == "US"]

    glb_eq = ["S0001"] + rng.sample([s["sec_id"] for s in secs[2:]], 27)
    books = {
        "GLB_EQ": glb_eq,
        "GLB_SUS": ["S0002"] + rng.sample([g for g in good if g != "S0002"], min(21, len(good) - 1)),
        "UK_EQ": rng.sample(uk, 14) + [rng.choice([u for u in us if u != "S0001"])],
    }
    swap_out, swap_in = glb_eq[-1], next(s["sec_id"] for s in secs if s["sec_id"] not in glb_eq)
    tilts = {(f, s): rng.uniform(0.4, 2.2) for f, ids in books.items() for s in ids + [swap_in]}

    rows = []
    for fund, _, _, _, bmk, aum in FUNDS:
        for d in DATES:
            ids = list(books[fund])
            if fund == "GLB_EQ" and d >= dt.date(2026, 8, 31):
                ids = [swap_in if s == swap_out else s for s in ids]  # turnover
            bw = bench[(bmk, d)]
            avg = sum(bw.values()) / len(bw)
            raw = {s: bw.get(s, avg) * tilts[(fund, s)] for s in ids}
            extra = {}
            if fund == "UK_EQ" and d == DATES[-1]:
                extra[UNRESOLVED_SEDOL] = 0.015  # new listing, not yet in the security master
            scale = (1 - sum(extra.values())) / sum(raw.values())
            weights = {by_id[s]["sedol"]: w * scale for s, w in raw.items()} | extra
            for sedol, w in weights.items():
                rows.append((d, fund, sedol, round(w * aum / 25.0), round(w * aum, 2), round(w * 100, 4)))
    return rows


# ------------------------------------------------------------------ writers


def write_oms(dsn: str, holdings: list[tuple]) -> None:
    import psycopg

    for attempt in range(30):
        try:
            conn = psycopg.connect(dsn)
            break
        except psycopg.OperationalError:
            if attempt == 29:
                raise
            time.sleep(1)
    with conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS holding, fund_manager, fund")
        cur.execute(
            "CREATE TABLE fund (fund_id varchar(16) PRIMARY KEY, fund_name text NOT NULL, "
            "strategy text, base_currency char(3), benchmark_id varchar(16), aum_usd numeric(18,2))"
        )
        cur.execute("CREATE TABLE fund_manager (fund_id varchar(16) REFERENCES fund, manager_name text, manager_role text)")
        cur.execute(
            "CREATE TABLE holding (as_of_date date, fund_id varchar(16) REFERENCES fund, "
            "sedol char(7), quantity numeric(18,0), market_value numeric(18,2), weight_percent numeric(9,4), "
            "PRIMARY KEY (as_of_date, fund_id, sedol))"
        )
        cur.execute("CREATE INDEX ON holding (fund_id, as_of_date)")
        cur.executemany("INSERT INTO fund VALUES (%s, %s, %s, %s, %s, %s)", FUNDS)
        cur.executemany("INSERT INTO fund_manager VALUES (%s, %s, %s)", PMS)
        cur.executemany("INSERT INTO holding VALUES (%s, %s, %s, %s, %s, %s)", holdings)
    conn.close()


def write_lake(root: Path, secs, bench) -> None:
    isin = {s["sec_id"]: s["isin"] for s in secs}
    shutil.rmtree(root, ignore_errors=True)  # stale partitions would be read too
    root.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({k: [b[i] for b in BENCHMARKS] for i, k in enumerate(["benchmark_id", "benchmark_name", "provider"])}),
        root / "benchmark.parquet",
    )
    for d in DATES:
        part = root / "benchmark_constituents" / f"as_of_date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        rows = [(b, isin[s], round(w, 8)) for (b, bd), ws in bench.items() if bd == d for s, w in ws.items()]
        pq.write_table(
            pa.table({"benchmark_id": [r[0] for r in rows], "isin": [r[1] for r in rows], "benchmark_weight": [r[2] for r in rows]}),
            part / "part-0.parquet",
        )


def write_secmaster(path: Path, secs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE security (security_id TEXT PRIMARY KEY, security_name TEXT, isin TEXT UNIQUE, sedol TEXT UNIQUE,
                               country TEXT, region TEXT, currency TEXT);
        CREATE TABLE classification (security_id TEXT, scheme TEXT, classification_code TEXT, valid_from TEXT, valid_to TEXT);
        CREATE TABLE gics (sub_industry_code TEXT PRIMARY KEY, sub_industry_name TEXT, industry_name TEXT,
                           industry_group_name TEXT, sector_name TEXT);
        CREATE TABLE credit_rating (security_id TEXT, agency TEXT, rating_term TEXT, rating TEXT, valid_from TEXT, valid_to TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO security VALUES (?,?,?,?,?,?,?)",
        [(s["sec_id"], s["name"], s["isin"], s["sedol"], s["country"], REGION[s["country"]], CCY[s["country"]]) for s in secs],
    )
    classif = []
    for s in secs:
        if s["sec_id"] == "S0001":
            classif += [(s["sec_id"], "GICS", "45103010", "2020-01-01", "2026-08-15"),
                        (s["sec_id"], "GICS", "50203010", "2026-08-15", None)]
        else:
            classif.append((s["sec_id"], "GICS", s["sub"][0], "2020-01-01", None))
        classif.append((s["sec_id"], "ICB", "ICB" + s["sub"][0][:4], "2020-01-01", None))
    conn.executemany("INSERT INTO classification VALUES (?,?,?,?,?)", classif)
    conn.executemany("INSERT INTO gics VALUES (?,?,?,?,?)", GICS)
    conn.executemany("INSERT INTO credit_rating VALUES (?,?,?,?,?,?)", credit_ratings(secs))
    conn.commit()
    conn.close()


def credit_ratings(secs) -> list[tuple]:
    """Issuer credit rating history: (security_id, agency, rating_term, rating, valid_from, valid_to).

    Uses its own RNG so adding credit ratings doesn't shift the rest of the estate."""
    rng = random.Random(7)
    ids = [s["sec_id"] for s in secs]
    unrated = set(rng.sample(ids[3:], 6))  # not every equity issuer is rated
    changers = rng.sample([i for i in ids[3:] if i not in unrated], 6)
    rows = []
    for sid in ids:
        if sid in unrated:
            continue
        r = rng.choices(CREDIT_RATINGS, weights=CREDIT_WEIGHTS)[0]
        change = None
        if sid == "S0003":  # fallen angel
            r, change = "BBB-", (dt.date(2026, 9, 10), "BB+")
        elif sid in changers:
            i = CREDIT_RATINGS.index(r) + rng.choice([-1, 1])
            change = (dt.date(2026, 7, 1) + dt.timedelta(days=rng.randint(0, 85)), CREDIT_RATINGS[min(max(i, 0), len(CREDIT_RATINGS) - 1)])
        if change:
            rows.append((sid, "ExampleRatings", "LT", r, "2024-01-01", change[0].isoformat()))
            rows.append((sid, "ExampleRatings", "LT", change[1], change[0].isoformat(), None))
        else:
            rows.append((sid, "ExampleRatings", "LT", r, "2024-01-01", None))
        # Short-term ratings share the table but not the scale.
        short = "A-1" if CREDIT_RATINGS.index(r) <= 5 else ("A-2" if CREDIT_RATINGS.index(r) <= 8 else "B")
        rows.append((sid, "ExampleRatings", "ST", short, "2024-01-01", None))
    return rows


def write_esg_ratings(path: Path, secs, rng) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    unrated = {"S0045", "S0050", "S0058"}
    changers = rng.sample([s["sec_id"] for s in secs[2:] if s["sec_id"] not in unrated], 8)
    rows = []
    for s in secs:
        sid, r = s["sec_id"], s["esg_rating"]
        if sid in unrated:
            continue
        change = None
        if sid == "S0002":
            change = (dt.date(2026, 8, 20), "BB")
        elif sid in changers:
            new = ESG_RATINGS[min(max(ESG_RATINGS.index(r) + rng.choice([-1, 1]), 0), len(ESG_RATINGS) - 1)]
            change = (dt.date(2026, 7, 1) + dt.timedelta(days=rng.randint(0, 85)), new)
        if change:
            rows.append((s["isin"], "ExampleESG", r, "20250101", change[0].strftime("%Y%m%d")))
            rows.append((s["isin"], "ExampleESG", change[1], change[0].strftime("%Y%m%d"), ""))
        else:
            rows.append((s["isin"], "ExampleESG", r, "20250101", ""))
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["isin", "agency", "rating", "valid_from", "valid_to"])
        w.writerows(rows)


def seed(dsn: str, data_dir: str | Path) -> None:
    data_dir = Path(data_dir)
    rng = random.Random(42)
    secs = build_universe(rng)
    bench = benchmark_weights(secs, rng)
    holdings = build_holdings(secs, bench, rng)
    write_oms(dsn, holdings)
    write_lake(data_dir / "lake", secs, bench)
    write_secmaster(data_dir / "secmaster" / "secmaster.db", secs)
    write_esg_ratings(data_dir / "vendor" / "esg_ratings.csv", secs, rng)


def main() -> None:
    dsn, data_dir = os.environ.get("OMS_DSN"), os.environ.get("DATA_DIR")
    if not dsn or not data_dir:
        sys.exit("OMS_DSN and DATA_DIR must be set")
    seed(dsn, data_dir)
    print(f"Seeded OMS ({dsn.split('@')[-1]}) and file stores under {data_dir}")


if __name__ == "__main__":
    main()
