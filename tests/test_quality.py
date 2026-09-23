"""Tests for the data quality layer.

Each test injects a specific defect and asserts the matching check catches it --
a check that cannot fail is not a check.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import quality, transform


def _by_id(results, check_id):
    return next(r for r in results if r.check_id == check_id)


def test_clean_data_passes_all_blocking_checks(modelled, extract_stats):
    results = quality.run_all_checks(modelled, extract_stats)
    blocking = [r for r in results if r.severity == quality.ERROR and r.status == quality.FAIL]
    assert blocking == [], [r.message for r in blocking]


def test_summary_overall_status_reflects_severity(modelled, extract_stats):
    results = quality.run_all_checks(modelled, extract_stats)
    summary = quality.summarise(results)
    assert summary["overall_status"] in {"PASS", "WARN"}
    assert summary["checks_run"] == len(results)
    assert summary["passed"] + summary["failed"] == summary["checks_run"]


def test_reconciliation_detects_a_partial_download(modelled):
    stats = {"expected_row_count": 10_000, "fetched_row_count": 6_000}
    result = quality.check_extract_reconciliation(modelled.fact, stats)
    assert result.status == quality.FAIL
    assert result.severity == quality.ERROR


def test_reconciliation_tolerates_tiny_drift(modelled):
    stats = {"expected_row_count": 10_000, "fetched_row_count": 9_998}
    assert quality.check_extract_reconciliation(modelled.fact, stats).status == quality.PASS


def test_out_of_range_prevalence_is_caught(modelled):
    fact = modelled.fact.copy()
    fact.loc[0, "value"] = 141.0
    result = quality.check_value_range(fact)
    assert result.status == quality.FAIL
    assert result.rows_failed == 1
    assert result.sample


def test_inverted_confidence_interval_is_caught(modelled):
    fact = modelled.fact.copy()
    fact.loc[0, "ci_low"] = 99.0
    fact.loc[0, "ci_high"] = 1.0
    assert quality.check_ci_ordering(fact).status == quality.FAIL


def test_estimate_outside_its_own_interval_is_caught(modelled):
    fact = modelled.fact.copy()
    fact.loc[0, "value"] = 90.0
    result = quality.check_value_within_ci(fact)
    assert result.status == quality.FAIL
    assert result.severity == quality.WARNING


def test_rounding_slack_does_not_trip_the_ci_check(modelled):
    fact = modelled.fact.copy()
    fact.loc[0, "value"] = fact.loc[0, "ci_low"] - 0.04
    assert quality.check_value_within_ci(fact).status == quality.PASS


def test_duplicate_grain_is_caught(modelled):
    fact = pd.concat([modelled.fact, modelled.fact.head(1)], ignore_index=True)
    result = quality.check_unique_grain(fact)
    assert result.status == quality.FAIL
    assert result.severity == quality.ERROR


def test_null_business_key_is_caught(modelled):
    fact = modelled.fact.copy()
    fact.loc[0, "location_id"] = None
    assert quality.check_key_not_null(fact).status == quality.FAIL


def test_orphaned_foreign_key_is_caught(modelled):
    broken = transform.TransformResult(
        modelled.fact,
        modelled.dim_location[modelled.dim_location["location_id"] != "17"],
        modelled.dim_indicator,
        modelled.dim_stratum,
        modelled.stats,
    )
    result = quality.check_referential_integrity(broken)
    assert result.status == quality.FAIL
    assert result.severity == quality.ERROR


def test_suppression_escalates_from_warning_to_error(modelled):
    fact = modelled.fact.copy()

    fact["is_suppressed"] = False
    assert quality.check_suppression_rate(fact).status == quality.PASS

    fact["is_suppressed"] = [i < len(fact) * 0.25 for i in range(len(fact))]
    mid = quality.check_suppression_rate(fact)
    assert mid.status == quality.FAIL and mid.severity == quality.WARNING

    fact["is_suppressed"] = True
    high = quality.check_suppression_rate(fact)
    assert high.status == quality.FAIL and high.severity == quality.ERROR


def test_unexplained_missing_values_are_flagged(modelled):
    fact = modelled.fact.copy()
    fact.loc[0, "is_missing_unexplained"] = True
    assert quality.check_unexplained_missing(fact).status == quality.FAIL


def test_thin_state_coverage_is_flagged(modelled):
    """Two states in the fixture is well below the 45-state threshold."""
    result = quality.check_state_coverage(modelled.fact, modelled.dim_location)
    assert result.status == quality.FAIL
    assert result.severity == quality.WARNING  # a source gap, not a pipeline failure


def test_stratum_drift_between_years_is_detected(modelled):
    """Drop a race category from one year only -- equity comparisons break silently."""
    fact = modelled.fact
    drift = fact[~((fact["year"] == 2023) & (fact["stratum_key"] == "RACE|BLK"))]
    result = quality.check_stratum_drift(drift, modelled.dim_stratum)
    assert result.status == quality.FAIL
    assert "2023" in result.message


def test_outlier_check_flags_an_implausible_value(modelled):
    fact = modelled.fact.copy()
    extra = pd.concat([fact] * 4, ignore_index=True)
    extra["location_id"] = [str(i) for i in range(len(extra))]
    extra.loc[0, "value"] = 99.9
    result = quality.check_outliers(extra)
    assert result.severity == quality.INFO  # flagged for review, never blocking


def test_a_raising_check_does_not_kill_the_run(modelled, extract_stats, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(quality, "check_outliers", boom)
    results = quality.run_all_checks(modelled, extract_stats)
    assert any("exception" in r.message for r in results)
    assert len(results) == 14


def test_results_serialise_to_a_frame(modelled, extract_stats):
    frame = quality.to_frame(quality.run_all_checks(modelled, extract_stats))
    assert {"check_id", "severity", "status", "fail_rate"} <= set(frame.columns)
    assert len(frame) == 14
