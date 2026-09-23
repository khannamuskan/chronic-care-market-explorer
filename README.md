# Chronic Care Market Prioritization Explorer

[![CI](https://github.com/khannamuskan/chronic-care-market-explorer/actions/workflows/ci.yml/badge.svg)](https://github.com/khannamuskan/chronic-care-market-explorer/actions/workflows/ci.yml)

An end-to-end data product that ranks US states on **where a digital chronic-care
company should deploy next** — blending disease burden, trajectory, health-equity
gaps, untreated patients and **market size** across diabetes, cardiovascular,
weight and behavioral health.

Built with Python + Streamlit on the **CDC Chronic Disease Indicators** public
API (keyless, no signup, public domain).

---

## Quick start

```bash
git clone <repository-url>
cd dario-chronic-care-explorer
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python etl.py
streamlit run app.py
```

**No API key, no signup, no Docker, no database server, no manual data prep.**
`python etl.py` downloads ~47,000 records in about 35 seconds and writes every
processed table to `data/processed/`. `streamlit run app.py` then opens on
<http://localhost:8501>.

| Extra flag | What it does |
|---|---|
| `python etl.py --offline` | Rebuilds everything from the cached raw snapshot in `data/raw/` — no network needed |
| `python etl.py --allow-dq-failures` | Exits 0 even when a blocking data quality check fails |
| `python etl.py --verbose` | Debug logging |
| `pytest tests -q` | Runs the 72-test suite (no network required) |
| `python scripts/refresh_population.py` | Rebuilds the committed Census population reference (rarely needed — see §3) |

> **Optional:** a free [Socrata app token](https://evergreen.data.socrata.com/signup)
> raises the anonymous rate limit. Set `SOCRATA_APP_TOKEN` if you hit throttling.
> **It is not required** — the pipeline runs fully unauthenticated.

---

## 1. The product

### The question

A digital chronic-care company sells condition-management programmes to health
plans and employers. Sales and clinical capacity are finite, so the recurring
commercial question is: **which states do we enter next, and which populations
do we design for?**

Raw disease prevalence is a poor answer on its own. It favours large,
already-saturated markets, and it says nothing about whether the problem is
growing or about who is being missed. This app blends four signals instead.

### The Opportunity Score

Each component is percentile-ranked 0–100 across states, then blended.
Ranking rather than raw values stops indicators with different natural scales
(diabetes ≈ 11%, obesity ≈ 35%) from dominating one another.

| Component | Weight | Reads as | Built from |
|---|---|---|---|
| **Disease burden** | 45% | How much chronic disease exists today | Mean percentile of 7 burden indicators |
| **Worsening trend** | 20% | Is it getting worse | OLS slope of the burden index over 5 years |
| **Equity gap** | 20% | Unmet need concentrated in specific groups | Mean within-state race/ethnicity spread |
| **Untreated care gap** | 15% | Diagnosed but not on treatment | 100 − % taking BP/cholesterol medication |

Weights are **adjustable live in the sidebar**. The component ranks are
persisted by the ETL, so re-weighting reshuffles the ranking instantly without
re-running the pipeline — the scoring logic stays inspectable rather than a
black box.

Where a state is missing a component, the remaining weights are **re-normalised**
so it is not silently penalised for a source gap.

### Rate or lives: two rankings, not one

A score built from rates answers *where is need most concentrated* — and it
systematically favours small states. Multiplying those rates by each state's
adult population answers a different and equally valid question: *where are the
most affected people*. The app ranks both ways and shows where they disagree.

| Basis | Ranks on | Reads as | Favours |
|---|---|---|---|
| **Rate** | Opportunity Score | Efficiency — best conditions per patient reached | Small states |
| **Lives** | Opportunity Score × condition caseload | Volume — total reachable population | Large states |

`condition caseload` = Σ over burden indicators of (prevalence % × adult
population). It counts **condition-cases, not unique people** — an adult with
both diabetes and hypertension is counted twice. That is the correct unit for a
company that sells and delivers programmes *per condition*, but it is never
labelled "people" anywhere in the app.

The denominator is adults 18+, not total population, because BRFSS only
interviews adults. Using total population would have inflated every headcount
by roughly the 22% of Americans who were never eligible to be surveyed.

### Selected findings (2023 data, default weights)

- **Ohio ranks #1 by rate and #4 by lives** — the only state in the top 5 of
  both. High burden (76th percentile), a worsening trend, a 16pp race gap, and
  16.9M condition-cases. It is the one recommendation that survives either
  commercial thesis.
- **Oregon ranks #2 on a middling burden index (46)** — driven almost entirely
  by the worst untreated rate in the country (57% of diagnosed adults not on
  medication). A pure prevalence ranking would have missed it completely.
- **The two lenses disagree violently: 18 of 49 states move 10+ places.**
  New York is 36th by rate but **5th by lives**; Delaware is 8th by rate and
  **38th by lives**. Publishing only the rate ranking would have quietly
  recommended Delaware over New York.
- **80.8M US adults** report high blood pressure or high cholesterol without
  taking medication for it — the directly addressable population for an
  adherence programme.
- **Mississippi has a 19.9pp race gap in diabetes** (12.8% → 32.7%), the widest
  in the dataset.
- **Kentucky and Pennsylvania published no 2023 data at all** and are excluded
  from the ranking — surfaced explicitly in the app rather than silently dropped.

---

## 2. The API

**CDC Chronic Disease Indicators (CDI)** — `https://data.cdc.gov/resource/hksd-2xuw.json`

- Keyless, public domain, no signup, no reviewer-provided secrets.
- ~470,000 records; US state × indicator × year × demographic stratum.
- Underlying source is BRFSS, CDC's telephone health survey.
- Ships published 95% confidence intervals and explicit suppression footnotes,
  which makes genuine data quality work possible rather than cosmetic.

**Scope:** 9 adult indicators across 4 pillars, chosen to mirror the product
lines of a real chronic-care company.

| Pillar | Indicators |
|---|---|
| Diabetes | Diabetes prevalence |
| Cardiovascular | High blood pressure · High cholesterol · **BP medication taken** · **Cholesterol medication taken** |
| Weight & Activity | Obesity · No leisure-time physical activity |
| Behavioral Health | Depression · Frequent mental distress |

The two **medication** indicators are `protective` (higher is better) and are
used to compute the care gap, not the burden index. Polarity is declared per
indicator in `src/config.py` so a future indicator cannot silently flip a score.

---

## 3. Architecture and ETL

```
CDC Socrata API  (keyless HTTPS)
      │
      ▼
src/extract.py ──────► data/raw/cdi_raw_<ts>.jsonl.gz   (immutable snapshot)
      │                 • offset pagination with a deterministic $order
      │                 • retry + exponential backoff on 429/5xx and transport errors
      │                 • fail-fast on 4xx (a bad query should not be retried)
      │                 • row-count reconciliation against the API's own count(1)
      ▼
src/transform.py ────► star schema
      │                 fact_indicator  (grain: year × location × indicator
      │                                         × measure_type × stratum)
      │                    ├── dim_location    state/territory/national, region, lat/lon
      │                    ├── dim_indicator   pillar, polarity, role in score
      │                    └── dim_stratum     Overall / Race / Sex / Age
      ▼
src/quality.py ──────► 15 severity-graded checks ──► dq_results + dq_summary.json
      │                 ERROR blocks the run (non-zero exit); WARNING/INFO inform
      ▼
src/analytics.py ────► 4 marts        ◄── src/population.py
      │                 mart_indicator_state_year · mart_pillar_burden
      │                 mart_equity_gap · mart_state_scorecard
      ▼
src/storage.py ──────► data/processed/*.parquet  +  warehouse.duckdb
      ▼
app.py ──────────────► Streamlit, 5 tabs

reference/state_adult_population.csv   committed, provenance-stamped
      ▲
scripts/refresh_population.py          run deliberately, not per-ETL
```

### Why the population reference is committed, not fetched

Market sizing needs a denominator, and the obvious source — the Census API —
**now requires a registered key**. Adding it would have broken the
"clone it and run it, no credentials" guarantee the whole submission rests on.

Census publishes the same estimates as static, keyless CSV files, so
`scripts/refresh_population.py` pulls one of those, validates it, and writes a
provenance-stamped snapshot to `reference/`. That file is committed.

Two properties fall out of this, both deliberate:

- `python etl.py` keeps **exactly one** network dependency, so there is one
  fewer way for a reviewer's run to fail.
- The denominator behind every published headcount is **reviewable in a diff**
  rather than shifting silently between runs. A market-size number that changes
  because someone re-ran the pipeline on a Tuesday is not a number you can take
  into a planning meeting.

This is the one place the project deliberately prefers a checked-in artefact to
a live fetch, and `.gitignore` treats `reference/` differently from `data/` for
exactly that reason.

### Why a star schema

The CDI feed is a tall "one row per estimate" file with 30+ heavily redundant
columns — every row repeats the full location name, question text and
stratification labels. Splitting it into conformed dimensions keeps the fact
narrow, makes the app's filters cheap, and creates real referential-integrity
checks to run (`DQ10`).

### Why both Parquet and DuckDB

Parquet is the portable artefact — a reviewer can open any table with pandas
alone. DuckDB gives the same tables a SQL surface with no server:

```python
from src import storage
storage.query("""
    SELECT location_name, opportunity_score, mean_race_gap_pp
    FROM mart_state_scorecard ORDER BY opportunity_score DESC LIMIT 5
""")
```

### Reproducibility

Every run writes `data/processed/run_manifest.json` with full lineage: source
URL, indicators in scope, row counts at each stage, rows dropped and why,
duplicates collapsed, the data quality verdict, and the output paths. The raw
snapshot is kept so `--offline` can rebuild the exact same outputs without
touching the network.

### Key modelling decisions

| Decision | Why |
|---|---|
| Prefer age-adjusted over crude prevalence | State age structures differ enough (Florida vs Utah) that crude prevalence misleads on cross-state comparison. Crude is the documented fallback and the measure used is recorded on every row. |
| Percentile-rank before averaging | Prevents obesity (~35%) from swamping diabetes (~11%) in a composite index. |
| Overall stratum only in the headline mart | Mixing Overall with demographic strata would double-count a population. |
| One measure type per equity comparison | A gap must never be crude-vs-age-adjusted; that would measure the method, not the disparity. |
| Territories excluded from the ranking | Different survey design and market structure; they remain in the warehouse and are flagged `is_addressable_market = false`. |

---

## 4. The Streamlit application

| Tab | What it answers |
|---|---|
| **🎯 Market Opportunity** | Choropleth + top-10 ranking under either ranking basis, a driver heatmap showing *why* each state ranks where it does, a concentration-vs-scale scatter exposing where the two bases disagree, and the full scorecard with CSV export |
| **📍 State Deep Dive** | One state vs the national benchmark per indicator, plus multi-year trends with CDC's published 95% confidence band |
| **⚖️ Equity & Access** | Within-state gaps by race / sex / age, and a burden-vs-untreated quadrant chart that isolates the most actionable markets |
| **🔍 Data Quality** | All 15 check results, a suppression heatmap, a state-coverage matrix, failing-row samples, and the full lineage manifest |
| **ℹ️ Method & Data Model** | Scoring methodology, the star schema, and an honest limitations section |

Interactivity: survey year, condition pillars, census region, **ranking basis
(rate vs lives)**, and the four Opportunity Score weights.

---

## 5. Data quality and validation

15 checks across six dimensions. Each one targets a failure mode that **actually
exists in this feed** — none are decorative.

| ID | Check | Dimension | Severity |
|---|---|---|---|
| DQ01 | Extract row count reconciles with the API's own `count(1)` | completeness | ERROR |
| DQ02 | All modelled columns present | consistency | ERROR |
| DQ03 | Business keys populated | completeness | ERROR |
| DQ04 | One row per declared grain | uniqueness | ERROR |
| DQ05 | Prevalence within 0–100% | validity | ERROR |
| DQ06 | Confidence bounds ordered (`ci_low ≤ ci_high`) | validity | ERROR |
| DQ07 | Point estimate sits inside its own interval | accuracy | WARNING |
| DQ08 | CDC suppression rate within tolerance | completeness | WARNING → ERROR >35% |
| DQ09 | Every null estimate is explained by a CDC footnote | completeness | WARNING |
| DQ10 | Fact keys resolve to dimensions | consistency | ERROR |
| DQ11 | ≥45 of 51 states covered per indicator-year | completeness | WARNING |
| DQ12 | Race/ethnicity strata stable across years | consistency | WARNING |
| DQ13 | Robust-z outliers flagged for review | accuracy | INFO |
| DQ14 | Source data freshness | timeliness | WARNING |
| DQ15 | Every addressable state has a population denominator | completeness | ERROR |

### The distinction that matters most

**DQ08 vs DQ09.** CDC blanks any estimate whose sample is too small to be
reliable and stamps a footnote on it. That is *their* deliberate suppression and
it is expected — currently **25% of rows**, which trips the 20% warning
threshold by design so it stays visible. A null **without** a footnote is a
different animal entirely: that would be *our* parsing loss. The pipeline
separates the two and only the second is treated as a defect.

This matters commercially: suppression is **not random**. It clusters in small
states and small race/ethnicity groups — precisely where the equity analysis
needs data. The Data Quality tab maps exactly where the holes are so a
stakeholder does not read a missing gap as a nonexistent one.

**Why DQ15 is an ERROR.** A missing population row does not produce a wrong
number, it produces a *blank* one — and a blank in a market-size column reads as
"small market" to anyone skimming the table. Absence that looks like evidence is
worse than a loud failure, so it blocks the run.

### Error handling

- **Transport / 429 / 5xx** → exponential backoff, up to 4 attempts.
- **4xx** → fail fast with the offending query echoed (retrying bad SoQL is waste).
- **Zero rows returned** → explicit error pointing at `src/config.INDICATORS`,
  because the likeliest cause is CDC re-coding a `questionid`.
- **API unreachable** → the error message tells you to use `--offline`.
- **A data quality check itself raising** → caught, recorded as a failed check,
  the run continues; one broken check never takes down the pipeline.
- **Blocking failure** → processed tables are *still written* so they can be
  inspected in the app, but the process **exits non-zero** so a scheduler or CI
  job can alert on it.

### Tests and CI

72 tests, no network access required (all HTTP is stubbed), covering retry and
pagination logic, CDC's null-ish placeholder tokens, suppression vs unexplained
loss, duplicate collapsing, schema stability when Socrata omits null columns,
protective-indicator orientation, score re-normalisation around missing
components, the population reference loader, and both ranking bases.

```bash
pytest tests -q     # 72 passed
```

Every data quality test injects a specific defect and asserts the matching check
catches it — a check that cannot fail is not a check.

**GitHub Actions** (`.github/workflows/ci.yml`) runs two jobs:

| Job | When | What it proves |
|---|---|---|
| `test` | every push and PR, Python 3.11 + 3.12 | The suite passes, and `streamlit run app.py` **before** the ETL shows a clear instruction rather than a traceback |
| `live-pipeline` | weekly schedule + manual dispatch | The full ETL still succeeds against the live CDC API; the run manifest and DQ report are uploaded as artifacts |

The live job is deliberately **kept off pull requests**: a third-party endpoint
having a bad minute must never turn an unrelated change red. Running it weekly
turns upstream drift — a re-coded `questionid`, a withdrawn dataset — into a
failing build instead of a wrong number nobody noticed.

---

## 6. Assumptions

- **BRFSS is a self-reported telephone survey.** It measures *reported*
  prevalence and therefore understates undiagnosed disease.
- **50 states + DC are the addressable market.** Territories have different
  survey design and market structure; they stay in the warehouse but are
  excluded from the ranking.
- **The latest survey year is the scoring year.** States absent that year are
  excluded and named in the UI.
- **Percentile ranking is the right normaliser** for a *relative* market-
  prioritisation question. It deliberately says nothing about absolute severity.
- **The four score components are treated as independent.** In reality burden
  and equity gap are correlated; the weighting does not adjust for that.
- **Suppression is missing-not-at-random** and is reported rather than imputed.

## 7. Known limitations

- **Caseload counts condition-cases, not unique people.** An adult with both
  diabetes and hypertension is counted twice. This is the right unit for
  per-condition programme sizing, but it is *not* a count of patients, and it
  cannot be summed across pillars to estimate distinct individuals. BRFSS
  publishes no comorbidity cross-tabs, so a unique-persons figure is not
  derivable from this source.
- **The population reference is a 2023 vintage matched to the 2023 survey
  year.** If CDC releases 2024 indicators before the reference is refreshed,
  the denominator and the numerator fall out of step. The ETL records both
  years in the run manifest and the app reports the mismatch rather than
  assuming it away.
- **Equity gaps are biased toward measurable groups.** Small populations are
  suppressed, so the true widest gap may be invisible in exactly the states
  that need it most.
- **The trend slope spans COVID.** 2020–2021 BRFSS collection was disrupted;
  slopes through that window are noisier than they look.
- **No significance testing on gaps.** A 3pp gap with overlapping confidence
  intervals is presented the same as a statistically robust one. The app greys
  out wide intervals but does not test them. **This is now the biggest gap.**
- **Age-adjusted is unavailable for some indicator-years**, so a small number of
  rows mix measure types across years (the measure used is recorded per row).
- **Correlation, not attribution.** A high score signals unmet need, not proven
  programme ROI.
- **Full-refresh only.** At this volume (~47k rows, ~35s) incremental loading
  would be unjustified complexity, but it would be required at scale.

## 8. What I would improve with more time

1. **Statistical significance on equity gaps** using the published confidence
   intervals, so the app distinguishes a real disparity from sampling noise.
   Highest-value remaining change.
2. **Insured / uninsured and income splits** on top of the population join, to
   separate the addressable market from the *payable* one.
3. **Incremental loading with a watermark** on `yearstart`, plus schema-drift
   detection on the raw payload.
4. **Move the DQ suite to Great Expectations or Soda** for a standard artefact
   and historical DQ trending, rather than a bespoke framework.
5. **Add a second, independent source** (CMS spending, county-level PLACES) to
   triangulate — single-source products are fragile.
6. **County-level granularity** via CDC PLACES, since health-plan contracting
   happens well below state level.
7. **Orchestration** (Dagster/Airflow) with asset lineage and alerting on the
   non-zero exit the ETL already emits.

---

## 9. How AI was used

The full, unedited conversation transcript is in **[`ai_transcript/`](ai_transcript/)**.

AI was used as a **pair engineer under review**, not as a code generator. The
things that materially shaped the solution:

**Where AI added the most value**

- **API reconnaissance.** Probing several candidate CDC datasets and discovering
  that the widely-cited CDI dataset ID (`g4ie-h725`) now returns **403 Forbidden**
  while `hksd-2xuw` is the live replacement. Verifying this *before* writing any
  code avoided building on a dead endpoint.
- **Grounding the scope in real codes.** Querying the API's own `$group`
  aggregations to enumerate the actual `topicid` / `questionid` / stratification
  values, rather than assuming them. This is why `MEN` (not the assumed `MTH`)
  is the correct Mental Health topic code.
- **Finding the genuine data quality story.** Inspecting footnote behaviour
  revealed the suppression-vs-unexplained-null distinction that became the
  centrepiece of the DQ layer.

**Where AI output was wrong and had to be corrected**

- A pandas bug in the equity mart: `sort_values()` before a `groupby().transform()`
  comparison produced `ValueError: Can only compare identically-labeled Series`.
  Caught by running the pipeline, fixed by resetting the index first.
- The first scorecard silently dropped Kentucky and Pennsylvania (49 states, not
  51). Rather than accepting the number, I traced it to genuinely absent 2023
  BRFSS data and changed the design to **name the excluded states in the UI** —
  a silent drop in a league table is a correctness bug.
- Deprecated Streamlit `use_container_width` usage, replaced with the current
  `width=` API.
- Non-contiguous data quality check IDs (DQ11/DQ12 were skipped) — a cosmetic
  issue, but it would have made the DQ table confusing to a reviewer.
- The first attempt at market sizing multiplied prevalence by **total** state
  population. BRFSS only surveys adults, so that inflated every headcount by
  roughly 22%. The denominator was changed to the civilian 18+ population.
- The Census **API** was the obvious source for population, but it now requires
  a registered key — which would have broken the keyless clone-and-run promise.
  Switching to Census's static published CSVs preserved it.

**Judgement applied against AI suggestions**

- Kept scope deliberately small: 9 curated indicators over 4 pillars rather than
  all 19 CDI topics. A focused product beats a data dump.
- Rejected raw-value averaging for the burden index in favour of percentile
  ranking, because prevalence scales differ by 3× across indicators.
- Insisted the score components be **persisted** so the app can re-weight live —
  making the methodology auditable instead of a black box.
- Required that every DQ test inject a real defect and assert the check catches
  it, rather than only testing the happy path.

---

## 10. Repository structure

```
README.md
requirements.txt
app.py                  Streamlit application (5 tabs)
etl.py                  ETL entry point / CLI orchestrator
.github/workflows/
  ci.yml                Tests on every push; live ETL weekly
src/
  config.py             Indicator scope, API settings, DQ thresholds, score weights
  extract.py            Paged API client, retry/backoff, raw snapshots, offline replay
  transform.py          Star schema modelling, typing, dedup, suppression flags
  quality.py            15 severity-graded data quality checks
  analytics.py          4 analytical marts + the Opportunity Score + dual ranking
  population.py         Loader for the committed population reference
  storage.py            Parquet + DuckDB persistence
scripts/
  refresh_population.py Regenerates the population reference from Census
reference/
  state_adult_population.csv   Committed, provenance-stamped denominator
data/
  raw/                  Immutable gzipped JSONL API snapshots + latest.json pointer
  processed/            Parquet tables, warehouse.duckdb, run_manifest.json, dq_summary.json
tests/                  72 tests (no network required)
ai_transcript/          Full AI conversation transcript
logs/                   ETL run logs
```

`data/` and `logs/` contents are gitignored — they are fully regenerated by
`python etl.py`, which is what keeps the clone-and-run flow honest. `reference/`
is the deliberate exception and **is** committed, for the reasons in §3.

---

## Data sources and licence

- **CDC Chronic Disease Indicators**, U.S. Centers for Disease Control and
  Prevention. Public domain, retrieved without authentication from
  [data.cdc.gov](https://data.cdc.gov/Chronic-Disease-Indicators/U-S-Chronic-Disease-Indicators/hksd-2xuw).
- **U.S. Census Bureau**, Population Estimates Program, Vintage 2024 — civilian
  population by single year of age. Public domain, retrieved from the Bureau's
  published static estimate files (no API key).

No confidential, proprietary or personally identifiable data is used anywhere in
this project.
