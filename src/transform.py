"""Transformation layer: raw CDI JSON records -> a small analytical star schema.

Model
-----
    fact_indicator  (grain: year x location x indicator x measure type x stratum)
        |-- dim_location    (location_id)
        |-- dim_indicator   (indicator_id)
        `-- dim_stratum     (stratum_key)

The CDI feed is a tall "one row per estimate" file with 30+ mostly-redundant
columns. Splitting it into conformed dimensions keeps the fact narrow, makes
the app's filters cheap, and gives us referential-integrity checks to run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import config

logger = logging.getLogger(__name__)

# Raw column -> model column. Anything not listed is deliberately dropped.
COLUMN_MAP: dict[str, str] = {
    "yearstart": "year_start",
    "yearend": "year_end",
    "locationid": "location_id",
    "locationabbr": "location_abbr",
    "locationdesc": "location_name",
    "questionid": "indicator_id",
    "question": "question",
    "topicid": "topic_id",
    "topic": "topic",
    "datavaluetypeid": "measure_type_id",
    "datavaluetype": "measure_type",
    "datavalueunit": "unit",
    "datavaluealt": "value",
    "lowconfidencelimit": "ci_low",
    "highconfidencelimit": "ci_high",
    "datavaluefootnotesymbol": "footnote_symbol",
    "datavaluefootnote": "footnote",
    "stratificationcategoryid1": "stratum_category_id",
    "stratificationcategory1": "stratum_category",
    "stratificationid1": "stratum_id",
    "stratification1": "stratum",
    "datasource": "data_source",
}

FACT_NATURAL_KEY = [
    "year_start",
    "location_id",
    "indicator_id",
    "measure_type_id",
    "stratum_category_id",
    "stratum_id",
]

NUMERIC_COLUMNS = ["value", "ci_low", "ci_high"]

INDICATOR_LOOKUP = {i.indicator_id: i for i in config.INDICATORS}


class TransformResult:
    """Container for the modelled tables plus lineage counters."""

    def __init__(
        self,
        fact: pd.DataFrame,
        dim_location: pd.DataFrame,
        dim_indicator: pd.DataFrame,
        dim_stratum: pd.DataFrame,
        stats: dict[str, Any],
    ) -> None:
        self.fact = fact
        self.dim_location = dim_location
        self.dim_indicator = dim_indicator
        self.dim_stratum = dim_stratum
        self.stats = stats

    def tables(self) -> dict[str, pd.DataFrame]:
        return {
            "fact_indicator": self.fact,
            "dim_location": self.dim_location,
            "dim_indicator": self.dim_indicator,
            "dim_stratum": self.dim_stratum,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_numeric(series: pd.Series) -> pd.Series:
    """Coerce CDI's string numerics, treating CDC's placeholder tokens as null."""
    cleaned = (
        series.astype("string")
        .str.strip()
        .str.replace(",", "", regex=False)
        .replace({"": pd.NA, ".": pd.NA, "-": pd.NA, "*": pd.NA, "NA": pd.NA})
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _extract_geo(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Pull lat/lon out of the nested GeoJSON point CDC embeds per row."""
    records = []
    for row in rows:
        geo = row.get("geolocation") or {}
        coords = geo.get("coordinates") if isinstance(geo, dict) else None
        lon, lat = (coords + [None, None])[:2] if isinstance(coords, list) else (None, None)
        records.append(
            {"location_id": str(row.get("locationid", "")).strip(), "longitude": lon, "latitude": lat}
        )
    geo_df = pd.DataFrame.from_records(records)
    geo_df = geo_df.dropna(subset=["location_id"])
    return geo_df.dropna(subset=["latitude"]).drop_duplicates(subset=["location_id"])


def _geo_level(abbr: str) -> str:
    if abbr == config.NATIONAL_ABBR:
        return "National"
    if abbr in config.TERRITORY_ABBRS:
        return "Territory"
    return "State"


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------
def build_dim_location(df: pd.DataFrame, geo: pd.DataFrame) -> pd.DataFrame:
    dim = (
        df[["location_id", "location_abbr", "location_name"]]
        .drop_duplicates(subset=["location_id"])
        .sort_values("location_id")
        .reset_index(drop=True)
    )
    dim["geo_level"] = dim["location_abbr"].map(_geo_level)
    dim["census_region"] = dim["location_abbr"].map(config.CENSUS_REGION).fillna("Other")
    dim["is_addressable_market"] = dim["geo_level"].eq("State")
    return dim.merge(geo, on="location_id", how="left")


def build_dim_indicator(df: pd.DataFrame) -> pd.DataFrame:
    dim = (
        df[["indicator_id", "topic_id", "topic", "question", "unit"]]
        .drop_duplicates(subset=["indicator_id"])
        .sort_values("indicator_id")
        .reset_index(drop=True)
    )
    dim["pillar"] = dim["indicator_id"].map(lambda k: INDICATOR_LOOKUP[k].pillar)
    dim["short_label"] = dim["indicator_id"].map(lambda k: INDICATOR_LOOKUP[k].short_label)
    dim["polarity"] = dim["indicator_id"].map(lambda k: INDICATOR_LOOKUP[k].polarity)
    dim["measure_role"] = dim["indicator_id"].map(lambda k: INDICATOR_LOOKUP[k].measure_role)
    return dim


def build_dim_stratum(df: pd.DataFrame) -> pd.DataFrame:
    dim = (
        df[["stratum_category_id", "stratum_category", "stratum_id", "stratum"]]
        .drop_duplicates()
        .sort_values(["stratum_category_id", "stratum_id"])
        .reset_index(drop=True)
    )
    dim["stratum_key"] = dim["stratum_category_id"] + "|" + dim["stratum_id"]
    dim["is_overall"] = dim["stratum_id"].eq(config.OVERALL_STRATUM_ID)
    return dim[["stratum_key", "stratum_category_id", "stratum_category", "stratum_id", "stratum", "is_overall"]]


# ---------------------------------------------------------------------------
# Main transform
# ---------------------------------------------------------------------------
def transform(rows: list[dict[str, Any]]) -> TransformResult:
    stats: dict[str, Any] = {"rows_in": len(rows)}
    loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    raw = pd.DataFrame.from_records(rows)
    missing_cols = [c for c in COLUMN_MAP if c not in raw.columns]
    # Socrata omits null columns entirely; re-create them so downstream code
    # can assume a stable schema instead of guarding every access.
    for col in missing_cols:
        raw[col] = pd.NA
    stats["columns_absent_in_payload"] = missing_cols

    geo = _extract_geo(rows)
    df = raw[list(COLUMN_MAP)].rename(columns=COLUMN_MAP)

    # --- type normalisation ------------------------------------------------
    for col in df.columns:
        if col not in NUMERIC_COLUMNS:
            df[col] = df[col].astype("string").str.strip()
    for col in NUMERIC_COLUMNS:
        df[col] = _to_numeric(df[col])
    df["year_start"] = pd.to_numeric(df["year_start"], errors="coerce").astype("Int64")
    df["year_end"] = pd.to_numeric(df["year_end"], errors="coerce").astype("Int64")

    # --- scope filters -----------------------------------------------------
    before = len(df)
    df = df[df["indicator_id"].isin(config.INDICATOR_IDS)]
    df = df[df["measure_type_id"].isin(config.MEASURE_TYPES)]
    stats["rows_dropped_out_of_scope_indicator"] = before - len(df)

    before = len(df)
    unexpected = sorted(
        set(df["stratum_category_id"].dropna().unique()) - set(config.ALLOWED_STRATUM_CATEGORIES)
    )
    df = df[df["stratum_category_id"].isin(config.ALLOWED_STRATUM_CATEGORIES)]
    stats["unexpected_stratum_categories"] = unexpected
    stats["rows_dropped_unsupported_stratum"] = before - len(df)

    before = len(df)
    df = df.dropna(subset=["year_start", "location_id", "indicator_id"])
    stats["rows_dropped_null_keys"] = before - len(df)

    # --- derived fields ----------------------------------------------------
    df["stratum_key"] = df["stratum_category_id"] + "|" + df["stratum_id"]
    df["year"] = df["year_start"].astype("Int64")
    df["is_multi_year"] = df["year_end"].fillna(df["year_start"]) != df["year_start"]

    # CDC blanks the estimate and stamps a footnote when a cell is statistically
    # unreliable. That is suppression, not a broken pipeline -- label it so the
    # DQ layer can separate "CDC withheld this" from "we lost this".
    has_footnote = df["footnote_symbol"].notna() & df["footnote_symbol"].ne("")
    df["is_suppressed"] = df["value"].isna() & has_footnote
    df["is_missing_unexplained"] = df["value"].isna() & ~has_footnote
    df["ci_width"] = df["ci_high"] - df["ci_low"]
    # Wide intervals mean a small denominator; the app greys these out.
    df["is_low_precision"] = df["ci_width"] > 10.0

    # --- de-duplication ----------------------------------------------------
    dup_mask = df.duplicated(subset=FACT_NATURAL_KEY, keep=False)
    stats["duplicate_rows_detected"] = int(dup_mask.sum())
    if dup_mask.any():
        logger.warning(
            "%s rows share a natural key; keeping the first non-null estimate per key",
            int(dup_mask.sum()),
        )
        # Prefer a populated estimate over a suppressed one when collapsing.
        df = (
            df.sort_values(FACT_NATURAL_KEY + ["value"], na_position="last")
            .drop_duplicates(subset=FACT_NATURAL_KEY, keep="first")
        )
    stats["rows_after_dedup"] = len(df)

    # --- dimensions + fact -------------------------------------------------
    dim_location = build_dim_location(df, geo)
    dim_indicator = build_dim_indicator(df)
    dim_stratum = build_dim_stratum(df)

    fact = df[
        [
            "year", "year_start", "year_end", "location_id", "indicator_id",
            "measure_type_id", "measure_type", "stratum_key", "value",
            "ci_low", "ci_high", "ci_width", "is_suppressed",
            "is_missing_unexplained", "is_low_precision", "is_multi_year",
            "footnote_symbol", "footnote", "data_source",
        ]
    ].reset_index(drop=True)
    fact["loaded_at"] = loaded_at

    stats["rows_out"] = len(fact)
    stats["year_min"] = int(fact["year"].min())
    stats["year_max"] = int(fact["year"].max())
    stats["suppressed_rows"] = int(fact["is_suppressed"].sum())
    stats["unexplained_missing_rows"] = int(fact["is_missing_unexplained"].sum())
    logger.info(
        "Transformed %s -> %s fact rows across %s-%s",
        stats["rows_in"], stats["rows_out"], stats["year_min"], stats["year_max"],
    )

    return TransformResult(fact, dim_location, dim_indicator, dim_stratum, stats)
