# AI Conversation Transcript

| File | What it is |
|---|---|
| **[`transcript.md`](transcript.md)** | The full, unedited conversation, every prompt, tool call and result |
| `export_transcript.py` | The script that renders the transcript from the CLI session log |

**Assistant:** Claude Opus 5, via GitHub Copilot CLI.

The transcript is generated programmatically from the CLI's raw session event
log rather than hand-curated, so it is verifiably complete. Regenerate it with:

```bash
python ai_transcript/export_transcript.py            # newest session
python ai_transcript/export_transcript.py <events.jsonl>
```

Only tool *outputs* are truncated (at 2,000 characters), because several return
full API payloads of tens of thousands of rows. Every prompt and every response
is reproduced in full.

---

## How AI was used, mapped to the assignment's questions

### 1. Understanding and breaking down the assignment

The assignment arrived as a `.docx`. It was extracted to text, and the
requirements were decomposed into a 14-item dependency-ordered build plan
(extract → transform → quality → analytics → storage → app → tests → docs)
before any code was written. The plan was reviewed and approved before
implementation started.

### 2. Selecting the API and designing the product direction

This was the highest-leverage AI use in the project, and it was **empirical, not
recalled from memory**:

- Candidate CDC datasets were probed live. The widely-cited Chronic Disease
  Indicators ID `g4ie-h725` returned **403 Forbidden**; `hksd-2xuw` is the live
  replacement. Verifying this *before* writing code avoided building an entire
  pipeline on a dead endpoint.
- The API's own `$group` aggregations were queried to enumerate real `topicid`,
  `questionid` and stratification codes rather than assuming them. This is how
  the Mental Health topic code was confirmed as `MEN`, not the assumed `MTH`.
- A sample row was pulled to confirm the feed actually ships confidence limits
  and suppression footnotes, the properties that make genuine data quality work
  possible rather than cosmetic.

Product direction was then chosen deliberately: rather than a generic "health
dashboard", the scope was narrowed to nine adult indicators mapping onto the
four condition pillars a chronic-care company actually sells, and framed around
one decision: *which market do we enter next*.

### 3. Designing the data model and ETL flow

The star schema, the declared grain, the polarity/measure-role metadata on each
indicator, and the four marts were designed up front and written into
`src/config.py` as data rather than scattered through the code. The
Opportunity Score was designed so its components are **persisted**, which is
what allows the app to re-weight live and keeps the methodology auditable
instead of a black box.

### 4. Debugging, validating and improving

Concrete defects found by running the code, not by reading it:

| Issue | How it surfaced | Fix |
|---|---|---|
| `ValueError: Can only compare identically-labeled Series` in the equity mart | First full ETL run crashed | `sort_values()` before a `groupby().transform()` comparison misaligned the index; reset the index first |
| Scorecard silently ranked 49 states, not 51 | Sanity-checking mart output against expectations | Traced to genuinely absent 2023 BRFSS data for KY and PA; changed the design to **name excluded states in the UI** |
| Deprecated Streamlit `use_container_width` | Headless `AppTest` render surfaced the warnings | Migrated to the current `width=` API |
| Non-contiguous DQ check IDs (DQ11/DQ12 skipped) | Reviewing ETL log output | Renumbered so the DQ table reads cleanly |
| Market sizing used **total** state population | Reviewing the proposed denominator against the source's own universe | BRFSS surveys adults only; switched to civilian 18+ population, which removed a ~22% overstatement |
| Census API returned "Missing Key" HTML with HTTP 200 | Probing the endpoint before building on it | The API now requires registration, which would have broken the keyless guarantee; switched to Census's static published CSVs |
| `TypeError: Invalid value ... for dtype 'float64'` when writing headcounts | ETL run after adding the population join | Nullable `Float64` was being assigned into a numpy `float64` column; kept the whole computation in the nullable dtype |

Validation was not assumed: the app was rendered headlessly with Streamlit's
`AppTest` harness to prove all five tabs build with zero exceptions, and the
ETL was run end-to-end against the live API *and* in `--offline` replay mode.

### 5. Reviewing and challenging AI output

Several AI-proposed approaches were rejected or reworked:

- **Raw-value averaging for the burden index**: rejected. Prevalence scales
  differ ~3× across indicators, so obesity would have swamped diabetes.
  Replaced with percentile ranking within indicator-year.
- **Dropping states with missing components from the score**: rejected. A state
  missing one input would be silently penalised. Replaced with weight
  re-normalisation over available components.
- **Treating all nulls as missing data**: rejected as factually wrong. CDC
  suppression is deliberate and footnoted; conflating it with parsing loss would
  have produced a misleading DQ report. Split into two separate checks (DQ08,
  DQ09).
- **Committing the generated data to git**: rejected. It would let a stale copy
  mask a broken pipeline and undermine the clone-and-run flow.

### 6. Using AI to improve quality, not only generate code

- Every data quality test **injects a specific defect and asserts the matching
  check catches it**. A check that cannot fail is not a check, so happy-path-only
  testing was explicitly ruled out.
- Extraction tests stub all HTTP, so the suite is deterministic and runs without
  network access.
- Edge cases were sought deliberately rather than waiting for them: CDC's
  null-ish placeholder tokens (`''`, `.`, `-`, `*`, `NA`), Socrata omitting
  all-null columns from its payload, thousands separators in numeric strings,
  protective-indicator orientation in equity gaps, and fast-fail on 4xx versus
  retry on 429/5xx.
- The README's limitations section was written honestly. It named the absence of
  population weighting as the single biggest gap in the analysis, and that
  admission is what drove the market-sizing work that later closed it; the
  remaining limitations are stated with the same candour.
