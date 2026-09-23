"""Analytics layer: turn the star schema into decision-ready marts.

Marts produced
--------------
``mart_indicator_state_year``  one resolved prevalence per state/indicator/year
``mart_pillar_burden``         normalised 0-100 burden index per state/pillar/year
``mart_equity_gap``            within-state disparity across demographic strata
``mart_state_scorecard``       latest-year ranking with the Opportunity Score

The Opportunity Score is the product's point of view: a market is attractive to
a digital chronic-care company when disease burden is high, the trend is
worsening, the burden is unevenly distributed (unmet need concentrated in
specific groups), and diagnosed patients are not on treatment.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from . import config
from .transform import TransformResult

logger = logging.getLogger(__name__)

OVERALL_KEY = f"OVERALL|{config.OVERALL_STRATUM_ID}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _percentile_rank(s: pd.Series) -> pd.Series:
    """0-100 percentile rank; NaNs stay NaN so they cannot fake a good score."""
    if s.notna().sum() <= 1:
        return pd.Series(np.where(s.notna(), 50.0, np.nan), index=s.index)
    return s.rank(pct=True, na_option="keep") * 100.0


def _slope_per_year(frame: pd.DataFrame, x: str = "year", y: str = "value") -> float:
    """OLS slope in units-per-year. NaN when there is too little history."""
    d = frame[[x, y]].dropna()
    if len(d) < 3 or d[x].nunique() < 3:
        return float("nan")
    return float(np.polyfit(d[x].astype(float), d[y].astype(float), 1)[0])


# ---------------------------------------------------------------------------
# Mart 1: resolved indicator values
# ---------------------------------------------------------------------------
def build_indicator_state_year(result: TransformResult) -> pd.DataFrame:
    """One value per state/indicator/year, preferring the age-adjusted measure.

    State age structures differ enough (Florida vs Utah) that crude prevalence
    is misleading for cross-state comparison. We take age-adjusted where CDC
    publishes it and fall back to crude, recording which was used.
    """
    fact = result.fact
    scoped = fact[(fact["stratum_key"] == OVERALL_KEY) & fact["value"].notna()].copy()

    priority = {config.PREFERRED_MEASURE: 0, config.FALLBACK_MEASURE: 1}
    scoped["_priority"] = scoped["measure_type_id"].map(priority).fillna(9)
    resolved = (
        scoped.sort_values(["year", "location_id", "indicator_id", "_priority"])
        .drop_duplicates(subset=["year", "location_id", "indicator_id"], keep="first")
        .drop(columns="_priority")
    )

    mart = (
        resolved.merge(
            result.dim_location[
                ["location_id", "location_abbr", "location_name", "geo_level",
                 "census_region", "is_addressable_market", "latitude", "longitude"]
            ],
            on="location_id", how="left",
        )
        .merge(
            result.dim_indicator[
                ["indicator_id", "pillar", "short_label", "polarity", "measure_role", "question"]
            ],
            on="indicator_id", how="left",
        )
    )

    # National benchmark so every state row knows how it compares.
    national = (
        mart[mart["geo_level"] == "National"][["year", "indicator_id", "value"]]
        .rename(columns={"value": "national_value"})
    )
    if national.empty:
        # CDI does not always publish a US rollup; use the state median instead.
        national = (
            mart[mart["is_addressable_market"]]
            .groupby(["year", "indicator_id"])["value"].median()
            .reset_index(name="national_value")
        )
        logger.info("No national rows in feed; using state median as the benchmark.")

    mart = mart.merge(national, on=["year", "indicator_id"], how="left")
    mart["delta_vs_national"] = mart["value"] - mart["national_value"]

    states = mart["is_addressable_market"].fillna(False)
    mart["state_percentile"] = np.nan
    mart.loc[states, "state_percentile"] = (
        mart[states].groupby(["year", "indicator_id"])["value"].transform(_percentile_rank)
    )

    keep = [
        "year", "location_id", "location_abbr", "location_name", "geo_level",
        "census_region", "is_addressable_market", "latitude", "longitude",
        "indicator_id", "pillar", "short_label", "polarity", "measure_role",
        "question", "measure_type_id", "measure_type", "value", "ci_low",
        "ci_high", "ci_width", "is_low_precision", "national_value",
        "delta_vs_national", "state_percentile",
    ]
    out = mart[keep].sort_values(["year", "location_abbr", "indicator_id"]).reset_index(drop=True)
    logger.info("mart_indicator_state_year: %s rows", len(out))
    return out


# ---------------------------------------------------------------------------
# Mart 2: pillar burden index
# ---------------------------------------------------------------------------
def build_pillar_burden(indicator_mart: pd.DataFrame) -> pd.DataFrame:
    """Normalise each burden indicator to 0-100 within its year, then average.

    Raw prevalences are not comparable across indicators (diabetes ~11%,
    obesity ~35%), so averaging them directly would let obesity dominate the
    index. Percentile-ranking within indicator-year puts every indicator on the
    same footing and makes the index a relative market-attractiveness measure.
    """
    burden = indicator_mart[
        indicator_mart["is_addressable_market"].fillna(False)
        & indicator_mart["measure_role"].eq("burden")
        & indicator_mart["value"].notna()
    ].copy()

    burden["indicator_score"] = burden.groupby(["year", "indicator_id"])["value"].transform(
        _percentile_rank
    )
    # Protective indicators would need inverting; none are tagged "burden" today,
    # but guard anyway so adding one later cannot silently flip the index.
    protective = burden["polarity"].eq("protective")
    burden.loc[protective, "indicator_score"] = 100.0 - burden.loc[protective, "indicator_score"]

    pillar = (
        burden.groupby(["year", "location_id", "location_abbr", "location_name",
                        "census_region", "pillar"])
        .agg(
            burden_index=("indicator_score", "mean"),
            mean_prevalence=("value", "mean"),
            n_indicators=("indicator_id", "nunique"),
        )
        .reset_index()
    )

    overall = (
        burden.groupby(["year", "location_id", "location_abbr", "location_name", "census_region"])
        .agg(
            burden_index=("indicator_score", "mean"),
            mean_prevalence=("value", "mean"),
            n_indicators=("indicator_id", "nunique"),
        )
        .reset_index()
        .assign(pillar="All pillars")
    )

    out = pd.concat([pillar, overall], ignore_index=True)
    out["burden_index"] = out["burden_index"].round(2)
    out = out.sort_values(["year", "pillar", "burden_index"], ascending=[True, True, False])
    logger.info("mart_pillar_burden: %s rows", len(out))
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Mart 3: equity gap
# ---------------------------------------------------------------------------
def build_equity_gap(result: TransformResult) -> pd.DataFrame:
    """Within a state-year-indicator, how far apart are demographic groups?

    Reported in percentage points (absolute gap) and as a ratio. A state with
    an average prevalence but a 12-point race gap is a different -- often
    better -- commercial and clinical target than a uniformly average state.
    """
    fact = result.fact
    strat = result.dim_stratum[~result.dim_stratum["is_overall"]]

    scoped = (
        fact[fact["value"].notna()]
        .merge(strat, on="stratum_key", how="inner")
        .merge(
            result.dim_location[["location_id", "location_abbr", "location_name",
                                 "geo_level", "census_region", "is_addressable_market"]],
            on="location_id", how="left",
        )
        .merge(
            result.dim_indicator[["indicator_id", "pillar", "short_label", "polarity"]],
            on="indicator_id", how="left",
        )
    )
    # Use one measure type per group so a gap is never crude-vs-age-adjusted.
    priority = {config.PREFERRED_MEASURE: 0, config.FALLBACK_MEASURE: 1}
    scoped = scoped.reset_index(drop=True)
    scoped["_p"] = scoped["measure_type_id"].map(priority).fillna(9)
    best = scoped.groupby(
        ["year", "location_id", "indicator_id", "stratum_category_id"]
    )["_p"].transform("min")
    scoped = scoped[scoped["_p"] == best]

    group_cols = [
        "year", "location_id", "location_abbr", "location_name", "geo_level",
        "census_region", "is_addressable_market", "indicator_id", "pillar",
        "short_label", "polarity", "stratum_category_id", "stratum_category",
    ]

    agg = scoped.groupby(group_cols).agg(
        n_strata=("value", "size"),
        min_value=("value", "min"),
        max_value=("value", "max"),
        mean_value=("value", "mean"),
    ).reset_index()

    # A "gap" needs at least two groups to compare.
    agg = agg[agg["n_strata"] >= 2].copy()

    idx_max = scoped.groupby(group_cols)["value"].idxmax()
    idx_min = scoped.groupby(group_cols)["value"].idxmin()
    highest = scoped.loc[idx_max, group_cols + ["stratum"]].rename(columns={"stratum": "highest_group"})
    lowest = scoped.loc[idx_min, group_cols + ["stratum"]].rename(columns={"stratum": "lowest_group"})

    out = agg.merge(highest, on=group_cols, how="left").merge(lowest, on=group_cols, how="left")
    out["gap_pp"] = (out["max_value"] - out["min_value"]).round(2)
    out["gap_ratio"] = np.where(
        out["min_value"] > 0, (out["max_value"] / out["min_value"]).round(2), np.nan
    )
    # For a protective indicator the disadvantaged group is the LOW one.
    out["most_burdened_group"] = np.where(
        out["polarity"].eq("risk"), out["highest_group"], out["lowest_group"]
    )
    out = out.sort_values(["year", "location_abbr", "indicator_id", "stratum_category_id"])
    logger.info("mart_equity_gap: %s rows", len(out))
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Mart 4: state scorecard
# ---------------------------------------------------------------------------
def build_state_scorecard(
    indicator_mart: pd.DataFrame,
    pillar_burden: pd.DataFrame,
    equity_gap: pd.DataFrame,
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Rank states for the latest year with a transparent, weighted score."""
    weights = weights or config.SCORE_WEIGHTS
    states = pillar_burden[pillar_burden["pillar"] == "All pillars"]
    if states.empty:
        return pd.DataFrame()

    latest_year = int(states["year"].max())
    base = states[states["year"] == latest_year].copy()

    # --- trend: slope of the burden index over the recent window -----------
    window_start = latest_year - config.TREND_WINDOW_YEARS + 1
    hist = states[states["year"].between(window_start, latest_year)]
    trend = (
        hist.groupby("location_id")
        .apply(lambda g: _slope_per_year(g.rename(columns={"burden_index": "value"})),
               include_groups=False)
        .reset_index(name="burden_trend_per_year")
    )

    # Prevalence trend in real percentage points is what a stakeholder reads.
    prev_hist = indicator_mart[
        indicator_mart["is_addressable_market"].fillna(False)
        & indicator_mart["measure_role"].eq("burden")
        & indicator_mart["year"].between(window_start, latest_year)
    ]
    prev_trend = (
        prev_hist.groupby(["location_id", "indicator_id"])
        .apply(_slope_per_year, include_groups=False)
        .reset_index(name="slope")
        .groupby("location_id")["slope"].mean()
        .reset_index(name="prevalence_trend_pp_per_year")
    )

    # --- equity: mean race/ethnicity gap across burden indicators ----------
    eq = equity_gap[
        equity_gap["is_addressable_market"].fillna(False)
        & equity_gap["year"].eq(latest_year)
        & equity_gap["stratum_category_id"].eq("RACE")
    ]
    equity = eq.groupby("location_id")["gap_pp"].mean().reset_index(name="mean_race_gap_pp")

    # --- care gap: diagnosed patients not on medication --------------------
    care = indicator_mart[
        indicator_mart["is_addressable_market"].fillna(False)
        & indicator_mart["measure_role"].eq("care_gap")
        & indicator_mart["year"].eq(latest_year)
    ].copy()
    # These indicators are protective (% taking medicine), so untreated = 100 - value.
    care["untreated_pct"] = 100.0 - care["value"]
    care_gap = care.groupby("location_id")["untreated_pct"].mean().reset_index(
        name="untreated_pct"
    )

    score = (
        base.drop(columns=["pillar"])
        .merge(trend, on="location_id", how="left")
        .merge(prev_trend, on="location_id", how="left")
        .merge(equity, on="location_id", how="left")
        .merge(care_gap, on="location_id", how="left")
    )

    # --- component percentile ranks (all oriented "higher = more opportunity")
    score["burden_component"] = _percentile_rank(score["burden_index"])
    score["trend_component"] = _percentile_rank(score["burden_trend_per_year"])
    score["equity_component"] = _percentile_rank(score["mean_race_gap_pp"])
    score["care_gap_component"] = _percentile_rank(score["untreated_pct"])

    components = {
        "burden": "burden_component",
        "trend": "trend_component",
        "equity_gap": "equity_component",
        "care_gap": "care_gap_component",
    }
    # Re-normalise weights over whichever components actually have data for a
    # state, so a state with one missing input is not silently penalised.
    weighted = pd.DataFrame(index=score.index)
    weight_mass = pd.Series(0.0, index=score.index)
    for name, col in components.items():
        w = float(weights.get(name, 0.0))
        present = score[col].notna()
        weighted[col] = score[col].fillna(0.0) * w * present
        weight_mass += w * present
    score["opportunity_score"] = (weighted.sum(axis=1) / weight_mass.replace(0, np.nan)).round(1)
    score["score_components_available"] = sum(score[c].notna() for c in components.values())

    score = score.sort_values("opportunity_score", ascending=False).reset_index(drop=True)
    score["rank"] = score["opportunity_score"].rank(ascending=False, method="min").astype("Int64")
    score["year"] = latest_year
    logger.info("mart_state_scorecard: %s states for %s", len(score), latest_year)
    return score


SCORE_COMPONENTS: dict[str, str] = {
    "burden": "burden_component",
    "trend": "trend_component",
    "equity_gap": "equity_component",
    "care_gap": "care_gap_component",
}


def rescore(scorecard: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    """Recompute the Opportunity Score under different weights.

    The component percentile ranks are persisted, so re-weighting is a cheap
    reshuffle -- this is what lets the app expose the weights as sliders and
    keep the scoring logic honest and inspectable rather than a black box.
    """
    out = scorecard.copy()
    if out.empty:
        return out
    weighted = pd.DataFrame(index=out.index)
    weight_mass = pd.Series(0.0, index=out.index)
    for name, col in SCORE_COMPONENTS.items():
        w = float(weights.get(name, 0.0))
        present = out[col].notna()
        weighted[col] = out[col].fillna(0.0) * w * present
        weight_mass += w * present
    out["opportunity_score"] = (weighted.sum(axis=1) / weight_mass.replace(0, np.nan)).round(1)
    out = out.sort_values("opportunity_score", ascending=False).reset_index(drop=True)
    out["rank"] = out["opportunity_score"].rank(ascending=False, method="min").astype("Int64")
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_all(result: TransformResult) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    indicator_mart = build_indicator_state_year(result)
    pillar_burden = build_pillar_burden(indicator_mart)
    equity_gap = build_equity_gap(result)
    scorecard = build_state_scorecard(indicator_mart, pillar_burden, equity_gap)

    marts = {
        "mart_indicator_state_year": indicator_mart,
        "mart_pillar_burden": pillar_burden,
        "mart_equity_gap": equity_gap,
        "mart_state_scorecard": scorecard,
    }
    stats = {name: len(df) for name, df in marts.items()}
    latest_year = int(indicator_mart["year"].max())
    stats["latest_year"] = latest_year
    stats["states_scored"] = int(len(scorecard))

    # States that exist in the feed but have no data in the scoring year are
    # excluded from the ranking. BRFSS participation is voluntary per state and
    # per module, so this is a real source gap rather than a pipeline defect --
    # name the states so it is visible instead of silently shrinking the league
    # table.
    all_states = set(
        indicator_mart.loc[indicator_mart["is_addressable_market"].fillna(False), "location_abbr"]
    )
    scored = set(scorecard["location_abbr"]) if not scorecard.empty else set()
    stats["states_excluded_from_scorecard"] = sorted(all_states - scored)
    if stats["states_excluded_from_scorecard"]:
        logger.warning(
            "No %s data for %s -- excluded from the scorecard.",
            latest_year, ", ".join(stats["states_excluded_from_scorecard"]),
        )
    return marts, stats
