"""Shared pytest fixtures.

The tests run entirely on synthetic frames -- no network, no dependency on a
previous ETL run -- so they are fast and deterministic in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import transform  # noqa: E402


def _row(**overrides) -> dict:
    """A realistically shaped raw CDI record."""
    base = {
        "yearstart": "2023",
        "yearend": "2023",
        "locationid": "17",
        "locationabbr": "IL",
        "locationdesc": "Illinois",
        "questionid": "DIA01",
        "question": "Diabetes among adults",
        "topicid": "DIA",
        "topic": "Diabetes",
        "datavaluetypeid": "AGEADJPREV",
        "datavaluetype": "Age-adjusted Prevalence",
        "datavalueunit": "%",
        "datavalue": "10.9",
        "datavaluealt": "10.9",
        "lowconfidencelimit": "8.8",
        "highconfidencelimit": "13.4",
        "stratificationcategoryid1": "OVERALL",
        "stratificationcategory1": "Overall",
        "stratificationid1": "OVR",
        "stratification1": "Overall",
        "datasource": "BRFSS",
        "geolocation": {"type": "Point", "coordinates": [-88.99, 40.48]},
    }
    base.update(overrides)
    return base


@pytest.fixture
def raw_row():
    return _row


@pytest.fixture
def raw_rows(raw_row):
    """A small but structurally complete extract: 2 states x 2 years x strata."""
    rows = []
    values = {
        ("17", 2022): 10.1, ("17", 2023): 10.9,
        ("06", 2022): 9.4, ("06", 2023): 9.9,
    }
    for (loc, year), val in values.items():
        abbr, name = ("IL", "Illinois") if loc == "17" else ("CA", "California")
        for qid, topic in (("DIA01", "DIA"), ("NPW14", "NPAW")):
            rows.append(raw_row(
                yearstart=str(year), yearend=str(year), locationid=loc,
                locationabbr=abbr, locationdesc=name, questionid=qid, topicid=topic,
                datavalue=str(val), datavaluealt=str(val),
                lowconfidencelimit=str(val - 1), highconfidencelimit=str(val + 1),
            ))
            # Two race strata so equity gaps are computable.
            for sid, sname, delta in (("BLK", "Black, non-Hispanic", 4.0),
                                      ("WHT", "White, non-Hispanic", -1.0)):
                rows.append(raw_row(
                    yearstart=str(year), yearend=str(year), locationid=loc,
                    locationabbr=abbr, locationdesc=name, questionid=qid,
                    topicid=topic,
                    datavalue=str(val + delta), datavaluealt=str(val + delta),
                    lowconfidencelimit=str(val + delta - 1),
                    highconfidencelimit=str(val + delta + 1),
                    stratificationcategoryid1="RACE",
                    stratificationcategory1="Race/Ethnicity",
                    stratificationid1=sid, stratification1=sname,
                ))
    return rows


@pytest.fixture
def modelled(raw_rows):
    return transform.transform(raw_rows)


@pytest.fixture
def extract_stats():
    return {"expected_row_count": None, "fetched_row_count": 24}
