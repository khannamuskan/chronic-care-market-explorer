"""Extraction layer: pull CDC Chronic Disease Indicator records over HTTP.

Design notes
------------
* Paged through the Socrata endpoint with a deterministic ``$order`` so pages
  cannot silently overlap or skip rows.
* Every HTTP call is retried with exponential backoff on transport errors and
  on retryable status codes (429 / 5xx). Non-retryable 4xx fail fast and loud.
* The raw payload is written to ``data/raw`` untouched (newline-delimited JSON)
  alongside a manifest, so the whole pipeline is reproducible offline and a
  reviewer can diff exactly what the API returned.
* ``--offline`` reuses the newest raw snapshot instead of calling the API,
  which is what makes the app demoable when the network or CDC is down.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import requests

from . import config

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ExtractionError(RuntimeError):
    """Raised when the source API cannot be read after exhausting retries."""


@dataclass
class ExtractResult:
    rows: list[dict[str, Any]]
    raw_path: Path
    expected_row_count: int | None
    fetched_row_count: int
    pages_fetched: int
    retries_used: int
    extracted_at: str
    source_url: str
    from_cache: bool


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session()
    headers = {"User-Agent": config.API_USER_AGENT, "Accept": "application/json"}
    token = os.environ.get(config.APP_TOKEN_ENV_VAR)
    if token:
        # Entirely optional. Raises Socrata's anonymous rate limit.
        headers["X-App-Token"] = token
        logger.info("Using Socrata app token from $%s", config.APP_TOKEN_ENV_VAR)
    s.headers.update(headers)
    return s


def _get_with_retry(
    session: requests.Session, params: dict[str, Any], retry_counter: list[int]
) -> list[dict[str, Any]]:
    """GET the CDI endpoint, retrying transient failures with backoff."""
    last_error: Exception | None = None

    for attempt in range(1, config.API_MAX_RETRIES + 1):
        try:
            response = session.get(
                config.API_BASE_URL, params=params, timeout=config.API_TIMEOUT_SECONDS
            )
            if response.status_code in RETRYABLE_STATUS:
                raise requests.HTTPError(
                    f"retryable status {response.status_code}", response=response
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ExtractionError(
                    f"Expected a JSON array from the API, got {type(payload).__name__}"
                )
            return payload

        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # A non-retryable client error means our query is wrong: fail fast.
            if status is not None and status not in RETRYABLE_STATUS and 400 <= status < 500:
                raise ExtractionError(
                    f"API rejected the request ({status}). Query params: {params}"
                ) from exc
            last_error = exc
            if attempt < config.API_MAX_RETRIES:
                sleep_for = config.API_BACKOFF_SECONDS * (2 ** (attempt - 1))
                retry_counter[0] += 1
                logger.warning(
                    "API call failed (attempt %s/%s): %s -- retrying in %.1fs",
                    attempt, config.API_MAX_RETRIES, exc, sleep_for,
                )
                time.sleep(sleep_for)

    raise ExtractionError(
        f"Gave up after {config.API_MAX_RETRIES} attempts against "
        f"{config.API_BASE_URL}. Last error: {last_error}"
    )


def _where_clause() -> str:
    quoted_questions = ",".join(f"'{q}'" for q in config.INDICATOR_IDS)
    quoted_measures = ",".join(f"'{m}'" for m in config.MEASURE_TYPES)
    return (
        f"questionid in({quoted_questions}) "
        f"and datavaluetypeid in({quoted_measures})"
    )


def fetch_expected_row_count(session: requests.Session, retry_counter: list[int]) -> int | None:
    """Ask the API how many rows our filter should return.

    Used to reconcile against what we actually downloaded -- a partial extract
    that silently stops halfway is the classic pipeline failure mode.
    """
    try:
        payload = _get_with_retry(
            session,
            {"$select": "count(1) as n", "$where": _where_clause()},
            retry_counter,
        )
        return int(payload[0]["n"])
    except (ExtractionError, KeyError, IndexError, ValueError) as exc:
        logger.warning("Could not read expected row count from API: %s", exc)
        return None


def iter_pages(
    session: requests.Session, retry_counter: list[int]
) -> Iterator[list[dict[str, Any]]]:
    """Yield successive pages of records using stable offset pagination."""
    offset = 0
    while True:
        params = {
            "$where": _where_clause(),
            "$limit": config.API_PAGE_SIZE,
            "$offset": offset,
            # Deterministic total ordering -> pages never overlap or skip.
            "$order": "yearstart,locationid,questionid,datavaluetypeid,"
                      "stratificationcategoryid1,stratificationid1",
        }
        page = _get_with_retry(session, params, retry_counter)
        logger.info("Fetched %s rows at offset %s", len(page), offset)
        if not page:
            return
        yield page
        if len(page) < config.API_PAGE_SIZE:
            return
        offset += config.API_PAGE_SIZE


# ---------------------------------------------------------------------------
# Raw persistence
# ---------------------------------------------------------------------------
def _write_raw_snapshot(rows: list[dict[str, Any]], extracted_at: str) -> Path:
    stamp = extracted_at.replace(":", "").replace("-", "").replace(".", "")[:15]
    path = config.RAW_DIR / f"cdi_raw_{stamp}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    latest = config.RAW_DIR / "latest.json"
    latest.write_text(
        json.dumps(
            {
                "file": path.name,
                "extracted_at": extracted_at,
                "row_count": len(rows),
                "source_url": config.API_BASE_URL,
                "dataset_id": config.API_DATASET_ID,
                "where": _where_clause(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Wrote raw snapshot -> %s (%s rows)", path.name, len(rows))
    return path


def _read_raw_snapshot(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def latest_raw_snapshot() -> Path | None:
    """Newest raw extract on disk, or None if we have never run."""
    pointer = config.RAW_DIR / "latest.json"
    if pointer.exists():
        candidate = config.RAW_DIR / json.loads(pointer.read_text())["file"]
        if candidate.exists():
            return candidate
    snapshots = sorted(config.RAW_DIR.glob("cdi_raw_*.jsonl*"))
    return snapshots[-1] if snapshots else None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def extract(offline: bool = False) -> ExtractResult:
    """Download (or reload) the raw CDI records in scope for this product."""
    extracted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if offline:
        snapshot = latest_raw_snapshot()
        if snapshot is None:
            raise ExtractionError(
                "--offline was requested but data/raw holds no snapshot. "
                "Run `python etl.py` once with network access first."
            )
        rows = _read_raw_snapshot(snapshot)
        logger.info("Offline mode: replayed %s rows from %s", len(rows), snapshot.name)
        return ExtractResult(
            rows=rows, raw_path=snapshot, expected_row_count=len(rows),
            fetched_row_count=len(rows), pages_fetched=0, retries_used=0,
            extracted_at=extracted_at, source_url=str(snapshot), from_cache=True,
        )

    session = _session()
    retry_counter = [0]
    expected = fetch_expected_row_count(session, retry_counter)
    logger.info("API reports %s rows in scope", expected if expected is not None else "unknown")

    rows: list[dict[str, Any]] = []
    pages = 0
    for page in iter_pages(session, retry_counter):
        rows.extend(page)
        pages += 1

    if not rows:
        raise ExtractionError(
            "The API returned zero rows for the configured indicator scope. "
            "The CDC may have re-coded questionids -- check src/config.INDICATORS."
        )

    raw_path = _write_raw_snapshot(rows, extracted_at)
    return ExtractResult(
        rows=rows, raw_path=raw_path, expected_row_count=expected,
        fetched_row_count=len(rows), pages_fetched=pages,
        retries_used=retry_counter[0], extracted_at=extracted_at,
        source_url=config.API_BASE_URL, from_cache=False,
    )


def result_summary(result: ExtractResult) -> dict[str, Any]:
    d = asdict(result)
    d.pop("rows")
    d["raw_path"] = str(result.raw_path)
    return d
