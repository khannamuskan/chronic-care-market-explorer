"""Persistence layer: Parquet files plus a DuckDB warehouse.

Parquet is the portable artefact (a reviewer can open it with pandas alone).
DuckDB is registered over the same frames so the app - and anyone poking at the
project - can run real SQL against the star schema without a server.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from . import config

logger = logging.getLogger(__name__)


def write_parquet(tables: dict[str, pd.DataFrame]) -> dict[str, str]:
    written: dict[str, str] = {}
    for name, df in tables.items():
        if df is None or df.empty:
            logger.warning("Table %s is empty; writing an empty Parquet file anyway", name)
        path = config.PROCESSED_DIR / f"{name}.parquet"
        # Object columns holding pandas NA break Arrow; normalise to str/None.
        out = df.copy()
        for col in out.columns:
            if out[col].dtype == "object" or str(out[col].dtype) == "string":
                out[col] = out[col].astype("object").where(out[col].notna(), None)
        out.to_parquet(path, index=False)
        written[name] = str(path)
        logger.info("Wrote %s (%s rows) -> %s", name, len(df), path.name)
    return written


def build_duckdb(tables: dict[str, pd.DataFrame]) -> Path:
    """(Re)create the DuckDB warehouse from the modelled tables."""
    if config.DUCKDB_PATH.exists():
        config.DUCKDB_PATH.unlink()
    con = duckdb.connect(str(config.DUCKDB_PATH))
    try:
        for name, df in tables.items():
            con.register("_staging", df)
            con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _staging')
            con.unregister("_staging")
        con.execute("CHECKPOINT")
        logger.info("Built DuckDB warehouse with %s tables -> %s",
                    len(tables), config.DUCKDB_PATH.name)
    finally:
        con.close()
    return config.DUCKDB_PATH


def read_table(name: str) -> pd.DataFrame:
    """Read a processed table, preferring Parquet and falling back to DuckDB."""
    path = config.PROCESSED_DIR / f"{name}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    if config.DUCKDB_PATH.exists():
        con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
        try:
            return con.execute(f'SELECT * FROM "{name}"').df()
        finally:
            con.close()
    raise FileNotFoundError(
        f"Processed table '{name}' not found. Run `python etl.py` first."
    )


def query(sql: str) -> pd.DataFrame:
    if not config.DUCKDB_PATH.exists():
        raise FileNotFoundError("Warehouse not built. Run `python etl.py` first.")
    con = duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote %s", path.name)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
