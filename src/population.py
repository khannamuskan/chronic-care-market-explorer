"""Committed reference data: adult population by state.

Separated from `extract.py` on purpose. Everything in `extract.py` is pulled
live from the CDC API and is expected to change between runs; this is a small,
slow-moving, version-controlled table whose whole value is that it *doesn't*
change without a visible commit.

Refresh it with `python scripts/refresh_population.py`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from . import config

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("location_id", "location_desc", "adult_population")


class PopulationReferenceError(RuntimeError):
    """The population reference is missing or unusable."""


def load_adult_population(path: Path | None = None) -> pd.DataFrame:
    """Load the committed adult-population table.

    The CSV carries `#` provenance headers so that anyone opening the file sees
    where the numbers came from without needing the README.
    """
    path = path or config.POPULATION_REFERENCE_PATH
    if not path.exists():
        raise PopulationReferenceError(
            f"Population reference not found at {path}. "
            "Run `python scripts/refresh_population.py` to rebuild it."
        )

    table = pd.read_csv(path, comment="#", dtype={"location_id": "string"})

    missing = [c for c in REQUIRED_COLUMNS if c not in table.columns]
    if missing:
        raise PopulationReferenceError(
            f"Population reference {path} is missing columns: {missing}"
        )

    table["location_id"] = table["location_id"].str.strip().str.zfill(2)
    table["adult_population"] = pd.to_numeric(
        table["adult_population"], errors="coerce"
    ).astype("Int64")

    if table["location_id"].duplicated().any():
        dupes = table.loc[table["location_id"].duplicated(), "location_id"].tolist()
        raise PopulationReferenceError(f"Duplicate location_id in reference: {dupes}")
    if table["adult_population"].isna().any():
        raise PopulationReferenceError("Non-numeric adult_population in reference.")

    logger.info(
        "population reference: %s states, %s adults (%s vintage)",
        len(table), f"{int(table['adult_population'].sum()):,}", config.POPULATION_YEAR,
    )
    return table[list(REQUIRED_COLUMNS)]


def attach(frame: pd.DataFrame, path: Path | None = None) -> pd.DataFrame:
    """Left-join adult population onto anything keyed by `location_id`.

    Left-joined deliberately: a geography with no population row (a territory,
    or the national rollup) keeps its rate-based analysis and simply has no
    headcount. Dropping those rows would quietly change the scope of the study.
    """
    pop = load_adult_population(path)[["location_id", "adult_population"]]
    return frame.merge(pop, on="location_id", how="left")
