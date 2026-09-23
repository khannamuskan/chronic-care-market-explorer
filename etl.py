"""ETL entry point for the Chronic Care Market Prioritization Explorer.

    python etl.py                 # full run against the live CDC API
    python etl.py --offline       # rebuild from the newest cached raw snapshot
    python etl.py --allow-dq-failures   # build outputs even if a blocking check fails

The run is deliberately fail-fast: if a blocking data quality check fails, the
processed tables are still written (so they can be inspected) but the process
exits non-zero, which is what a scheduler or CI job needs in order to alert.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from src import analytics, config, extract, quality, storage, transform

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_DIR / "etl.log", mode="a", encoding="utf-8"),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the chronic-care analytical dataset.")
    p.add_argument("--offline", action="store_true",
                   help="Skip the API and rebuild from the newest raw snapshot.")
    p.add_argument("--allow-dq-failures", action="store_true",
                   help="Exit 0 even when a blocking data quality check fails.")
    p.add_argument("--verbose", action="store_true", help="Debug logging.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    log = logging.getLogger("etl")
    started = time.perf_counter()

    log.info("=" * 78)
    log.info("Chronic Care Market Prioritization Explorer -- ETL run")
    log.info("Source: CDC Chronic Disease Indicators (%s)", config.API_DATASET_ID)
    log.info("Scope : %s indicators across %s pillars",
             len(config.INDICATORS), len(config.PILLARS))
    log.info("=" * 78)

    # ---------------------------------------------------------------- extract
    try:
        log.info("[1/5] Extracting from the CDC API ...")
        extracted = extract.extract(offline=args.offline)
    except extract.ExtractionError as exc:
        log.error("Extraction failed: %s", exc)
        log.error("Tip: re-run with --offline to rebuild from a cached snapshot.")
        return 2

    extract_stats = extract.result_summary(extracted)
    log.info("      %s rows from %s", f"{extracted.fetched_row_count:,}",
             "cache" if extracted.from_cache else "live API")

    # -------------------------------------------------------------- transform
    log.info("[2/5] Transforming into the star schema ...")
    modelled = transform.transform(extracted.rows)
    log.info("      fact_indicator=%s  dim_location=%s  dim_indicator=%s  dim_stratum=%s",
             len(modelled.fact), len(modelled.dim_location),
             len(modelled.dim_indicator), len(modelled.dim_stratum))

    # ---------------------------------------------------------- data quality
    log.info("[3/5] Running data quality checks ...")
    checks = quality.run_all_checks(modelled, extract_stats)
    dq_summary = quality.summarise(checks)
    log.info("      %s checks | passed=%s failed=%s | overall=%s",
             dq_summary["checks_run"], dq_summary["passed"],
             dq_summary["failed"], dq_summary["overall_status"])

    # ------------------------------------------------------------- analytics
    log.info("[4/5] Building analytical marts ...")
    marts, mart_stats = analytics.build_all(modelled)

    # --------------------------------------------------------------- persist
    log.info("[5/5] Persisting Parquet + DuckDB ...")
    tables = modelled.tables() | marts | {"dq_results": quality.to_frame(checks)}
    written = storage.write_parquet(tables)
    storage.build_duckdb(tables)
    storage.write_json(config.DQ_SUMMARY_PATH, dq_summary)

    elapsed = time.perf_counter() - started
    manifest = {
        "run_completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(elapsed, 2),
        "offline_mode": args.offline,
        "source": {
            "dataset_id": config.API_DATASET_ID,
            "url": config.API_BASE_URL,
            "indicators": list(config.INDICATOR_IDS),
            "pillars": list(config.PILLARS),
        },
        "extract": extract_stats,
        "transform": modelled.stats,
        "analytics": mart_stats,
        "data_quality": {k: v for k, v in dq_summary.items() if k != "details"},
        "outputs": written | {"duckdb": str(config.DUCKDB_PATH)},
    }
    storage.write_json(config.RUN_MANIFEST_PATH, manifest)

    log.info("-" * 78)
    log.info("ETL finished in %.1fs | data quality: %s", elapsed, dq_summary["overall_status"])
    if dq_summary["blocking_failures"]:
        log.error("Blocking data quality failures: %s", dq_summary["blocking_failures"])
        log.error("Outputs were still written so the failures can be inspected in the app.")
        if not args.allow_dq_failures:
            return 1
    log.info("Next step: streamlit run app.py")
    log.info("-" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
