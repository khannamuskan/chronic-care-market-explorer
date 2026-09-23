"""Regenerate the committed state adult-population reference table.

    python scripts/refresh_population.py

Why this is a script and not a stage of `etl.py`
------------------------------------------------
The Census *API* now requires a registered key, which would break the
"clone it and run it, no credentials" guarantee this project is built around.
Census also publishes the same estimates as static, keyless CSV files, so the
reference table is refreshed **deliberately and rarely** by running this
script, and the resulting snapshot is committed to the repository.

That preserves two properties that matter:

* `python etl.py` keeps exactly one network dependency (the CDC API), so there
  is one fewer way for a reviewer's run to fail.
* The denominator behind every published headcount is version-controlled and
  reviewable in a diff, instead of shifting silently between runs.

Source
------
U.S. Census Bureau, Population Estimates Program, Vintage 2024:
"State Characteristics: Civilian Population by Single Year of Age and Sex".
Public domain.
"""

from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import config  # noqa: E402

SOURCE_URL = (
    "https://www2.census.gov/programs-surveys/popest/datasets/"
    "2020-2024/state/asrh/sc-est2024-agesex-civ.csv"
)

# BRFSS interviews adults, so the denominator has to be adults too. Using total
# resident population would inflate every prevalence-derived headcount by the
# roughly 22% of the population that was never eligible to be surveyed.
POP_COLUMN = f"POPEST{config.POPULATION_YEAR}_CIV"

STATE_SUMMARY_LEVEL = 40  # 040 = state; 010 is the national rollup, dropped
BOTH_SEXES = 0
AGE_TOTAL_SENTINEL = 999  # AGE=999 is an all-ages total row, not an age
ADULT_MIN_AGE = 18

# Sanity band for the national 18+ civilian total. Wide enough to survive a
# vintage change, tight enough to catch a broken filter or a column shift.
NATIONAL_TOTAL_MIN = 230_000_000
NATIONAL_TOTAL_MAX = 290_000_000


def fetch_source() -> pd.DataFrame:
    response = requests.get(
        SOURCE_URL,
        headers={"User-Agent": config.API_USER_AGENT},
        timeout=300,
    )
    response.raise_for_status()
    return pd.read_csv(io.StringIO(response.text))


def summarise_adults(raw: pd.DataFrame) -> pd.DataFrame:
    if POP_COLUMN not in raw.columns:
        available = [c for c in raw.columns if c.startswith("POPEST")]
        raise SystemExit(
            f"Census file has no column {POP_COLUMN!r}; available: {available}. "
            "The vintage has probably rolled forward -- update SOURCE_URL and "
            "config.POPULATION_YEAR together."
        )

    adults = raw[
        (raw["SUMLEV"] == STATE_SUMMARY_LEVEL)
        & (raw["SEX"] == BOTH_SEXES)
        & (raw["AGE"] != AGE_TOTAL_SENTINEL)
        & (raw["AGE"] >= ADULT_MIN_AGE)
    ]

    out = (
        adults.groupby(["STATE", "NAME"], as_index=False)[POP_COLUMN]
        .sum()
        .rename(columns={POP_COLUMN: "adult_population", "NAME": "location_desc"})
    )
    # CDC keys locations by zero-padded state FIPS; Census stores it as an int.
    out["location_id"] = out["STATE"].astype(int).astype(str).str.zfill(2)
    out["adult_population"] = out["adult_population"].astype(int)
    return (
        out[["location_id", "location_desc", "adult_population"]]
        .sort_values("location_id")
        .reset_index(drop=True)
    )


def validate(table: pd.DataFrame) -> None:
    """Fail loudly rather than commit a silently wrong denominator."""
    problems = []
    if len(table) != config.N_ADDRESSABLE_STATES:
        problems.append(
            f"expected {config.N_ADDRESSABLE_STATES} rows (50 states + DC), got {len(table)}"
        )
    if table["location_id"].duplicated().any():
        problems.append("duplicate location_id values")
    if (table["adult_population"] <= 0).any():
        problems.append("non-positive adult_population values")
    total = int(table["adult_population"].sum())
    if not NATIONAL_TOTAL_MIN <= total <= NATIONAL_TOTAL_MAX:
        problems.append(f"implausible national adult total: {total:,}")
    if problems:
        raise SystemExit("Refusing to write reference table -- " + "; ".join(problems))


def write_table(table: pd.DataFrame) -> Path:
    path = config.POPULATION_REFERENCE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        fh.write(f"# Adult (18+) civilian population by state, {config.POPULATION_YEAR}\n")
        fh.write(f"# source: {config.POPULATION_SOURCE}\n")
        fh.write(f"# url: {SOURCE_URL}\n")
        fh.write(f"# derivation: {POP_COLUMN} summed over AGE >= {ADULT_MIN_AGE}, SEX = 0\n")
        fh.write(f"# generated: {date.today().isoformat()} by scripts/refresh_population.py\n")
        table.to_csv(fh, index=False, lineterminator="\n")
    return path


def main() -> int:
    print(f"Fetching {SOURCE_URL} ...")
    raw = fetch_source()
    print(f"  {len(raw):,} source rows")
    table = summarise_adults(raw)
    validate(table)
    path = write_table(table)
    print(
        f"Wrote {path.relative_to(PROJECT_ROOT)} -- {len(table)} states, "
        f"{table['adult_population'].sum():,} adults"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
