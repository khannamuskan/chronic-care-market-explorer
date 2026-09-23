"""Tests for the population reference and the market-sizing it enables.

No network: the loader tests build their own CSV, and the ranking tests use a
synthetic scorecard. The one test that touches the committed reference file is
explicit about it, because that file is a deliberate, reviewable artefact.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import analytics, config, population


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def _write_reference(tmp_path, body: str):
    path = tmp_path / "pop.csv"
    path.write_text(
        "# provenance header that must be ignored\n"
        "location_id,location_desc,adult_population\n" + body,
        encoding="utf-8",
    )
    return path


def test_loader_skips_provenance_comments_and_preserves_fips_padding(tmp_path):
    path = _write_reference(tmp_path, "01,Alabama,3972413\n06,California,30562950\n")
    table = population.load_adult_population(path)
    assert list(table["location_id"]) == ["01", "06"]
    assert table.loc[table["location_id"] == "06", "adult_population"].item() == 30562950


def test_loader_zero_pads_a_fips_code_that_lost_its_leading_zero(tmp_path):
    """Spreadsheet round-trips turn '01' into 1; that must not break the join."""
    path = _write_reference(tmp_path, "1,Alabama,3972413\n")
    table = population.load_adult_population(path)
    assert table["location_id"].tolist() == ["01"]


def test_missing_reference_names_the_fix(tmp_path):
    with pytest.raises(population.PopulationReferenceError, match="refresh_population"):
        population.load_adult_population(tmp_path / "absent.csv")


def test_duplicate_states_are_rejected(tmp_path):
    path = _write_reference(tmp_path, "06,California,30562950\n06,California,1\n")
    with pytest.raises(population.PopulationReferenceError, match="Duplicate"):
        population.load_adult_population(path)


def test_non_numeric_population_is_rejected(tmp_path):
    path = _write_reference(tmp_path, "06,California,unknown\n")
    with pytest.raises(population.PopulationReferenceError, match="Non-numeric"):
        population.load_adult_population(path)


def test_attach_is_a_left_join_so_unmatched_geographies_survive(tmp_path):
    path = _write_reference(tmp_path, "06,California,30562950\n")
    frame = pd.DataFrame({"location_id": ["06", "72"], "value": [1.0, 2.0]})
    out = population.attach(frame, path)
    assert len(out) == 2  # Puerto Rico keeps its row
    assert pd.isna(out.loc[out["location_id"] == "72", "adult_population"].item())


def test_committed_reference_covers_every_addressable_state():
    """The shipped denominator must cover the market the app claims to rank."""
    table = population.load_adult_population()
    assert len(table) == config.N_ADDRESSABLE_STATES
    assert table["adult_population"].min() > 0
    # National 18+ civilian population, sanity-banded rather than pinned exactly
    # so a Census vintage refresh does not fail the suite spuriously.
    assert 230_000_000 < int(table["adult_population"].sum()) < 290_000_000


# ---------------------------------------------------------------------------
# Market sizing and dual ranking
# ---------------------------------------------------------------------------
def _scorecard() -> pd.DataFrame:
    """Small state with a great rate, large state with a mediocre one."""
    return pd.DataFrame({
        "location_abbr": ["SM", "LG", "MD"],
        "location_name": ["Smallville", "Largeland", "Middleton"],
        "census_region": ["West", "South", "Midwest"],
        "burden_index": [90.0, 50.0, 60.0],
        "opportunity_score": [100.0, 50.0, 60.0],
        "burden_component": [100.0, 50.0, 60.0],
        "trend_component": [100.0, 50.0, 60.0],
        "equity_component": [100.0, 50.0, 60.0],
        "care_gap_component": [100.0, 50.0, 60.0],
        "untreated_pct": [40.0, 50.0, 30.0],
        "adult_population": [500_000, 20_000_000, 3_000_000],
        "condition_caseload": [400_000.0, 12_000_000.0, 1_800_000.0],
        "cardio_caseload": [200_000.0, 6_000_000.0, 900_000.0],
    })


def test_the_two_bases_disagree_and_both_are_reported():
    scored = analytics.apply_rankings(_scorecard(), basis="rate")
    assert scored.iloc[0]["location_abbr"] == "SM"  # best rate wins

    by_lives = analytics.apply_rankings(_scorecard(), basis="lives")
    assert by_lives.iloc[0]["location_abbr"] == "LG"  # volume wins

    # Both rankings travel with the row regardless of which one sorted it.
    for frame in (scored, by_lives):
        assert {"rank_rate", "rank_lives", "rank_shift"} <= set(frame.columns)


def test_rank_shift_signs_point_the_way_they_are_documented():
    scored = analytics.apply_rankings(_scorecard(), basis="rate")
    row = scored.set_index("location_abbr")
    # Largeland ranks worse on rate than on lives -> positive shift.
    assert row.loc["LG", "rank_shift"] > 0
    # Smallville is the reverse.
    assert row.loc["SM", "rank_shift"] < 0


def test_opportunity_adults_discounts_volume_by_score():
    scored = analytics.apply_rankings(_scorecard(), basis="rate")
    row = scored.set_index("location_abbr")
    expected = row.loc["LG", "opportunity_score"] / 100.0 * 12_000_000.0
    assert row.loc["LG", "opportunity_adults"] == pytest.approx(expected, rel=1e-6)


def test_untreated_adults_uses_the_cardiovascular_caseload_not_everything():
    """The care-gap indicators only apply to BP/cholesterol, so the denominator
    must be the cardiovascular caseload, never the full condition caseload."""
    scored = analytics.apply_rankings(_scorecard(), basis="rate")
    row = scored.set_index("location_abbr")
    assert row.loc["LG", "untreated_adults"] == pytest.approx(6_000_000.0 * 0.50)
    assert row.loc["SM", "untreated_adults"] == pytest.approx(200_000.0 * 0.40)


def test_an_unknown_basis_is_rejected():
    with pytest.raises(ValueError, match="Unknown ranking basis"):
        analytics.apply_rankings(_scorecard(), basis="vibes")


def test_ranking_degrades_to_rate_when_population_is_unavailable():
    """Losing the reference must not break the rate ranking the app falls back to."""
    bare = _scorecard().drop(columns=["condition_caseload", "cardio_caseload"])
    scored = analytics.apply_rankings(bare, basis="rate")
    assert scored.iloc[0]["location_abbr"] == "SM"
    assert scored["opportunity_adults"].isna().all()


def test_ranking_by_lives_refuses_to_run_without_a_denominator():
    """An empty volume ranking would read as 'no big markets'; fail instead."""
    bare = _scorecard().drop(columns=["condition_caseload", "cardio_caseload"])
    with pytest.raises(ValueError, match="no population denominator"):
        analytics.apply_rankings(bare, basis="lives")


def test_rescore_rebuilds_headcounts_so_the_views_cannot_drift():
    base = analytics.apply_rankings(_scorecard(), basis="rate")
    trend_led = analytics.rescore(
        base, {"burden": 0.0, "trend": 1.0, "equity_gap": 0.0, "care_gap": 0.0}
    )
    row = trend_led.set_index("location_abbr")
    expected = row.loc["MD", "opportunity_score"] / 100.0 * 1_800_000.0
    assert row.loc["MD", "opportunity_adults"] == pytest.approx(expected, rel=1e-6)


def test_a_state_with_no_population_row_keeps_its_rate_rank():
    frame = _scorecard()
    frame.loc[frame["location_abbr"] == "MD", "condition_caseload"] = np.nan
    scored = analytics.apply_rankings(frame, basis="rate")
    row = scored.set_index("location_abbr")
    assert pd.notna(row.loc["MD", "opportunity_score"])
    assert pd.isna(row.loc["MD", "opportunity_adults"])
    assert pd.isna(row.loc["MD", "rank_lives"])
