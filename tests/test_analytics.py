"""Tests for the analytics layer -- the marts and the Opportunity Score."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import analytics, config


def test_indicator_mart_prefers_the_age_adjusted_measure(raw_row):
    from src import transform

    rows = [
        raw_row(datavaluetypeid="CRDPREV", datavaluetype="Crude Prevalence",
                datavalue="20.0", datavaluealt="20.0"),
        raw_row(datavaluetypeid="AGEADJPREV", datavaluetype="Age-adjusted Prevalence",
                datavalue="12.0", datavaluealt="12.0"),
    ]
    mart = analytics.build_indicator_state_year(transform.transform(rows))
    assert len(mart) == 1
    assert mart["measure_type_id"].iloc[0] == config.PREFERRED_MEASURE
    assert mart["value"].iloc[0] == pytest.approx(12.0)


def test_indicator_mart_falls_back_to_crude(raw_row):
    from src import transform

    rows = [raw_row(datavaluetypeid="CRDPREV", datavalue="20.0", datavaluealt="20.0")]
    mart = analytics.build_indicator_state_year(transform.transform(rows))
    assert mart["measure_type_id"].iloc[0] == config.FALLBACK_MEASURE


def test_indicator_mart_holds_only_the_overall_stratum(modelled):
    mart = analytics.build_indicator_state_year(modelled)
    # 2 states x 2 years x 2 indicators, overall only -- strata must not leak in.
    assert len(mart) == 8


def test_burden_index_is_a_relative_rank_not_a_raw_average(modelled):
    mart = analytics.build_indicator_state_year(modelled)
    burden = analytics.build_pillar_burden(mart)
    assert burden["burden_index"].between(0, 100).all()
    assert "All pillars" in set(burden["pillar"])


def test_burden_index_ranks_the_sicker_state_higher(modelled):
    mart = analytics.build_indicator_state_year(modelled)
    burden = analytics.build_pillar_burden(mart)
    latest = burden[(burden["year"] == 2023) & (burden["pillar"] == "All pillars")]
    ranked = latest.set_index("location_abbr")["burden_index"]
    # The fixture gives Illinois higher prevalence than California throughout.
    assert ranked["IL"] > ranked["CA"]


def test_equity_gap_computes_the_spread_between_strata(modelled):
    gaps = analytics.build_equity_gap(modelled)
    row = gaps[
        (gaps["location_abbr"] == "IL") & (gaps["year"] == 2023)
        & (gaps["indicator_id"] == "DIA01") & (gaps["stratum_category_id"] == "RACE")
    ].iloc[0]
    # Fixture: base 10.9, Black +4.0 -> 14.9, White -1.0 -> 9.9. Gap = 5.0pp.
    assert row["gap_pp"] == pytest.approx(5.0)
    assert row["most_burdened_group"] == "Black, non-Hispanic"
    assert row["n_strata"] == 2


def test_equity_gap_needs_at_least_two_groups(raw_row):
    from src import transform

    rows = [raw_row(stratificationcategoryid1="RACE",
                    stratificationcategory1="Race/Ethnicity",
                    stratificationid1="BLK", stratification1="Black, non-Hispanic")]
    assert analytics.build_equity_gap(transform.transform(rows)).empty


def test_equity_gap_orients_protective_indicators_correctly(raw_row):
    """For '% taking medication', the LOW group is the disadvantaged one."""
    from src import transform

    rows = []
    for sid, sname, val in (("BLK", "Black, non-Hispanic", "60.0"),
                            ("WHT", "White, non-Hispanic", "80.0")):
        rows.append(raw_row(
            questionid="CVD02", topicid="CVD", datavalue=val, datavaluealt=val,
            stratificationcategoryid1="RACE", stratificationcategory1="Race/Ethnicity",
            stratificationid1=sid, stratification1=sname,
        ))
    gaps = analytics.build_equity_gap(transform.transform(rows))
    row = gaps.iloc[0]
    assert row["polarity"] == "protective"
    assert row["most_burdened_group"] == "Black, non-Hispanic"


def test_percentile_rank_keeps_nulls_null():
    s = pd.Series([1.0, 2.0, np.nan, 4.0])
    ranked = analytics._percentile_rank(s)
    assert ranked.isna().sum() == 1
    assert ranked.max() == pytest.approx(100.0)


def test_slope_needs_enough_history():
    thin = pd.DataFrame({"year": [2022, 2023], "value": [1.0, 2.0]})
    assert np.isnan(analytics._slope_per_year(thin))

    rising = pd.DataFrame({"year": [2021, 2022, 2023], "value": [1.0, 2.0, 3.0]})
    assert analytics._slope_per_year(rising) == pytest.approx(1.0)


def _synthetic_scorecard() -> pd.DataFrame:
    return pd.DataFrame({
        "location_abbr": ["AA", "BB", "CC"],
        "location_name": ["Alpha", "Beta", "Gamma"],
        "census_region": ["South", "West", "Midwest"],
        "burden_index": [90.0, 50.0, 10.0],
        "burden_component": [100.0, 66.0, 33.0],
        "trend_component": [33.0, 66.0, 100.0],
        "equity_component": [100.0, 66.0, 33.0],
        "care_gap_component": [100.0, 66.0, 33.0],
    })


def test_rescoring_changes_the_ranking_as_weights_change():
    base = _synthetic_scorecard()

    burden_led = analytics.rescore(base, {"burden": 1.0, "trend": 0.0,
                                          "equity_gap": 0.0, "care_gap": 0.0})
    assert burden_led.iloc[0]["location_abbr"] == "AA"

    trend_led = analytics.rescore(base, {"burden": 0.0, "trend": 1.0,
                                         "equity_gap": 0.0, "care_gap": 0.0})
    assert trend_led.iloc[0]["location_abbr"] == "CC"


def test_rescoring_renormalises_around_a_missing_component():
    """A state missing one input must not be penalised to zero for it."""
    base = _synthetic_scorecard()
    base.loc[0, "care_gap_component"] = np.nan
    scored = analytics.rescore(base, config.SCORE_WEIGHTS)
    alpha = scored[scored["location_abbr"] == "AA"].iloc[0]
    # Alpha scores 100/33/100 on its three available components; the weighted
    # mean over those must stay well above the 66.6 a zero-fill would produce.
    assert alpha["opportunity_score"] > 80
    assert scored["opportunity_score"].between(0, 100).all()


def test_scores_stay_within_bounds_and_rank_is_dense():
    scored = analytics.rescore(_synthetic_scorecard(), config.SCORE_WEIGHTS)
    assert scored["opportunity_score"].between(0, 100).all()
    assert scored["rank"].tolist() == [1, 2, 3]


def test_build_all_reports_states_missing_from_the_scoring_year(modelled):
    """A state present historically but absent in the latest year must be named."""
    fact = modelled.fact
    trimmed = fact[~((fact["year"] == 2023) & (fact["location_id"] == "06"))]
    from src import transform as t

    result = t.TransformResult(trimmed, modelled.dim_location,
                               modelled.dim_indicator, modelled.dim_stratum,
                               modelled.stats)
    _marts, stats = analytics.build_all(result)
    assert stats["latest_year"] == 2023
    assert "CA" in stats["states_excluded_from_scorecard"]
