"""Tests for the extraction layer -- all HTTP is stubbed, nothing hits the network."""

from __future__ import annotations

import json

import pytest
import requests

from src import config, extract


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else []
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}", response=self)


class FakeSession:
    """Replays a scripted list of responses/exceptions and records the calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(params or {})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(extract.time, "sleep", lambda *_: None)


def test_retries_then_succeeds_on_a_transient_error():
    session = FakeSession([
        requests.ConnectionError("network blip"),
        FakeResponse([{"ok": 1}]),
    ])
    counter = [0]
    rows = extract._get_with_retry(session, {"$limit": 1}, counter)
    assert rows == [{"ok": 1}]
    assert counter[0] == 1


def test_retries_on_rate_limiting():
    session = FakeSession([FakeResponse(status_code=429), FakeResponse([{"ok": 1}])])
    counter = [0]
    assert extract._get_with_retry(session, {}, counter) == [{"ok": 1}]
    assert counter[0] == 1


def test_gives_up_after_max_retries():
    session = FakeSession([requests.ConnectionError("down")] * config.API_MAX_RETRIES)
    with pytest.raises(extract.ExtractionError, match="Gave up"):
        extract._get_with_retry(session, {}, [0])


def test_a_bad_query_fails_fast_without_retrying():
    """A 400 means our SoQL is wrong; retrying it just wastes time."""
    session = FakeSession([FakeResponse(status_code=400)])
    with pytest.raises(extract.ExtractionError, match="rejected the request"):
        extract._get_with_retry(session, {"$where": "nonsense"}, [0])
    assert len(session.calls) == 1


def test_non_list_payload_is_rejected():
    session = FakeSession([FakeResponse({"error": "boom"})] * config.API_MAX_RETRIES)
    with pytest.raises(extract.ExtractionError):
        extract._get_with_retry(session, {}, [0])


def test_pagination_stops_on_a_short_page(monkeypatch):
    monkeypatch.setattr(config, "API_PAGE_SIZE", 2)
    session = FakeSession([
        FakeResponse([{"i": 1}, {"i": 2}]),
        FakeResponse([{"i": 3}]),
    ])
    pages = list(extract.iter_pages(session, [0]))
    assert [len(p) for p in pages] == [2, 1]
    assert [c["$offset"] for c in session.calls] == [0, 2]


def test_pagination_stops_on_an_empty_page(monkeypatch):
    monkeypatch.setattr(config, "API_PAGE_SIZE", 2)
    session = FakeSession([FakeResponse([{"i": 1}, {"i": 2}]), FakeResponse([])])
    assert sum(len(p) for p in extract.iter_pages(session, [0])) == 2


def test_pages_are_ordered_so_they_cannot_overlap(monkeypatch):
    monkeypatch.setattr(config, "API_PAGE_SIZE", 2)
    session = FakeSession([FakeResponse([])])
    list(extract.iter_pages(session, [0]))
    assert "$order" in session.calls[0]


def test_where_clause_covers_the_configured_scope():
    clause = extract._where_clause()
    for indicator_id in config.INDICATOR_IDS:
        assert f"'{indicator_id}'" in clause
    for measure in config.MEASURE_TYPES:
        assert f"'{measure}'" in clause


def test_expected_row_count_degrades_gracefully():
    session = FakeSession([FakeResponse(status_code=400)])
    assert extract.fetch_expected_row_count(session, [0]) is None


def test_offline_without_a_snapshot_explains_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", tmp_path)
    with pytest.raises(extract.ExtractionError, match="offline"):
        extract.extract(offline=True)


def test_raw_snapshot_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", tmp_path)
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    path = extract._write_raw_snapshot(rows, "2026-01-01T00:00:00+00:00")
    assert extract._read_raw_snapshot(path) == rows

    pointer = json.loads((tmp_path / "latest.json").read_text())
    assert pointer["row_count"] == 2
    assert pointer["dataset_id"] == config.API_DATASET_ID

    replayed = extract.extract(offline=True)
    assert replayed.from_cache and replayed.fetched_row_count == 2
