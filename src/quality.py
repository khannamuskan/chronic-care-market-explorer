"""Data quality layer.

Every check returns a :class:`CheckResult` so the ETL, the tests and the
Streamlit app all consume the same structure. Checks are graded:

``ERROR``    the output is not trustworthy -- fail the run (unless overridden)
``WARNING``  worth a human look, but the product still works
``INFO``     observability only

The checks below are not decorative: each one targets a failure mode that
actually exists in the CDI feed (statistical suppression, confidence intervals
that invert, stratification codes that change between survey years, states that
skip a BRFSS module in a given year, and partial downloads).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import config
from .transform import FACT_NATURAL_KEY, TransformResult

logger = logging.getLogger(__name__)

ERROR, WARNING, INFO = "ERROR", "WARNING", "INFO"
PASS, FAIL = "PASS", "FAIL"


@dataclass
class CheckResult:
    check_id: str
    name: str
    dimension: str  # completeness | validity | uniqueness | consistency | timeliness | accuracy
    severity: str
    status: str
    rows_checked: int
    rows_failed: int
    message: str
    sample: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fail_rate(self) -> float:
        return self.rows_failed / self.rows_checked if self.rows_checked else 0.0

    def as_row(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "dimension": self.dimension,
            "severity": self.severity,
            "status": self.status,
            "rows_checked": self.rows_checked,
            "rows_failed": self.rows_failed,
            "fail_rate": round(self.fail_rate, 6),
            "message": self.message,
        }


def _sample(df: pd.DataFrame, cols: list[str], n: int = 5) -> list[dict[str, Any]]:
    if df.empty:
        return []
    cols = [c for c in cols if c in df.columns]
    return df[cols].head(n).astype(str).to_dict(orient="records")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_extract_reconciliation(fact: pd.DataFrame, extract_stats: dict[str, Any]) -> CheckResult:
    """Did we download everything the API said was in scope?"""
    expected = extract_stats.get("expected_row_count")
    fetched = extract_stats.get("fetched_row_count", 0)
    if expected is None:
        return CheckResult(
            "DQ01", "Extract row-count reconciliation", "completeness", INFO, PASS,
            fetched, 0, "API did not return a count; reconciliation skipped.",
        )
    delta = abs(expected - fetched)
    # The CDI dataset can be republished mid-crawl; a handful of rows of drift
    # is tolerable, a large gap means the pagination loop broke.
    ok = delta <= max(5, int(0.001 * expected))
    return CheckResult(
        "DQ01", "Extract row-count reconciliation", "completeness", ERROR,
        PASS if ok else FAIL, fetched, delta,
        f"API reported {expected:,} rows in scope; downloaded {fetched:,} (delta {delta:,}).",
    )


def check_required_columns(fact: pd.DataFrame) -> CheckResult:
    required = set(FACT_NATURAL_KEY) | {"value", "ci_low", "ci_high", "is_suppressed"}
    missing = sorted(required - set(fact.columns) - {"stratum_category_id", "stratum_id"})
    # The fact stores the composite stratum_key rather than its two parts.
    missing = [m for m in missing if m != "stratum_key"]
    return CheckResult(
        "DQ02", "Required fact columns present", "consistency", ERROR,
        PASS if not missing else FAIL, len(fact), len(missing),
        "All modelled columns present." if not missing else f"Missing columns: {missing}",
    )


def check_key_not_null(fact: pd.DataFrame) -> CheckResult:
    keys = ["year", "location_id", "indicator_id", "measure_type_id", "stratum_key"]
    bad = fact[fact[keys].isna().any(axis=1)]
    return CheckResult(
        "DQ03", "Business keys are populated", "completeness", ERROR,
        PASS if bad.empty else FAIL, len(fact), len(bad),
        "No null business keys." if bad.empty else f"{len(bad):,} rows have a null key column.",
        _sample(bad, keys),
    )


def check_unique_grain(fact: pd.DataFrame) -> CheckResult:
    keys = ["year", "location_id", "indicator_id", "measure_type_id", "stratum_key"]
    dupes = fact[fact.duplicated(subset=keys, keep=False)]
    return CheckResult(
        "DQ04", "One row per declared grain", "uniqueness", ERROR,
        PASS if dupes.empty else FAIL, len(fact), len(dupes),
        "Grain is unique." if dupes.empty
        else f"{len(dupes):,} rows violate the declared grain {keys}.",
        _sample(dupes.sort_values(keys), keys),
    )


def check_value_range(fact: pd.DataFrame) -> CheckResult:
    populated = fact[fact["value"].notna()]
    bad = populated[(populated["value"] < config.VALUE_MIN) | (populated["value"] > config.VALUE_MAX)]
    return CheckResult(
        "DQ05", "Prevalence within 0-100%", "validity", ERROR,
        PASS if bad.empty else FAIL, len(populated), len(bad),
        "All prevalence values are valid percentages."
        if bad.empty else f"{len(bad):,} estimates fall outside 0-100%.",
        _sample(bad, ["year", "location_id", "indicator_id", "value"]),
    )


def check_ci_ordering(fact: pd.DataFrame) -> CheckResult:
    both = fact[fact["ci_low"].notna() & fact["ci_high"].notna()]
    bad = both[both["ci_low"] > both["ci_high"]]
    return CheckResult(
        "DQ06", "Confidence interval bounds ordered", "validity", ERROR,
        PASS if bad.empty else FAIL, len(both), len(bad),
        "Lower bound never exceeds upper bound."
        if bad.empty else f"{len(bad):,} intervals are inverted.",
        _sample(bad, ["year", "location_id", "indicator_id", "ci_low", "ci_high"]),
    )


def check_value_within_ci(fact: pd.DataFrame) -> CheckResult:
    """The point estimate should sit inside its own confidence interval."""
    scoped = fact[fact["value"].notna() & fact["ci_low"].notna() & fact["ci_high"].notna()]
    tol = 0.05  # CDC publishes to 1dp, so allow rounding slack
    bad = scoped[
        (scoped["value"] < scoped["ci_low"] - tol) | (scoped["value"] > scoped["ci_high"] + tol)
    ]
    return CheckResult(
        "DQ07", "Point estimate inside its confidence interval", "accuracy", WARNING,
        PASS if bad.empty else FAIL, len(scoped), len(bad),
        "Every estimate sits inside its interval."
        if bad.empty else f"{len(bad):,} estimates fall outside their own CI.",
        _sample(bad, ["year", "location_id", "indicator_id", "value", "ci_low", "ci_high"]),
    )


def check_suppression_rate(fact: pd.DataFrame) -> CheckResult:
    rate = float(fact["is_suppressed"].mean()) if len(fact) else 0.0
    if rate > config.MAX_SUPPRESSION_RATE_FAIL:
        severity, status = ERROR, FAIL
    elif rate > config.MAX_SUPPRESSION_RATE_WARN:
        severity, status = WARNING, FAIL
    else:
        severity, status = WARNING, PASS
    return CheckResult(
        "DQ08", "CDC suppression rate within tolerance", "completeness", severity, status,
        len(fact), int(fact["is_suppressed"].sum()),
        f"{rate:.1%} of estimates are suppressed by CDC for statistical reliability "
        f"(warn >{config.MAX_SUPPRESSION_RATE_WARN:.0%}, fail >{config.MAX_SUPPRESSION_RATE_FAIL:.0%}).",
    )


def check_unexplained_missing(fact: pd.DataFrame) -> CheckResult:
    """Null estimate with no CDC footnote = our problem, not theirs."""
    bad = fact[fact["is_missing_unexplained"]]
    return CheckResult(
        "DQ09", "Missing values are explained by a CDC footnote", "completeness", WARNING,
        PASS if bad.empty else FAIL, len(fact), len(bad),
        "Every null estimate carries a suppression footnote."
        if bad.empty
        else f"{len(bad):,} estimates are null with no footnote - possible parsing loss.",
        _sample(bad, ["year", "location_id", "indicator_id", "stratum_key"]),
    )


def check_referential_integrity(result: TransformResult) -> CheckResult:
    fact = result.fact
    orphans = 0
    details = []
    for col, dim, key in (
        ("location_id", result.dim_location, "location_id"),
        ("indicator_id", result.dim_indicator, "indicator_id"),
        ("stratum_key", result.dim_stratum, "stratum_key"),
    ):
        missing = set(fact[col].dropna()) - set(dim[key].dropna())
        if missing:
            orphans += len(missing)
            details.append(f"{col}: {sorted(missing)[:5]}")
    return CheckResult(
        "DQ10", "Fact keys resolve to dimensions", "consistency", ERROR,
        PASS if orphans == 0 else FAIL, len(fact), orphans,
        "All foreign keys resolve." if orphans == 0
        else "Orphaned keys -> " + "; ".join(details),
    )


def check_state_coverage(fact: pd.DataFrame, dim_location: pd.DataFrame) -> CheckResult:
    """A BRFSS module a state skipped shows up as a coverage hole, not an error."""
    states = set(dim_location.loc[dim_location["is_addressable_market"], "location_id"])
    scoped = fact[fact["location_id"].isin(states) & fact["value"].notna()]
    if scoped.empty:
        return CheckResult(
            "DQ11", "State coverage per indicator-year", "completeness", ERROR, FAIL,
            0, 0, "No state-level estimates survived transformation.",
        )
    coverage = (
        scoped.groupby(["indicator_id", "year"])["location_id"].nunique().reset_index(name="n_states")
    )
    thin = coverage[coverage["n_states"] < config.MIN_STATE_COVERAGE]
    return CheckResult(
        "DQ11", "State coverage per indicator-year", "completeness", WARNING,
        PASS if thin.empty else FAIL, len(coverage), len(thin),
        f"All indicator-years cover >={config.MIN_STATE_COVERAGE}/{config.N_ADDRESSABLE_STATES} states."
        if thin.empty
        else f"{len(thin)} indicator-years cover fewer than {config.MIN_STATE_COVERAGE} states "
             f"(BRFSS modules are optional for states in some years).",
        _sample(thin.sort_values("n_states"), ["indicator_id", "year", "n_states"]),
    )


def check_stratum_drift(fact: pd.DataFrame, dim_stratum: pd.DataFrame) -> CheckResult:
    """Did the demographic breakdown change shape between survey years?

    CDC has re-coded race/ethnicity categories over time (for example splitting
    Asian/Pacific Islander). Joining across years without noticing produces
    silently wrong equity comparisons.
    """
    race = dim_stratum.loc[dim_stratum["stratum_category_id"] == "RACE", "stratum_key"]
    scoped = fact[fact["stratum_key"].isin(race)]
    if scoped.empty:
        return CheckResult(
            "DQ12", "Stratification categories stable across years", "consistency",
            WARNING, PASS, 0, 0, "No race/ethnicity strata in scope.",
        )
    per_year = scoped.groupby("year")["stratum_key"].apply(lambda s: frozenset(s.unique()))
    modal = per_year.value_counts().idxmax()
    drifted = per_year[per_year != modal]
    return CheckResult(
        "DQ12", "Stratification categories stable across years", "consistency", WARNING,
        PASS if drifted.empty else FAIL, len(per_year), len(drifted),
        "Race/ethnicity strata are consistent across all years."
        if drifted.empty
        else f"{len(drifted)} year(s) use a different race/ethnicity breakdown "
             f"({sorted(drifted.index.tolist())}) - cross-year equity gaps for those years "
             f"are not strictly comparable.",
    )


def check_outliers(fact: pd.DataFrame) -> CheckResult:
    """Robust (median/MAD) z-score within each indicator-year-measure."""
    scoped = fact[fact["value"].notna()].copy()
    if scoped.empty:
        return CheckResult("DQ13", "Statistical outliers", "accuracy", INFO, PASS, 0, 0, "No data.")

    def _robust_z(g: pd.Series) -> pd.Series:
        med = g.median()
        mad = (g - med).abs().median()
        if mad == 0 or np.isnan(mad):
            return pd.Series(0.0, index=g.index)
        return 0.6745 * (g - med) / mad

    scoped["robust_z"] = (
        scoped.groupby(["indicator_id", "year", "measure_type_id"])["value"]
        .transform(_robust_z)
    )
    bad = scoped[scoped["robust_z"].abs() > config.OUTLIER_Z_THRESHOLD]
    return CheckResult(
        "DQ13", "Statistical outliers flagged for review", "accuracy", INFO,
        PASS if bad.empty else FAIL, len(scoped), len(bad),
        "No extreme outliers."
        if bad.empty
        else f"{len(bad):,} estimates exceed |robust z| > {config.OUTLIER_Z_THRESHOLD} "
             f"within their indicator-year peer group (kept, but flagged).",
        _sample(bad.reindex(bad["robust_z"].abs().sort_values(ascending=False).index),
                ["year", "location_id", "indicator_id", "value", "robust_z"]),
    )


def check_freshness(fact: pd.DataFrame) -> CheckResult:
    latest = int(fact["year"].max())
    age = datetime.now().year - latest
    ok = age <= config.MAX_DATA_AGE_YEARS
    return CheckResult(
        "DQ14", "Source data freshness", "timeliness", WARNING, PASS if ok else FAIL,
        1, 0 if ok else 1,
        f"Most recent survey year is {latest} ({age} years old; threshold "
        f"{config.MAX_DATA_AGE_YEARS}). CDI is published with a reporting lag.",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_all_checks(result: TransformResult, extract_stats: dict[str, Any]) -> list[CheckResult]:
    fact = result.fact
    checks: list[Callable[[], CheckResult]] = [
        lambda: check_extract_reconciliation(fact, extract_stats),
        lambda: check_required_columns(fact),
        lambda: check_key_not_null(fact),
        lambda: check_unique_grain(fact),
        lambda: check_value_range(fact),
        lambda: check_ci_ordering(fact),
        lambda: check_value_within_ci(fact),
        lambda: check_suppression_rate(fact),
        lambda: check_unexplained_missing(fact),
        lambda: check_referential_integrity(result),
        lambda: check_state_coverage(fact, result.dim_location),
        lambda: check_stratum_drift(fact, result.dim_stratum),
        lambda: check_outliers(fact),
        lambda: check_freshness(fact),
    ]

    results: list[CheckResult] = []
    for fn in checks:
        try:
            results.append(fn())
        except Exception as exc:  # a broken check must not kill the pipeline
            logger.exception("Data quality check raised")
            results.append(
                CheckResult("DQ??", f"{fn} errored", "consistency", ERROR, FAIL, 0, 1,
                            f"Check raised an exception: {exc}")
            )
    for r in results:
        log = logger.error if (r.status == FAIL and r.severity == ERROR) else (
            logger.warning if r.status == FAIL else logger.info)
        log("[%s] %s %s -- %s", r.check_id, r.status, r.name, r.message)
    return results


def to_frame(results: list[CheckResult]) -> pd.DataFrame:
    return pd.DataFrame([r.as_row() for r in results])


def summarise(results: list[CheckResult]) -> dict[str, Any]:
    blocking = [r for r in results if r.status == FAIL and r.severity == ERROR]
    warnings = [r for r in results if r.status == FAIL and r.severity == WARNING]
    return {
        "checks_run": len(results),
        "passed": sum(r.status == PASS for r in results),
        "failed": sum(r.status == FAIL for r in results),
        "blocking_failures": [r.check_id for r in blocking],
        "warnings": [r.check_id for r in warnings],
        "overall_status": "FAIL" if blocking else ("WARN" if warnings else "PASS"),
        "details": [r.as_row() | {"sample": r.sample} for r in results],
    }
