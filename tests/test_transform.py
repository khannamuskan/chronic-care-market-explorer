"""Tests for the transformation layer."""

from __future__ import annotations

import pandas as pd
import pytest

from src import config, transform


def test_grain_is_unique(modelled):
    fact = modelled.fact
    keys = ["year", "location_id", "indicator_id", "measure_type_id", "stratum_key"]
    assert not fact.duplicated(subset=keys).any()


def test_dimensions_cover_every_fact_key(modelled):
    fact = modelled.fact
    assert set(fact["location_id"]) <= set(modelled.dim_location["location_id"])
    assert set(fact["indicator_id"]) <= set(modelled.dim_indicator["indicator_id"])
    assert set(fact["stratum_key"]) <= set(modelled.dim_stratum["stratum_key"])


def test_numeric_coercion_of_cdc_placeholders(raw_row):
    """CDC uses '', '.', '-' and '*' as null-ish tokens; none may become 0."""
    rows = [
        raw_row(datavaluealt=token, datavalue=token, locationid=str(i))
        for i, token in enumerate(["", ".", "-", "*", "NA"], start=90)
    ]
    result = transform.transform(rows)
    assert result.fact["value"].isna().all()
    assert (result.fact["value"] == 0).sum() == 0


def test_thousands_separator_is_parsed(raw_row):
    rows = [raw_row(datavaluealt="1,234.5", datavalue="1,234.5")]
    result = transform.transform(rows)
    assert result.fact["value"].iloc[0] == pytest.approx(1234.5)


def test_suppression_is_distinguished_from_unexplained_loss(raw_row):
    """A null WITH a footnote is CDC suppression; a null WITHOUT one is our bug."""
    rows = [
        raw_row(locationid="01", datavalue=None, datavaluealt=None,
                datavaluefootnotesymbol="~",
                datavaluefootnote="Statistically unreliable"),
        raw_row(locationid="02", datavalue=None, datavaluealt=None),
    ]
    result = transform.transform(rows)
    suppressed = result.fact.set_index("location_id")["is_suppressed"]
    unexplained = result.fact.set_index("location_id")["is_missing_unexplained"]
    assert suppressed["01"] and not unexplained["01"]
    assert unexplained["02"] and not suppressed["02"]


def test_out_of_scope_indicators_are_dropped(raw_row):
    rows = [raw_row(), raw_row(questionid="ALC01", topicid="ALC", locationid="02")]
    result = transform.transform(rows)
    assert set(result.fact["indicator_id"]) == {"DIA01"}
    assert result.stats["rows_dropped_out_of_scope_indicator"] == 1


def test_unsupported_strata_are_dropped_and_reported(raw_row):
    """Youth GRADE bands exist in the feed but are out of scope for adult care."""
    rows = [
        raw_row(),
        raw_row(locationid="02", stratificationcategoryid1="GRADE",
                stratificationcategory1="Grade", stratificationid1="GRD9",
                stratification1="Grade 9"),
    ]
    result = transform.transform(rows)
    assert "GRADE" in result.stats["unexpected_stratum_categories"]
    assert result.stats["rows_dropped_unsupported_stratum"] == 1
    assert "GRADE" not in set(result.fact["stratum_key"].str.split("|").str[0])


def test_duplicates_collapse_preferring_a_populated_estimate(raw_row):
    rows = [
        raw_row(datavalue=None, datavaluealt=None, datavaluefootnotesymbol="~"),
        raw_row(datavalue="11.5", datavaluealt="11.5"),
    ]
    result = transform.transform(rows)
    assert len(result.fact) == 1
    assert result.fact["value"].iloc[0] == pytest.approx(11.5)
    assert result.stats["duplicate_rows_detected"] == 2


def test_missing_payload_columns_are_backfilled(raw_row):
    """Socrata omits all-null columns; the schema must stay stable regardless."""
    row = raw_row()
    row.pop("lowconfidencelimit")
    row.pop("datavaluefootnotesymbol", None)
    result = transform.transform([row])
    assert "ci_low" in result.fact.columns
    assert result.fact["ci_low"].isna().all()
    assert "lowconfidencelimit" in result.stats["columns_absent_in_payload"]


def test_geo_level_and_region_classification(raw_row):
    rows = [
        raw_row(locationid="17", locationabbr="IL"),
        raw_row(locationid="59", locationabbr="US", locationdesc="United States"),
        raw_row(locationid="72", locationabbr="PR", locationdesc="Puerto Rico"),
    ]
    result = transform.transform(rows)
    dim = result.dim_location.set_index("location_abbr")
    assert dim.loc["IL", "geo_level"] == "State"
    assert dim.loc["US", "geo_level"] == "National"
    assert dim.loc["PR", "geo_level"] == "Territory"
    assert dim.loc["IL", "is_addressable_market"]
    assert not dim.loc["PR", "is_addressable_market"]
    assert dim.loc["IL", "census_region"] == "Midwest"


def test_ci_width_and_low_precision_flag(raw_row):
    rows = [
        raw_row(lowconfidencelimit="9.0", highconfidencelimit="12.0"),
        raw_row(locationid="02", lowconfidencelimit="2.0", highconfidencelimit="40.0"),
    ]
    result = transform.transform(rows)
    fact = result.fact.set_index("location_id")
    assert fact.loc["17", "ci_width"] == pytest.approx(3.0)
    assert not fact.loc["17", "is_low_precision"]
    assert fact.loc["02", "is_low_precision"]


def test_indicator_dimension_carries_product_metadata(modelled):
    dim = modelled.dim_indicator.set_index("indicator_id")
    assert dim.loc["DIA01", "pillar"] == "Diabetes"
    assert dim.loc["DIA01", "polarity"] == "risk"
    assert set(dim["measure_role"]) <= {"burden", "care_gap"}
