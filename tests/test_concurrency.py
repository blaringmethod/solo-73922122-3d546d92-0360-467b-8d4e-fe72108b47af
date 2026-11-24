"""Concurrency semantics: writers serialise, readers see published snapshots."""

from __future__ import annotations

import concurrent.futures
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import service
from app.db import read_transaction, write_transaction
from app.errors import CycleDetectedError, VersionConflictError


def _layers(db: Path, area: str) -> dict[str, int]:
    return {
        item["code"]: item["id"]
        for item in service.list_layers(db, area, 5_000)
    }


def _version(db: Path, area: str) -> int:
    return int(service.get_area(db, area, 5_000)["version"])


def _addition(number: str, earlier: int, later: int) -> dict:
    return {
        "record_number": number,
        "earlier_layer_id": earlier,
        "later_layer_id": later,
    }


def test_same_base_version_concurrent_writers_only_one_wins(seeded, db_path: Path):
    t1 = _layers(db_path, "T1")
    base = _version(db_path, "T1")

    def submit(number: str) -> str:
        try:
            service.submit_batch(
                db_path,
                "T1",
                base_version=base,
                additions=[_addition(number, t1["L1"], t1["L4"])],
                withdrawals=[],
                note=None,
                busy_timeout_ms=5_000,
            )
            return "ok"
        except VersionConflictError:
            return "conflict"

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, [f"R-T1-{n:03d}" for n in range(800, 806)]))

    assert results.count("ok") == 1
    assert results.count("conflict") == 5
    assert _version(db_path, "T1") == base + 1
    active = service.list_records(db_path, "T1", include_withdrawn=False, busy_timeout_ms=5_000)
    added = [item for item in active if item["record_number"].startswith("R-T1-8")]
    assert len(added) == 1


def test_different_areas_commit_concurrently(seeded, db_path: Path):
    t1 = _layers(db_path, "T1")
    t2 = _layers(db_path, "T2")

    def submit(area: str, number: str, earlier: int, later: int) -> str:
        try:
            service.submit_batch(
                db_path,
                area,
                base_version=_version(db_path, area),
                additions=[_addition(number, earlier, later)],
                withdrawals=[],
                note=None,
                busy_timeout_ms=5_000,
            )
            return "ok"
        except VersionConflictError:
            return "conflict"

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(submit, "T1", "R-T1-901", t1["L1"], t1["L4"]),
            pool.submit(submit, "T1", "R-T1-902", t1["L2"], t1["L4"]),
            pool.submit(submit, "T2", "R-T2-901", t2["M1"], t2["M2"]),
            pool.submit(submit, "T2", "R-T2-902", t2["M1"], t2["M2"]),
        ]
        results = [future.result() for future in futures]
    # One publisher per area, both areas publish.
    assert results.count("ok") == 2


def test_readers_keep_previous_snapshot_while_write_is_uncommitted(
    seeded, db_path: Path
):
    layer_id = seeded["demo_rank_layer"]["id"]
    before = service.get_rank(db_path, layer_id, 5_000)
    t2 = _layers(db_path, "T2")

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO records (area_id, record_number, earlier_layer_id, "
            "later_layer_id, created_at) VALUES (2, 'R-T2-UNCOMMITTED', ?, ?, 'now')",
            (t2["M2"], t2["M1"]),
        )
        # An uncommitted cycle-creating row must not be visible to readers.
        during = service.get_rank(db_path, layer_id, 5_000)
        assert during["site_version"] == before["site_version"]
        assert during["earliest_rank"] == before["earliest_rank"]
        assert during["latest_rank"] == before["latest_rank"]
        relations = service.site_relations(db_path, 5_000)
        assert all(
            "R-T2-UNCOMMITTED" not in pair["record_numbers"]
            for pair in relations["relations"]
        )
    finally:
        conn.execute("ROLLBACK")
        conn.close()

    after = service.get_rank(db_path, layer_id, 5_000)
    assert after == before


def test_failed_write_rolls_back_every_statement(seeded, db_path: Path):
    t1 = _layers(db_path, "T1")
    version = _version(db_path, "T1")
    with pytest.raises(CycleDetectedError):
        service.submit_batch(
            db_path,
            "T1",
            base_version=version,
            additions=[
                _addition("R-T1-990", t1["L1"], t1["L4"]),
                _addition("R-T1-991", t1["L4"], t1["L2"]),
            ],
            withdrawals=[],
            note=None,
            busy_timeout_ms=5_000,
        )
    assert _version(db_path, "T1") == version
    numbers = {
        item["record_number"]
        for item in service.list_records(
            db_path, "T1", include_withdrawn=True, busy_timeout_ms=5_000
        )
    }
    assert not {"R-T1-990", "R-T1-991"} & numbers
    assert service.list_batches(db_path, "T1", 5_000)[-1]["additions_count"] == 4


def test_write_transaction_helper_rolls_back_on_exception(db_path: Path):
    from app.db import init_db

    init_db(db_path)
    with pytest.raises(RuntimeError):
        with write_transaction(db_path) as conn:
            conn.execute(
                "INSERT INTO areas (code, name, version, created_at) "
                "VALUES ('X', 'X', 0, 'now')"
            )
            raise RuntimeError("boom")
    with read_transaction(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM areas").fetchone()["n"] == 0


def test_health_and_docs_available(client: TestClient):
    assert client.get("/health").json()["status"] == "ok"
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()
    schema = client.get("/openapi.json").json()
    assert "/ranks/{layer_id}" in schema["paths"]
    assert "撤证后重查名次" in schema["info"]["description"]
    assert schema["tags"]
