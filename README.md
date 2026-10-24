# Asset management semantic layer

This is a small reference implementation of a **semantic layer over federated data**. You ask for metrics like `active_weight` by dimensions like `security.sector`. The layer figures out that:

- holdings live in **Postgres** (keyed by SEDOL, weights in percent);
- benchmark constituents live in a **Parquet lake** (keyed by ISIN);
- sectors and credit ratings live in a **SQLite** security master, with date-range validity;
- ESG ratings arrive as a **vendor CSV**.

It then pushes filters down to each store, resolves identifiers, joins attributes point-in-time, and returns the answer. It also **refuses** queries that would quietly produce wrong numbers.

```python
dl.query(
    metrics=["portfolio_weight", "benchmark_weight", "active_weight"],
    dimensions=["security.sector"],
    filters={"fund.code": "GLB_EQ"},
    as_of="2026-09-30",
)
```

```
           security.sector  portfolio_weight  benchmark_weight  active_weight
0              Industrials            0.2193            0.0767         0.1426
1                Materials            0.2007            0.0898         0.1109
...
8                Utilities            0.0000            0.0842        -0.0842   <- not held, still an underweight
```

📐 **Read [docs/DESIGN.md](docs/DESIGN.md)** for the problem, the mental model, the domain rules and the trade-offs.

## How it works

Three short animations, best watched in order. Each one zooms in further: the whole estate, then one query, then how that query's joins are decided. The step-by-step guide under each GIF follows what happens on screen.

### 1. The estate, and the layer over it

![Four physical stores with mismatched conventions, first tangled up with every consumer, then sitting behind a two-part semantic layer](docs/img/01-estate.gif)

**What it shows:** why the layer exists, and how it's split into "what the data means" and "where it lives".

The data lives in four stores that disagree on identifiers (SEDOL, ISIN, internal id), units (percent vs fraction) and date formats. Without a layer, every notebook, dashboard and agent re-learns those quirks. With one, the layer has two halves. `physical.yaml` absorbs where and how the data is stored. `semantic.yaml` defines what it means. Consumers only ever see the vocabulary in the top half.

<details>
<summary><b>Step by step</b></summary>

1. **The physical estate (0–4s).** Four stores appear. The **OMS** (Postgres) holds funds and holdings keyed by SEDOL, with weights in percent. The **data lake** (Parquet) holds benchmark weights keyed by ISIN and by benchmark rather than fund. The **security master** (SQLite) holds the canonical security id, the identifier crosswalk, dated sector classifications and dated credit ratings. The **vendor feed** (CSV) holds ESG ratings with `YYYYMMDD` dates. The coloured chips on each card are the quirks a consumer would otherwise have to know about.
2. **Without a layer (4–8s).** Three consumers (an analyst notebook, a dashboard and an LLM agent) each connect to every store: twelve red connections. Each consumer reimplements the ID mapping, unit fixes and joins, and each can get them wrong differently.
3. **`physical.yaml` (8–10s).** The tangle disappears. A mapping layer sits directly over the stores, and its chips show what it absorbs per store: `% → fraction`, `SEDOL → security_id`, `ISIN → security_id`, `text → DATE`, `SCHEME = 'GICS'`.
4. **`semantic.yaml` (10–12s).** Above it, the business vocabulary appears: entities (`fund`, `benchmark`, `security`, `time`), metrics (`active_weight`, `active_share`, …) and dimensions (`security.sector`, `security.rating`).
5. **One interface (12s onward).** Each consumer now has a single green connection, to the semantic layer only, with data flowing up from the stores through both halves. Moving a table between databases changes `physical.yaml`; no consumer query changes.

</details>

### 2. Life of a query

![The parts of a query explained, then the actual SQL sent to each database, colour-coded by which part of the query produced it](docs/img/02-query-flow.gif)

**What it shows:** the exact SQL the layer sends to each database for one query, and which part of the query caused each piece of it.

A query has four parts, all in business terms. The animation uses one colour per part throughout:

| Part | Colour | Meaning | Example |
|---|---|---|---|
| **metrics** | 🟡 yellow | what to calculate (the numbers) | `active_weight` = portfolio weight − benchmark weight |
| **dimensions** | 🔵 blue | how to break it down (like `GROUP BY`) | `security.sector`, one result row per sector |
| **filters** | 🟢 green | which subset | `fund.code = GLB_EQ` |
| **as_of** | 🟠 orange | which date | `2026-09-30` |

<details>
<summary><b>Step by step</b></summary>

1. **Anatomy of the query (0–10s).** The query is shown large, and each argument gets a callout saying what it means. Below it is the shape of the answer: one row per dimension value (sector), one column per metric (`active_weight`). Nothing in the query names a table or a database.
2. **Query 1 → OMS (Postgres).** The filter `fund.code = GLB_EQ` becomes `WHERE "fund_id" IN ('GLB_EQ')`. One row comes back, and it tells the layer the fund's benchmark is `IDX_GLB_ALL`. That decides which benchmark rows to fetch in query 4.
3. **Query 2 → Security master (SQLite).** The crosswalk from `security_id` to ISIN and SEDOL (60 rows). The OMS and the lake identify securities differently, so this is what lets their rows be matched later.
4. **Query 3 → OMS (Postgres).** The portfolio side of the metric. The metric becomes `"weight_percent" / 100.0`, because the OMS stores percent. The filter becomes `"fund_id" IN (...)`, and `as_of` becomes `"as_of_date" = '2026-09-30'`. 28 holdings come back.
5. **Query 4 → Data lake (Parquet).** The benchmark side of the metric. The benchmark id from query 1 becomes a filter, and `as_of` selects the date partition, so only one of four files is read. 60 constituents come back.
6. **Query 5 → Security master (SQLite).** The dimension `security.sector`. Sectors change over time, so `as_of` becomes a validity-window condition (`valid_from <= date < valid_to`), followed by a lookup of sector names in `gics`.
7. **Vendor feed (CSV): skipped.** Nothing in this query needs ESG ratings, so the file is never read.
8. **Query 6 → DuckDB (inside the layer).** The five results are combined in one SQL statement. It builds one row per fund × security × date from holdings and benchmark (`UNION ALL`, so benchmark-only names are kept), joins sectors as of the date, and computes `SUM(portfolio_weight) - SUM(benchmark_weight)`.
9. **Result.** Eleven sector rows go back to the user. Utilities isn't held at all, but still shows as a −8.4% underweight rather than a missing row.

</details>

### 3. How the layer joins: the same way for every query

![A join graph rooted at the position table; three user queries light up their join paths and the generated SQL; a one-to-many path is refused](docs/img/03-join-closeup.gif)

**What it shows:** how joins are decided when a query comes in. The user never writes them; the layer derives them from the model, using the same rules every time.

<details>
<summary><b>Step by step</b></summary>

1. **The base table (0–7s).** Every query starts from **position**: one row per fund × security × date, carrying both portfolio and benchmark weight. The example rows include `S0005` with portfolio weight `0.0000`. The fund doesn't hold it, but it's in the benchmark, so it stays as an underweight. Metrics are calculated from these rows, and dimensions are joins outward from them.
2. **The join graph (7–12s).** The other tables appear around position, linked by relationships declared once in `semantic.yaml`. **N:1** means each position row matches at most one row on the other side, so joining can't duplicate it. Tables marked ⏱ change over time and are joined "as of the date". `fund → fund_manager` is **1:N** (red, dashed): a fund can have several portfolio managers.
3. **Query 1: `dimensions = ["security.sector"]` (12–19s).** The path position → classification → gics lights up. Beside it is the JOIN SQL the layer generates, including the as-of condition `valid_from <= as_of_date < valid_to`. Every step is N:1, so sector weights still add up to 100%.
4. **Query 2: `dimensions = ["benchmark.name", "security.rating"]` (19–26s).** A different question takes a different path, position → fund → benchmark and position → credit_rating, but the JOIN SQL is generated the same way, with the same as-of condition and the same N:1 check.
5. **Query 3: `dimensions = ["fund.portfolio_manager"]` (26–32s).** This path crosses the 1:N link. The example shows why it's dangerous: GLB_EQ has two PMs, so every position row would be copied, and the fund would sum to 200%. The layer returns a `QueryError` before any database is queried.
6. **Summary (32s onward).** All the safe paths light up. You name the dimensions; the layer picks the path, adds the as-of conditions, and refuses paths that would double count.

</details>

## Quick start

Requires Docker.

```bash
make up      # Postgres + seed all four stores + API on http://localhost:8000
make demo    # guided walkthrough (examples/walkthrough.py)
make test    # test suite, inside the container
make down    # stop and delete data
```

Without `make`: `docker compose up -d --build --wait`, then `docker compose run --rm api python examples/walkthrough.py`.

## Try the API

The API is read-only: `GET /catalog` to see what you can ask for, `POST /query` to ask. (`/query` is a POST only because its body doesn't fit comfortably in a URL.) Interactive docs are at http://localhost:8000/docs.

```bash
# What can I ask for?
curl -s localhost:8000/catalog | jq '.metrics | keys'

# Sector active weights, with the execution plan
curl -s localhost:8000/query -H 'content-type: application/json' -d '{
  "metrics": ["portfolio_weight", "benchmark_weight", "active_weight"],
  "dimensions": ["fund.code", "security.sector"],
  "filters": {"fund.code": "GLB_EQ"},
  "as_of": "2026-09-30",
  "explain": true
}' | jq '.rows, .plan.source_queries[].source'

# Fund summary across the quarter
curl -s localhost:8000/query -H 'content-type: application/json' -d '{
  "metrics": ["active_share", "active_rating_score", "active_esg_score", "esg_coverage"],
  "dimensions": ["fund.code", "time.as_of_date"],
  "date_range": ["2026-06-30", "2026-09-30"]
}' | jq '.rows'

# A query that would be wrong gets a 400 that explains why
curl -s localhost:8000/query -H 'content-type: application/json' \
  -d '{"metrics": ["portfolio_weight"], "dimensions": ["fund.strategy"], "as_of": "2026-09-30"}'
```

## What's in the model

| | |
|---|---|
| **Entities** | `fund`, `benchmark`, `security`, `time` |
| **Dimensions** | `fund.code/name/strategy/base_currency` · `benchmark.code/name/provider` · `security.id/name/isin/sedol/country/region/currency` · `security.sector/industry_group/industry/sub_industry` (point-in-time GICS) · `security.rating/rating_score/rating_grade` (point-in-time credit rating, AAA..D, investment grade / high yield) · `security.esg_rating/esg_score` (point-in-time ESG, AAA..CCC) · `time.as_of_date` |
| **Metrics** | `market_value`, `portfolio_weight`, `benchmark_weight`, `active_weight`, `active_share`, `holding_count`, `portfolio_rating_score`, `benchmark_rating_score`, `active_rating_score`, `rating_coverage` (credit) · `portfolio_esg_score`, `benchmark_esg_score`, `active_esg_score`, `esg_coverage` |
| **Data** | 3 funds, 2 benchmarks, 60 fictional securities, month-ends Jun–Sep 2026 |

Everything consumer-facing is defined in [`model/semantic.yaml`](model/semantic.yaml). Everything about where it lives is in [`model/physical.yaml`](model/physical.yaml).

## Running locally without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export OMS_DSN=postgresql://semlayer:semlayer@localhost:5432/oms DATA_DIR=$PWD/data
python -m semlayer.seed                         # needs a reachable Postgres
pytest -q
uvicorn semlayer.api:app --reload
```

You can point `OMS_DSN` at the compose database (`localhost:55432`) to reuse it.

## Layout

```
model/                 semantic.yaml (meaning) · physical.yaml (location)
src/semlayer/          model · compiler · connectors · engine · api · seed
examples/              walkthrough.py
tests/                 one test per correctness rule
docs/DESIGN.md         design document
docs/animations/       source for the README GIFs (SVG timelines)
```

### Re-rendering the animations

Each GIF is a deterministic SVG timeline (`docs/animations/0*.html`) captured frame by frame with Playwright and encoded with ffmpeg:

```bash
npm i -D playwright && npx playwright install chromium   # once
node docs/animations/render.cjs                          # all -> docs/img/*.gif
node docs/animations/render.cjs 03-join-closeup --still 12,23   # PNG stills to check layout
```

Open any of the HTML files in a browser to watch it live (it loops), or call `seek(t)` in the console to scrub.

All companies, identifiers, index names and people in the seed data are fictional.
